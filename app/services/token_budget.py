"""Per-token daily budgets along the token tree (PRD AG-ST-16).

Every metered unit (a write, a query, embedding tokens) is added to the
calling token and to each ancestor up to the branch, so a branch's ``used``
is its whole subtree. A request is refused with 429 ``budget_exhausted``
when the token or any ancestor has spent a limit; an exhausted embedding
budget defers indexing instead (writes are kept, as in AG-ST-15). A child's
limits never exceed its nearest limited ancestor's; the sum of children may.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.database import async_session_factory
from app.errors import ApiError, Forbidden, NotFound
from app.services.common import append_event
from app.stamp_context import current_stamp
from app.warnings_context import add_warning

logger = logging.getLogger(__name__)
DIMENSIONS = ("writes", "embedding_tokens", "queries")
WARN_AT = 0.8


def _day_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def resets_at() -> datetime:
    return _day_start() + timedelta(days=1)


def chain_of(principal: Principal) -> list[str]:
    """The caller's token and its ancestors up to the branch (the root excluded)."""
    if principal.is_human:
        return []
    root = principal.workspace.root_token_id
    return [token for token in (principal.lineage or []) if token and token != root]


def stamp_chain(stamp: dict[str, Any] | None) -> list[str]:
    if not stamp or stamp.get("kind") in ("human", "admin", "system", "root"):
        return []
    chain = list(stamp.get("chain") or [])
    return chain[1:] if len(chain) > 1 else []


# Limits change rarely: cache them briefly so a write by a token without
# budgets costs no extra connection. Usage is buffered in-process and flushed
# every couple of seconds; checks add the unflushed part.
LIMITS_TTL_SECONDS = 5.0
_limits_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_pending: dict[tuple[str, date, str], int] = defaultdict(int)
_workspace_of: dict[str, Any] = {}
_warned: set[tuple[str, date, str]] = set()


def forget_limits(token: str | None = None) -> None:
    if token is None:
        _limits_cache.clear()
    else:
        _limits_cache.pop(token, None)


async def _limits(chain: list[str]) -> dict[str, dict[str, Any] | None]:
    now = time.monotonic()
    missing = [token for token in chain
               if token not in _limits_cache or now - _limits_cache[token][0] > LIMITS_TTL_SECONDS]
    if missing:
        async with async_session_factory() as session:
            rows = (await session.execute(
                text("SELECT token_id, writes, embedding_tokens, queries FROM token_budgets "
                     "WHERE token_id = ANY(:tokens)"), {"tokens": missing})).mappings().all()
        found = {row["token_id"]: {dimension: row[dimension] for dimension in DIMENSIONS}
                 for row in rows}
        for token in missing:
            _limits_cache[token] = (now, found.get(token))
    return {token: _limits_cache[token][1] for token in chain}


async def _used(tokens: list[str]) -> dict[str, dict[str, int]]:
    day = _day_start().date()
    async with async_session_factory() as session:
        rows = (await session.execute(
            text("SELECT token_id, writes, embedding_tokens, queries, warned FROM token_usage "
                 "WHERE token_id = ANY(:tokens) AND day = :day"),
            {"tokens": tokens, "day": day})).mappings().all()
    stored = {row["token_id"]: row for row in rows}
    result: dict[str, dict[str, int]] = {}
    for token in tokens:
        row = stored.get(token)
        result[token] = {dimension: int(row[dimension] if row else 0)
                         + _pending.get((token, day, dimension), 0)
                         for dimension in DIMENSIONS}
        for dimension in (row["warned"] if row else []):
            _warned.add((token, day, dimension))
    return result


def _limited(chain: list[str], limits: dict[str, dict[str, Any] | None],
             dimension: str) -> list[str]:
    return [token for token in chain
            if limits.get(token) and limits[token].get(dimension) is not None]


async def _spent(chain: list[str], dimension: str) -> dict[str, Any] | None:
    limits = await _limits(chain)
    limited = _limited(chain, limits, dimension)
    if not limited:
        return None
    used = await _used(limited)
    for token in limited:
        limit = limits[token][dimension]
        if used[token][dimension] >= limit:
            return {"token_id": token, "dimension": dimension, "limit": limit,
                    "used": used[token][dimension], "resets_at": resets_at().isoformat()}
    return None


async def check(principal: Principal, dimension: str) -> None:
    chain = chain_of(principal)
    if not chain:
        return
    spent = await _spent(chain, dimension)
    if spent is not None:
        retry = max(1, math.ceil((resets_at() - datetime.now(timezone.utc)).total_seconds()))
        raise ApiError(429, "budget_exhausted",
                       f"the daily {dimension} budget of a token in this chain is spent",
                       details=spent, headers={"Retry-After": str(retry)})


async def embedding_exhausted(chain: list[str] | None = None) -> bool:
    chain = chain if chain is not None else stamp_chain(current_stamp())
    if not chain:
        return False
    try:
        return await _spent(chain, "embedding_tokens") is not None
    except Exception:  # noqa: BLE001 - a failed check must not block the write
        logger.exception("could not read token budgets")
        return False


async def meter(workspace_id: Any, dimension: str, amount: int, *,
                chain: list[str] | None = None, principal: Principal | None = None) -> None:
    """Add usage to the token and every ancestor; warn at 80% once a day."""
    if chain is None:
        chain = chain_of(principal) if principal is not None else stamp_chain(current_stamp())
    if not chain or amount <= 0:
        return
    day = _day_start().date()
    for token in chain:
        _pending[(token, day, dimension)] += amount
        _workspace_of[token] = workspace_id
    try:
        await _warn(workspace_id, dimension, chain, day)
    except Exception:  # noqa: BLE001 - metering must never fail the request it meters
        logger.exception("could not evaluate token budget warnings")


async def _warn(workspace_id: Any, dimension: str, chain: list[str], day: date) -> None:
    limits = await _limits(chain)
    limited = [token for token in _limited(chain, limits, dimension)
               if (token, day, dimension) not in _warned]
    if not limited:
        return
    used = await _used(limited)
    for token in limited:
        limit = limits[token][dimension]
        if used[token][dimension] < limit * WARN_AT or (token, day, dimension) in _warned:
            continue
        _warned.add((token, day, dimension))
        add_warning("budget_warning", f"a token's daily {dimension} budget is at 80%",
                    token_id=token, dimension=dimension, used=used[token][dimension],
                    limit=limit)
        async with async_session_factory() as session:
            await session.execute(
                text("INSERT INTO token_usage (token_id, day, workspace_id, warned) VALUES "
                     "(:t, :day, :ws, ARRAY[:d]) ON CONFLICT (token_id, day) DO UPDATE SET "
                     "warned = array_append(token_usage.warned, :d)"),
                {"t": token, "day": day, "ws": workspace_id, "d": dimension})
            await append_event(
                session, workspace_id=workspace_id, agent="token_budget",
                event_type="BUDGET_WARNING",
                payload={"token_id": token, "dimension": dimension,
                         "used": used[token][dimension], "limit": limit,
                         "targets": await _warn_targets(session, workspace_id, token)})
            await session.commit()


async def flush() -> int:
    """Write buffered usage; on failure it stays buffered for the next round."""
    if not _pending:
        return 0
    batch = dict(_pending)
    _pending.clear()
    try:
        async with async_session_factory() as session:
            for (token, day, dimension), amount in batch.items():
                await session.execute(
                    text(f"INSERT INTO token_usage (token_id, day, workspace_id, {dimension}) "
                         f"VALUES (:t, :day, :ws, :n) ON CONFLICT (token_id, day) DO UPDATE "
                         f"SET {dimension} = token_usage.{dimension} + EXCLUDED.{dimension}"),
                    {"t": token, "day": day, "ws": _workspace_of[token], "n": amount})
            await session.commit()
    except Exception:
        for key, amount in batch.items():
            _pending[key] += amount
        raise
    return len(batch)


async def flush_loop(interval: float = 2.0) -> None:
    while True:
        try:
            await flush()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - background resilience
            logger.exception("token usage flush failed")
        await asyncio.sleep(interval)


async def _rows(session: AsyncSession, chain: list[str]) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text("SELECT b.token_id, b.writes AS lim_writes, b.embedding_tokens AS "
                 "lim_embedding_tokens, b.queries AS lim_queries, COALESCE(u.writes, 0) AS "
                 "writes, COALESCE(u.embedding_tokens, 0) AS embedding_tokens, "
                 "COALESCE(u.queries, 0) AS queries, COALESCE(u.warned, '{}') AS warned "
                 "FROM unnest(CAST(:chain AS text[])) WITH ORDINALITY AS c(token_id, pos) "
                 "LEFT JOIN token_budgets b ON b.token_id = c.token_id "
                 "LEFT JOIN token_usage u ON u.token_id = c.token_id AND u.day = :day "
                 "ORDER BY c.pos"),
            {"chain": chain, "day": _day_start().date()},
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def _warn_targets(session: AsyncSession, workspace_id: Any, token: str) -> list[str]:
    targets: list[str] = []
    issuer = (await session.execute(
        text("SELECT issued_by_token_id, issued_by_human FROM agent_tokens "
             "WHERE canonical_id = :t"), {"t": token})).one_or_none()
    if issuer is not None:
        if issuer[0]:
            targets += [f"agent:{agent}" for agent in (await session.execute(
                text("SELECT agent_id FROM agent_directory WHERE :t = ANY(token_ids)"),
                {"t": issuer[0]})).scalars().all()]
            targets.append(f"token:{issuer[0]}")
        if issuer[1]:
            targets.append(f"human:{issuer[1]}")
    targets += [f"human:{owner}" for owner in (await session.execute(
        text("SELECT id FROM human_users WHERE workspace_id = :ws AND role = 'OWNER' "
             "AND is_active"), {"ws": workspace_id})).scalars().all()]
    return list(dict.fromkeys(targets))


# ------------------------------------------------------------------ API


async def _lineage_of(session: AsyncSession, principal: Principal, ref: str) -> list[str]:
    workspace = principal.workspace
    if ref == "self":
        ref = principal.token_cid or ""
    if ref == workspace.key_token_id:
        return [workspace.root_token_id, workspace.key_token_id]
    row = (await session.execute(
        text("SELECT canonical_id, lineage FROM agent_tokens WHERE workspace_id = :ws AND "
             "(canonical_id = :r OR CAST(id AS text) = :r)"),
        {"ws": workspace.id, "r": ref})).one_or_none()
    if row is None:
        raise NotFound("agent_token", f"token '{ref}' not found")
    return list(row[1] or [workspace.root_token_id, row[0]])


def _may_read(principal: Principal, lineage: list[str]) -> bool:
    return principal.is_human or (principal.token_cid in lineage)


async def _budget(session: AsyncSession, token: str) -> dict[str, Any]:
    rows = await _rows(session, [token])
    row = rows[0] if rows else {}
    limits = {dimension: row.get(f"lim_{dimension}") for dimension in DIMENSIONS}
    used = {dimension: int(row.get(dimension) or 0) for dimension in DIMENSIONS}
    state = "ok"
    for dimension in DIMENSIONS:
        if limits[dimension] is None:
            continue
        if used[dimension] >= limits[dimension]:
            state = "exhausted"
            break
        if used[dimension] >= limits[dimension] * WARN_AT:
            state = "warning"
    return {"budget_id": f"bud_{token}", "token_id": token, "window": "day",
            "limits": limits, "used": used, "state": state,
            "resets_at": resets_at().isoformat()}


async def get_budget(principal: Principal, ref: str) -> dict[str, Any]:
    await flush()
    async with async_session_factory() as session:
        lineage = await _lineage_of(session, principal, ref)
        if not _may_read(principal, lineage):
            raise Forbidden("only the token, its ancestors and people read its budget")
        root = principal.workspace.root_token_id
        token = lineage[-1]
        result = await _budget(session, token)
        ancestors = []
        for ancestor in reversed([item for item in lineage[:-1] if item != root]):
            budget = await _budget(session, ancestor)
            remaining = {dimension: (budget["limits"][dimension] - budget["used"][dimension])
                         if budget["limits"][dimension] is not None else None
                         for dimension in DIMENSIONS}
            ancestors.append({"token_id": ancestor, "state": budget["state"],
                              "remaining": remaining})
    return {**result, "ancestors": ancestors}


async def set_budget(principal: Principal, ref: str, limits: dict[str, Any]) -> dict[str, Any]:
    async with async_session_factory() as session:
        lineage = await _lineage_of(session, principal, ref)
        token = lineage[-1]
        ancestor = principal.token_cid in lineage[:-1]
        if not (principal.is_human or ancestor):
            raise Forbidden("only an ancestor token or a person sets a token's budget")
        root = principal.workspace.root_token_id
        parents = [item for item in reversed(lineage[:-1]) if item != root]
        for dimension in DIMENSIONS:
            value = limits.get(dimension)
            if value is None:
                continue
            for parent in parents:
                parent_limit = (await _budget(session, parent))["limits"][dimension]
                if parent_limit is not None:
                    if value > parent_limit:
                        raise ApiError(422, "budget_exceeds_parent",
                                       "a token's limit cannot exceed its parent's",
                                       details={"dimension": dimension, "limit": value,
                                                "parent_token_id": parent,
                                                "parent_limit": parent_limit})
                    break
        await session.execute(
            text("INSERT INTO token_budgets (token_id, workspace_id, writes, embedding_tokens, "
                 "queries, updated_by, updated_at) VALUES (:t, :ws, :writes, :embedding, "
                 ":queries, :by, NOW()) ON CONFLICT (token_id) DO UPDATE SET writes = "
                 "EXCLUDED.writes, embedding_tokens = EXCLUDED.embedding_tokens, queries = "
                 "EXCLUDED.queries, updated_by = EXCLUDED.updated_by, updated_at = NOW()"),
            {"t": token, "ws": principal.workspace.id, "writes": limits.get("writes"),
             "embedding": limits.get("embedding_tokens"), "queries": limits.get("queries"),
             "by": principal.agent_signature[:256]})
        await session.commit()
    forget_limits(token)
    return await get_budget(principal, token)


async def list_budgets(principal: Principal, subtree_of: str | None) -> dict[str, Any]:
    await flush()
    async with async_session_factory() as session:
        top = (await _lineage_of(session, principal, subtree_of or "self"))[-1] \
            if subtree_of or not principal.is_human else None
        if top is not None and not _may_read(
                principal, await _lineage_of(session, principal, top)):
            raise Forbidden("only the token, its ancestors and people list its subtree")
        tokens = (await session.execute(
            text("SELECT canonical_id FROM agent_tokens WHERE workspace_id = :ws AND "
                 "(CAST(:top AS text) IS NULL OR :top = ANY(lineage) OR canonical_id = :top) "
                 "ORDER BY created_at"),
            {"ws": principal.workspace.id, "top": top})).scalars().all()
        items = [await _budget(session, token) for token in tokens]
    return {"items": items, "subtree_of": top}
