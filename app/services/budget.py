"""Embedding metering, per-token write limits and loop breakers (PRD AG-ST-15).

Shadow mode (the default) records what enforcement would do as
``would_rate_limit`` / ``would_circuit_open`` / ``would_defer`` ledger rows and
never blocks, so thresholds can be calibrated on real traffic. Enforce mode
answers 429 ``rate_limited`` or ``circuit_open`` with ``Retry-After``.
All counters commit in their own transaction: cost is real even when the
surrounding write later rolls back.
"""

from __future__ import annotations

import logging
import math
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, literal_column, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.config import get_settings
from app.database import async_session_factory
from app.errors import ApiError, Forbidden
from app.models import UsageLedger
from app.services.common import append_event
from app.stamp_context import current_stamp
from app.warnings_context import add_warning

settings = get_settings()

EMBEDDING_CATEGORY = "embedding_tokens"
WRITE_CATEGORY = "writes"
_request_usage: ContextVar[dict[str, Any] | None] = ContextVar(
    "agentgraph_request_usage", default=None
)
_suppressed: ContextVar[bool] = ContextVar("agentgraph_metering_suppressed", default=False)
logger = logging.getLogger("agentgraph.budget")


class suppress_metering:
    """Platform-generated work (system documents) is not customer usage."""

    def __enter__(self):
        self._token = _suppressed.set(True)

    def __exit__(self, *exc):
        _suppressed.reset(self._token)
        return False


def enforcing() -> bool:
    return settings.budget_mode == "enforce"


def estimate_tokens(chars: int) -> int:
    """~4 characters per token; providers report differently, this is the unit."""
    return max(1, math.ceil(chars / 4)) if chars > 0 else 0


def principal_key(principal: Principal | None = None) -> str:
    stamp = current_stamp() or {}
    key = stamp.get("token_id") or stamp.get("principal_id")
    if key:
        return str(key)
    return principal.agent_signature if principal is not None else "system"


def request_usage() -> dict[str, Any] | None:
    return _request_usage.get()


def _accumulate(tokens: int, remaining: int) -> None:
    usage = _request_usage.get()
    if usage is None:
        usage = {"embedding_tokens": 0, "quota_remaining": remaining}
        _request_usage.set(usage)
    usage["embedding_tokens"] += tokens
    usage["quota_remaining"] = remaining


def _day_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def _ledger(
    session: AsyncSession, workspace_id: uuid.UUID, category: str, amount: int,
    metadata: dict[str, Any],
) -> None:
    session.add(
        UsageLedger(
            workspace_id=workspace_id,
            agent_signature=str((current_stamp() or {}).get("principal_id") or "system")[:128],
            category=category[:32],
            amount=amount,
            cost_usd=0.0,
            metadata_={**metadata, "principal_key": principal_key()},
            created_at=datetime.now(timezone.utc),
        )
    )


async def meter_embedding(
    workspace_id: uuid.UUID, chars: int, *, kind: str, model: str | None = None
) -> None:
    """Record one embedding call and apply the daily workspace quota."""
    tokens = estimate_tokens(chars)
    if tokens <= 0 or _suppressed.get():
        return
    try:
        used = await _record_embedding(workspace_id, tokens, chars, kind, model)
    except Exception:  # noqa: BLE001 - metering must never fail the write it meters
        logger.exception("could not meter embedding usage")
        return
    quota = settings.embedding_tokens_per_day
    if used >= quota * 0.8:
        add_warning(
            "quota_80_percent",
            f"daily embedding quota at {min(100, round(used * 100 / quota))}%",
            used=used,
            quota=quota,
        )
    _accumulate(tokens, max(quota - used, 0))


async def _record_embedding(
    workspace_id: uuid.UUID, tokens: int, chars: int, kind: str, model: str | None
) -> int:
    async with async_session_factory() as session:
        await _ledger(session, workspace_id, EMBEDDING_CATEGORY, tokens,
                      {"kind": kind, "chars": chars, "model": model, "cost_units": tokens})
        await session.flush()
        used = int(
            await session.scalar(
                select(func.coalesce(func.sum(UsageLedger.amount), 0)).where(
                    UsageLedger.workspace_id == workspace_id,
                    UsageLedger.category == EMBEDDING_CATEGORY,
                    UsageLedger.created_at >= _day_start(),
                )
            )
            or 0
        )
        quota = settings.embedding_tokens_per_day
        if used >= quota:
            # AG-ST-13 defers indexing instead of refusing the write; until the
            # async index lands this is recorded only.
            await _ledger(session, workspace_id, "would_defer", 1,
                          {"used": used, "quota": quota})
        await session.commit()
    return used


async def _take_token(
    session: AsyncSession, workspace_id: uuid.UUID, key: str, *, rate: int, burst: int
) -> float:
    """Refill-and-take on a token bucket; returns the tokens left (< 0 = empty)."""
    return float(
        await session.scalar(
            text(
                """
INSERT INTO budget_buckets (workspace_id, bucket_key, tokens, updated_at)
VALUES (CAST(:workspace_id AS uuid), :key, :burst - 1, clock_timestamp())
ON CONFLICT (workspace_id, bucket_key) DO UPDATE SET
    tokens = LEAST(
        :burst,
        budget_buckets.tokens
          + EXTRACT(EPOCH FROM clock_timestamp() - budget_buckets.updated_at) * :rate / 60.0
    ) - 1,
    updated_at = clock_timestamp()
RETURNING tokens
"""
            ),
            {"workspace_id": str(workspace_id), "key": key, "rate": rate, "burst": burst},
        )
    )


async def _open_circuit(
    session: AsyncSession, principal: Principal
) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                "SELECT reason, doc_key, reopen_at FROM budget_circuits "
                "WHERE workspace_id = CAST(:workspace_id AS uuid) AND principal_key = :key "
                "AND cleared_at IS NULL AND reopen_at > NOW() ORDER BY reopen_at DESC LIMIT 1"
            ),
            {"workspace_id": str(principal.workspace.id), "key": principal_key(principal)},
        )
    ).mappings().one_or_none()
    return dict(row) if row else None


def _circuit_error(circuit: dict[str, Any]) -> ApiError:
    retry = max(1, int((circuit["reopen_at"] - datetime.now(timezone.utc)).total_seconds()))
    return ApiError(
        429,
        "circuit_open",
        "writes from this token are paused after a runaway loop; a human can clear it",
        details={
            "reason": circuit["reason"],
            "doc_key": circuit.get("doc_key"),
            "reopen_at": circuit["reopen_at"].isoformat(),
            "retry_after_ms": retry * 1000,
        },
        headers={"Retry-After": str(retry)},
    )


async def check_write(
    principal: Principal,
    *,
    document_id: uuid.UUID | None = None,
    doc_key: str | None = None,
) -> None:
    """Write rate (token bucket) and hot-key loop detection for one write."""
    if not settings.rate_limit_enabled or principal.is_human:
        return
    key = principal_key(principal)
    workspace_id = principal.workspace.id
    async with async_session_factory() as session:
        if enforcing():
            circuit = await _open_circuit(session, principal)
            if circuit is not None:
                raise _circuit_error(circuit)
        left = await _take_token(
            session, workspace_id, f"writes:{key}",
            rate=settings.write_rate_per_minute, burst=settings.write_burst,
        )
        await _ledger(session, workspace_id, WRITE_CATEGORY, 1, {"doc_key": doc_key})
        loop_writes = 0
        if document_id is not None:
            loop_writes = int(
                await session.scalar(
                    text(
                        "SELECT count(DISTINCT content_hash) FROM document_versions "
                        "WHERE document_id = :document_id "
                        "AND created_at > NOW() - INTERVAL '60 seconds' "
                        "AND stamp->>'token_id' IS NOT DISTINCT FROM :token"
                    ),
                    {"document_id": document_id,
                     "token": (current_stamp() or {}).get("token_id")},
                )
                or 0
            )
        hot = loop_writes >= settings.hot_key_writes_per_minute
        if left < 0:
            await _ledger(session, workspace_id, "would_rate_limit", 1,
                          {"dimension": "writes", "doc_key": doc_key})
        if hot:
            await _ledger(session, workspace_id, "would_circuit_open", 1,
                          {"reason": "hot_key_loop", "doc_key": doc_key,
                           "writes_60s": loop_writes + 1})
        if enforcing() and hot:
            reopen = datetime.now(timezone.utc) + timedelta(minutes=settings.circuit_open_minutes)
            await session.execute(
                text(
                    "INSERT INTO budget_circuits (workspace_id, principal_key, reason, doc_key, "
                    "reopen_at) VALUES (CAST(:workspace_id AS uuid), :key, 'hot_key_loop', "
                    ":doc_key, :reopen)"
                ),
                {"workspace_id": str(workspace_id), "key": key, "doc_key": doc_key,
                 "reopen": reopen},
            )
            await append_event(
                session,
                workspace_id=workspace_id,
                agent="budget",
                event_type="BUDGET_CIRCUIT_OPEN",
                payload={"principal": key, "reason": "hot_key_loop", "doc_key": doc_key,
                         "reopen_at": reopen.isoformat()},
            )
        await session.commit()
    if enforcing() and hot:
        raise _circuit_error({"reason": "hot_key_loop", "doc_key": doc_key, "reopen_at": reopen})
    if enforcing() and left < 0:
        retry = max(1, math.ceil(-left * 60 / settings.write_rate_per_minute))
        raise ApiError(
            429,
            "rate_limited",
            "write rate limit for this token exceeded",
            details={"dimension": "writes", "retry_after_ms": retry * 1000},
            headers={"Retry-After": str(retry)},
        )


async def check_query(principal: Principal) -> None:
    if not settings.rate_limit_enabled or principal.is_human:
        return
    key = principal_key(principal)
    async with async_session_factory() as session:
        left = await _take_token(
            session, principal.workspace.id, f"queries:{key}",
            rate=settings.query_rate_per_minute, burst=settings.query_rate_per_minute,
        )
        if left < 0:
            await _ledger(session, principal.workspace.id, "would_rate_limit", 1,
                          {"dimension": "queries"})
        await session.commit()
    if enforcing() and left < 0:
        retry = max(1, math.ceil(-left * 60 / settings.query_rate_per_minute))
        raise ApiError(
            429, "rate_limited", "query rate limit for this token exceeded",
            details={"dimension": "queries", "retry_after_ms": retry * 1000},
            headers={"Retry-After": str(retry)},
        )


_WINDOWS = {"day": timedelta(days=1), "week": timedelta(days=7), "month": timedelta(days=30)}


async def get_usage(
    session: AsyncSession, principal: Principal, window: str = "day"
) -> dict[str, Any]:
    if window not in _WINDOWS:
        raise ApiError(422, "validation_error", "window must be day, week or month",
                       details={"field": "window", "allowed": list(_WINDOWS)})
    since = _day_start() if window == "day" else datetime.now(timezone.utc) - _WINDOWS[window]
    # Inline literal key: a bound parameter would differ between SELECT and
    # GROUP BY and PostgreSQL would reject the grouping.
    key_expr = UsageLedger.metadata_.op("->>")(literal_column("'principal_key'"))
    rows = (
        await session.execute(
            select(
                UsageLedger.category,
                key_expr,
                UsageLedger.agent_signature,
                func.sum(UsageLedger.amount),
            )
            .where(
                UsageLedger.workspace_id == principal.workspace.id,
                UsageLedger.created_at >= since,
            )
            .group_by(UsageLedger.category, key_expr, UsageLedger.agent_signature)
        )
    ).all()
    totals: dict[str, int] = {}
    by_agent: dict[str, dict[str, Any]] = {}
    for category, key, agent, amount in rows:
        totals[category] = totals.get(category, 0) + int(amount or 0)
        bucket = by_agent.setdefault(
            key or agent or "unknown",
            {"principal_key": key, "agent_id": agent, "writes": 0, "embedding_tokens": 0},
        )
        if category == WRITE_CATEGORY:
            bucket["writes"] += int(amount or 0)
        elif category == EMBEDDING_CATEGORY:
            bucket["embedding_tokens"] += int(amount or 0)
    me = principal_key(principal)
    agents = list(by_agent.values())
    if not principal.is_human:
        # Agents see the workspace totals and only their own row.
        agents = [row for row in agents if row["principal_key"] == me]
    circuits = (
        await session.execute(
            text(
                "SELECT principal_key, reason, doc_key, opened_at, reopen_at FROM budget_circuits "
                "WHERE workspace_id = CAST(:workspace_id AS uuid) AND cleared_at IS NULL "
                "AND reopen_at > NOW()"
            ),
            {"workspace_id": str(principal.workspace.id)},
        )
    ).mappings().all()
    return {
        "window": window,
        "mode": settings.budget_mode,
        "embedding_tokens_used": totals.get(EMBEDDING_CATEGORY, 0),
        "embedding_tokens_quota": settings.embedding_tokens_per_day
        * (1 if window == "day" else _WINDOWS[window].days),
        "writes": totals.get(WRITE_CATEGORY, 0),
        "would_rate_limit": totals.get("would_rate_limit", 0),
        "would_circuit_open": totals.get("would_circuit_open", 0),
        "would_defer": totals.get("would_defer", 0),
        "by_agent": agents,
        "circuit_open": [
            dict(row) for row in circuits
            if principal.is_human or row["principal_key"] == me
        ],
        "limits": {
            "write_rate_per_minute": settings.write_rate_per_minute,
            "write_burst": settings.write_burst,
            "query_rate_per_minute": settings.query_rate_per_minute,
            "hot_key_writes_per_minute": settings.hot_key_writes_per_minute,
            "embedding_tokens_per_day": settings.embedding_tokens_per_day,
        },
    }


async def clear_circuit(
    session: AsyncSession, principal: Principal, target_key: str
) -> dict[str, Any]:
    if not principal.is_human or principal.human_role != "OWNER":
        raise Forbidden("only a workspace owner can clear a circuit breaker")
    result = await session.execute(
        text(
            "UPDATE budget_circuits SET cleared_at = NOW(), cleared_by = :by "
            "WHERE workspace_id = CAST(:workspace_id AS uuid) AND principal_key = :key "
            "AND cleared_at IS NULL"
        ),
        {"workspace_id": str(principal.workspace.id), "key": target_key,
         "by": principal.agent_signature},
    )
    await session.commit()
    return {"principal_key": target_key, "cleared": int(result.rowcount or 0)}
