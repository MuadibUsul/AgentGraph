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


_LIMIT_FIELDS = (
    "embedding_tokens_per_day", "write_rate_per_minute", "write_burst", "query_rate_per_minute",
)


def _default_limits() -> dict[str, Any]:
    return {field: getattr(settings, field) for field in _LIMIT_FIELDS} | {"tenant_caps": []}


async def limits_for(session: AsyncSession, workspace_id: uuid.UUID) -> dict[str, Any]:
    """Effective limits: settings, then the workspace override; tenant caps on top."""
    limits = _default_limits()
    rows = (
        await session.execute(
            text(
                "SELECT bl.scope_kind, bl.scope_id, bl.embedding_tokens_per_day, "
                "bl.write_rate_per_minute, bl.write_burst, bl.query_rate_per_minute "
                "FROM workspaces w JOIN tenants t ON t.id = w.tenant_id "
                "JOIN budget_limits bl ON "
                "(bl.scope_kind = 'workspace' AND bl.scope_id = CAST(w.id AS text)) "
                "OR (bl.scope_kind = 'tenant' AND bl.scope_id = ANY(t.path)) "
                "WHERE w.id = CAST(:workspace_id AS uuid)"
            ),
            {"workspace_id": str(workspace_id)},
        )
    ).mappings().all()
    for row in rows:
        if row["scope_kind"] == "workspace":
            for field in _LIMIT_FIELDS:
                if row[field] is not None:
                    limits[field] = int(row[field])
        elif row["embedding_tokens_per_day"] is not None:
            limits["tenant_caps"].append((row["scope_id"], int(row["embedding_tokens_per_day"])))
    return limits


async def _embedding_used(session: AsyncSession, workspace_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.coalesce(func.sum(UsageLedger.amount), 0)).where(
                UsageLedger.workspace_id == workspace_id,
                UsageLedger.category == EMBEDDING_CATEGORY,
                UsageLedger.created_at >= _day_start(),
            )
        )
        or 0
    )


async def embedding_state(
    session: AsyncSession, workspace_id: uuid.UUID, limits: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Today's embedding use against the workspace quota and any tenant caps."""
    limits = limits or await limits_for(session, workspace_id)
    used = await _embedding_used(session, workspace_id)
    quota = limits["embedding_tokens_per_day"]
    state = {"used": used, "quota": quota, "exhausted": used >= quota, "scope": "workspace"}
    for tenant_id, cap in limits["tenant_caps"]:
        tenant_used = int(
            await session.scalar(
                text(
                    "SELECT COALESCE(SUM(amount), 0) FROM usage_ledger WHERE category = :category "
                    "AND created_at >= :since AND tenant_id IN "
                    "(SELECT id FROM tenants WHERE :tenant_id = ANY(path))"
                ),
                {"category": EMBEDDING_CATEGORY, "since": _day_start(), "tenant_id": tenant_id},
            )
            or 0
        )
        if tenant_used >= cap and not state["exhausted"]:
            state.update(exhausted=True, scope="tenant", tenant_id=tenant_id,
                         tenant_used=tenant_used, tenant_quota=cap)
    return state


async def defer_embedding(workspace_id: uuid.UUID) -> bool:
    """True when enforcement is on and the daily embedding quota is spent.

    Writes are never refused for embedding cost: the document is stored and
    its indexing waits for the next day's quota (index_state=deferred).
    """
    if not enforcing() or _suppressed.get():
        return False
    try:
        async with async_session_factory() as session:
            return bool((await embedding_state(session, workspace_id))["exhausted"])
    except Exception:  # noqa: BLE001 - a failed check must not block the write
        logger.exception("could not read the embedding quota")
        return False


def next_quota_reset() -> datetime:
    return _day_start() + timedelta(days=1)


async def meter_embedding(
    workspace_id: uuid.UUID, chars: int, *, kind: str, model: str | None = None
) -> None:
    """Record one embedding call and apply the daily workspace quota."""
    tokens = estimate_tokens(chars)
    if tokens <= 0 or _suppressed.get():
        return
    try:
        used, quota = await _record_embedding(workspace_id, tokens, chars, kind, model)
    except Exception:  # noqa: BLE001 - metering must never fail the write it meters
        logger.exception("could not meter embedding usage")
        return
    if used >= quota * 0.8:
        add_warning(
            "quota_80_percent",
            f"daily embedding quota at {min(100, round(used * 100 / max(quota, 1)))}%",
            used=used,
            quota=quota,
        )
    _accumulate(tokens, max(quota - used, 0))


async def _record_embedding(
    workspace_id: uuid.UUID, tokens: int, chars: int, kind: str, model: str | None
) -> tuple[int, int]:
    async with async_session_factory() as session:
        await _ledger(session, workspace_id, EMBEDDING_CATEGORY, tokens,
                      {"kind": kind, "chars": chars, "model": model, "cost_units": tokens})
        await session.flush()
        state = await embedding_state(session, workspace_id)
        if state["exhausted"] and not enforcing():
            # Shadow mode: enforcement would defer indexing from here on.
            await _ledger(session, workspace_id, "would_defer", 1,
                          {"used": state["used"], "quota": state["quota"],
                           "scope": state["scope"]})
        await session.commit()
    return state["used"], state["quota"]


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
    session: AsyncSession, principal: Principal, reasons: tuple[str, ...] | None = None
) -> dict[str, Any] | None:
    reason_filter = "AND reason = ANY(:reasons) " if reasons else ""
    params: dict[str, Any] = {
        "workspace_id": str(principal.workspace.id), "key": principal_key(principal),
    }
    if reasons:
        params["reasons"] = list(reasons)
    row = (
        await session.execute(
            text(
                "SELECT reason, doc_key, reopen_at FROM budget_circuits "
                "WHERE workspace_id = CAST(:workspace_id AS uuid) AND principal_key = :key "
                f"AND cleared_at IS NULL AND reopen_at > NOW() {reason_filter}"
                "ORDER BY reopen_at DESC LIMIT 1"
            ),
            params,
        )
    ).mappings().one_or_none()
    return dict(row) if row else None


def _circuit_error(circuit: dict[str, Any]) -> ApiError:
    retry = max(1, math.ceil((circuit["reopen_at"] - datetime.now(timezone.utc)).total_seconds()))
    return ApiError(
        429,
        "circuit_open",
        "requests from this token are paused; a human can clear the circuit",
        details={
            "reason": circuit["reason"],
            "doc_key": circuit.get("doc_key"),
            "reopen_at": circuit["reopen_at"].isoformat(),
            "retry_after_ms": retry * 1000,
        },
        headers={"Retry-After": str(retry)},
    )


def _rate_limited(dimension: str, retry: int, **details: Any) -> ApiError:
    return ApiError(
        429,
        "rate_limited",
        f"{dimension} rate limit for this token exceeded",
        details={"dimension": dimension, "retry_after_ms": retry * 1000, **details},
        headers={"Retry-After": str(retry)},
    )


async def _trip_circuit(
    session: AsyncSession, workspace_id: uuid.UUID, key: str, *, reason: str,
    minutes: int, doc_key: str | None = None, details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reopen = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    await session.execute(
        text(
            "INSERT INTO budget_circuits (workspace_id, principal_key, reason, doc_key, "
            "reopen_at) VALUES (CAST(:workspace_id AS uuid), :key, :reason, :doc_key, :reopen)"
        ),
        {"workspace_id": str(workspace_id), "key": key, "reason": reason, "doc_key": doc_key,
         "reopen": reopen},
    )
    await append_event(
        session,
        workspace_id=workspace_id,
        agent="budget",
        event_type="BUDGET_CIRCUIT_OPEN",
        payload={"principal": key, "reason": reason, "doc_key": doc_key,
                 "reopen_at": reopen.isoformat(), **(details or {})},
    )
    return {"reason": reason, "doc_key": doc_key, "reopen_at": reopen}


async def _check_backoff(
    session: AsyncSession, workspace_id: uuid.UUID, key: str, dimension: str
) -> ApiError | None:
    """Refuse a retry sent before Retry-After (AG-ST-15 2a), without taking a token.

    Each early retry doubles the next wait (capped); enough of them in a row
    open a short ``ignored_backoff`` circuit. A retry that waited clears it.
    """
    row = (
        await session.execute(
            text(
                "SELECT retry_until, penalty_seconds, violations FROM budget_backoff "
                "WHERE workspace_id = CAST(:workspace_id AS uuid) AND principal_key = :key "
                "AND dimension = :dimension FOR UPDATE"
            ),
            {"workspace_id": str(workspace_id), "key": key, "dimension": dimension},
        )
    ).mappings().one_or_none()
    if row is None:
        return None
    params = {"workspace_id": str(workspace_id), "key": key, "dimension": dimension}
    if row["retry_until"] <= datetime.now(timezone.utc):
        await session.execute(
            text(
                "DELETE FROM budget_backoff WHERE workspace_id = CAST(:workspace_id AS uuid) "
                "AND principal_key = :key AND dimension = :dimension"
            ),
            params,
        )
        return None
    violations = int(row["violations"]) + 1
    penalty = min(settings.backoff_max_seconds, max(1, int(row["penalty_seconds"])) * 2)
    await _ledger(session, workspace_id, "ignored_backoff", 1,
                  {"dimension": dimension, "violations": violations})
    if violations >= settings.backoff_violations_to_trip:
        await session.execute(
            text(
                "DELETE FROM budget_backoff WHERE workspace_id = CAST(:workspace_id AS uuid) "
                "AND principal_key = :key AND dimension = :dimension"
            ),
            params,
        )
        circuit = await _trip_circuit(
            session, workspace_id, key, reason="ignored_backoff",
            minutes=settings.backoff_circuit_minutes,
            details={"dimension": dimension, "violations": violations},
        )
        return _circuit_error(circuit)
    await session.execute(
        text(
            "UPDATE budget_backoff SET retry_until = NOW() + make_interval(secs => :penalty), "
            "penalty_seconds = :penalty, violations = :violations, updated_at = NOW() "
            "WHERE workspace_id = CAST(:workspace_id AS uuid) AND principal_key = :key "
            "AND dimension = :dimension"
        ),
        {**params, "penalty": penalty, "violations": violations},
    )
    return _rate_limited(dimension, penalty, reason="ignored_backoff", violations=violations)


async def _start_backoff(
    session: AsyncSession, workspace_id: uuid.UUID, key: str, dimension: str, retry: int
) -> None:
    await session.execute(
        text(
            "INSERT INTO budget_backoff (workspace_id, principal_key, dimension, retry_until, "
            "penalty_seconds) VALUES (CAST(:workspace_id AS uuid), :key, :dimension, "
            "NOW() + make_interval(secs => :retry), :retry) "
            "ON CONFLICT (workspace_id, principal_key, dimension) DO UPDATE SET "
            "retry_until = EXCLUDED.retry_until, penalty_seconds = EXCLUDED.penalty_seconds, "
            "updated_at = NOW()"
        ),
        {"workspace_id": str(workspace_id), "key": key, "dimension": dimension, "retry": retry},
    )


async def _write_spike(
    session: AsyncSession, workspace_id: uuid.UUID, key: str, limits: dict[str, Any]
) -> dict[str, Any] | None:
    """Writes this minute far above the token's own 7-day p99 for this hour."""
    params = {"workspace_id": str(workspace_id), "key": key}
    current = int(
        await session.scalar(
            text(
                "SELECT count(*) FROM usage_ledger "
                "WHERE workspace_id = CAST(:workspace_id AS uuid) "
                "AND category = 'writes' AND metadata->>'principal_key' = :key "
                "AND created_at > NOW() - INTERVAL '60 seconds'"
            ),
            params,
        )
        or 0
    )
    if current < limits["write_burst"]:
        return None  # cheap exit: a spike needs at least a burst's worth
    row = (
        await session.execute(
            text(
                """
WITH minutes AS (
    SELECT date_trunc('minute', created_at) AS minute, count(*) AS writes
    FROM usage_ledger
    WHERE workspace_id = CAST(:workspace_id AS uuid) AND category = 'writes'
      AND metadata->>'principal_key' = :key
      AND created_at > NOW() - INTERVAL '7 days' AND created_at < NOW() - INTERVAL '1 hour'
      AND extract(hour FROM created_at) = extract(hour FROM NOW())
    GROUP BY 1
)
SELECT percentile_cont(0.99) WITHIN GROUP (ORDER BY writes) AS p99, count(*) AS minutes
FROM minutes
"""
            ),
            params,
        )
    ).mappings().one()
    if int(row["minutes"] or 0) < settings.write_spike_min_history_minutes or not row["p99"]:
        return None
    p99 = float(row["p99"])
    if current > p99 * settings.write_spike_multiplier:
        return {"writes_60s": current, "p99_per_minute": p99}
    return None


async def check_write(
    principal: Principal,
    *,
    document_id: uuid.UUID | None = None,
    doc_key: str | None = None,
) -> None:
    """Write rate (token bucket), forced backoff and runaway-loop detection."""
    if not settings.rate_limit_enabled or principal.is_human:
        return
    key = principal_key(principal)
    workspace_id = principal.workspace.id
    refusal: ApiError | None = None
    async with async_session_factory() as session:
        limits = await limits_for(session, workspace_id)
        if enforcing():
            circuit = await _open_circuit(session, principal)
            if circuit is not None:
                raise _circuit_error(circuit)
            refusal = await _check_backoff(session, workspace_id, key, "writes")
            if refusal is not None:
                await session.commit()
                raise refusal
        left = await _take_token(
            session, workspace_id, f"writes:{key}",
            rate=limits["write_rate_per_minute"], burst=limits["write_burst"],
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
        trip: dict[str, Any] | None = None
        if loop_writes >= settings.hot_key_writes_per_minute:
            trip = {"reason": "hot_key_loop", "doc_key": doc_key, "writes_60s": loop_writes + 1}
        else:
            spike = await _write_spike(session, workspace_id, key, limits)
            if spike is not None:
                trip = {"reason": "write_spike", "doc_key": None, **spike}
        if left < 0:
            await _ledger(session, workspace_id, "would_rate_limit", 1,
                          {"dimension": "writes", "doc_key": doc_key})
        if trip is not None:
            await _ledger(session, workspace_id, "would_circuit_open", 1, trip)
        if enforcing() and trip is not None:
            circuit = await _trip_circuit(
                session, workspace_id, key, reason=trip["reason"],
                minutes=settings.circuit_open_minutes, doc_key=trip["doc_key"],
                details={k: v for k, v in trip.items() if k not in ("reason", "doc_key")},
            )
            refusal = _circuit_error(circuit)
        elif enforcing() and left < 0:
            retry = max(1, math.ceil(-left * 60 / limits["write_rate_per_minute"]))
            await _start_backoff(session, workspace_id, key, "writes", retry)
            refusal = _rate_limited("writes", retry)
        await session.commit()
    if refusal is not None:
        raise refusal


async def check_query(principal: Principal) -> None:
    if not settings.rate_limit_enabled or principal.is_human:
        return
    key = principal_key(principal)
    workspace_id = principal.workspace.id
    refusal: ApiError | None = None
    async with async_session_factory() as session:
        limits = await limits_for(session, workspace_id)
        if enforcing():
            circuit = await _open_circuit(session, principal, ("ignored_backoff",))
            if circuit is not None:
                raise _circuit_error(circuit)
            refusal = await _check_backoff(session, workspace_id, key, "queries")
            if refusal is not None:
                await session.commit()
                raise refusal
        rate = limits["query_rate_per_minute"]
        left = await _take_token(session, workspace_id, f"queries:{key}", rate=rate, burst=rate)
        if left < 0:
            await _ledger(session, workspace_id, "would_rate_limit", 1,
                          {"dimension": "queries"})
            if enforcing():
                retry = max(1, math.ceil(-left * 60 / rate))
                await _start_backoff(session, workspace_id, key, "queries", retry)
                refusal = _rate_limited("queries", retry)
        await session.commit()
    if refusal is not None:
        raise refusal


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
    # Limits live outside row security; read them with the platform session.
    async with async_session_factory() as platform:
        limits = await limits_for(platform, principal.workspace.id)
    return {
        "window": window,
        "mode": settings.budget_mode,
        "embedding_tokens_used": totals.get(EMBEDDING_CATEGORY, 0),
        "embedding_tokens_quota": limits["embedding_tokens_per_day"]
        * (1 if window == "day" else _WINDOWS[window].days),
        "writes": totals.get(WRITE_CATEGORY, 0),
        "would_rate_limit": totals.get("would_rate_limit", 0),
        "would_circuit_open": totals.get("would_circuit_open", 0),
        "would_defer": totals.get("would_defer", 0),
        "ignored_backoff": totals.get("ignored_backoff", 0),
        "by_agent": agents,
        "circuit_open": [
            dict(row) for row in circuits
            if principal.is_human or row["principal_key"] == me
        ],
        "limits": {
            **{field: limits[field] for field in _LIMIT_FIELDS},
            "hot_key_writes_per_minute": settings.hot_key_writes_per_minute,
            "max_in_flight_per_token": settings.max_in_flight_per_token,
            "backoff_max_seconds": settings.backoff_max_seconds,
        },
    }


async def get_limits(scope_kind: str, scope_id: str) -> dict[str, Any]:
    async with async_session_factory() as session:
        row = (
            await session.execute(
                text("SELECT * FROM budget_limits WHERE scope_kind = :kind AND scope_id = :id"),
                {"kind": scope_kind, "id": scope_id},
            )
        ).mappings().one_or_none()
    values = {field: (row[field] if row else None) for field in _LIMIT_FIELDS}
    return {
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        **values,
        "defaults": {field: getattr(settings, field) for field in _LIMIT_FIELDS},
        "updated_at": row["updated_at"].isoformat() if row else None,
    }


async def set_limits(
    scope_kind: str, scope_id: str, values: dict[str, Any], *, updated_by: str
) -> dict[str, Any]:
    """Admin override; ``None`` falls back to the deployment default."""
    if scope_kind == "tenant" and any(
        values.get(field) is not None
        for field in _LIMIT_FIELDS
        if field != "embedding_tokens_per_day"
    ):
        raise ApiError(422, "validation_error",
                       "a tenant override caps embedding_tokens_per_day only",
                       details={"field": "scope_kind"})
    fields = {field: values.get(field) for field in _LIMIT_FIELDS}
    async with async_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO budget_limits (scope_kind, scope_id, embedding_tokens_per_day, "
                "write_rate_per_minute, write_burst, query_rate_per_minute, updated_by, "
                "updated_at) "
                "VALUES (:kind, :id, :embedding_tokens_per_day, :write_rate_per_minute, "
                ":write_burst, :query_rate_per_minute, :by, NOW()) "
                "ON CONFLICT (scope_kind, scope_id) DO UPDATE SET "
                "embedding_tokens_per_day = EXCLUDED.embedding_tokens_per_day, "
                "write_rate_per_minute = EXCLUDED.write_rate_per_minute, "
                "write_burst = EXCLUDED.write_burst, "
                "query_rate_per_minute = EXCLUDED.query_rate_per_minute, "
                "updated_by = EXCLUDED.updated_by, updated_at = NOW()"
            ),
            {"kind": scope_kind, "id": scope_id, "by": updated_by[:256], **fields},
        )
        await session.commit()
    return await get_limits(scope_kind, scope_id)


async def clear_circuit(
    session: AsyncSession, principal: Principal, target_key: str
) -> dict[str, Any]:
    if not principal.is_human or principal.human_role not in ("OWNER", "ADMIN"):
        raise Forbidden("only an owner or admin can clear a circuit breaker")
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
