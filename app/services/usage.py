"""Workspace budgets, usage ledger, and fixed-window request rate limits."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.config import get_settings
from app.database import async_session_factory
from app.errors import ApiError, BudgetExceeded, RateLimited
from app.models import AgentToken, UsageLedger, WorkspaceBudget
from app.tenant import set_session_tenant

settings = get_settings()


def default_budget(workspace_id: uuid.UUID) -> WorkspaceBudget:
    return WorkspaceBudget(
        workspace_id=workspace_id,
        monthly_token_budget=settings.default_monthly_token_budget,
        monthly_cost_budget_usd=settings.default_monthly_cost_budget_usd,
        max_requests_per_minute=settings.rate_limit_default_per_minute,
        max_concurrent_leases=settings.default_max_concurrent_leases,
        updated_at=datetime.now(timezone.utc),
    )


async def get_budget(
    session: AsyncSession, workspace_id: uuid.UUID
) -> WorkspaceBudget:
    row = await session.get(WorkspaceBudget, workspace_id)
    return row or default_budget(workspace_id)


async def set_budget(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    monthly_token_budget: int,
    monthly_cost_budget_usd: float,
    max_requests_per_minute: int,
    max_concurrent_leases: int,
) -> WorkspaceBudget:
    row = await session.get(WorkspaceBudget, workspace_id)
    if row is None:
        row = WorkspaceBudget(workspace_id=workspace_id)
        session.add(row)
    row.monthly_token_budget = monthly_token_budget
    row.monthly_cost_budget_usd = monthly_cost_budget_usd
    row.max_requests_per_minute = max_requests_per_minute
    row.max_concurrent_leases = max_concurrent_leases
    row.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return row


async def enforce_rate_limit(session: AsyncSession, principal: Principal) -> None:
    """Count one request for this principal in a fixed window.

    A separate short transaction is used so GET endpoints cannot silently roll
    back their rate counter. The counter table has no tenant business data.
    """
    if not settings.rate_limit_enabled:
        return
    actor_key = (
        str(principal.token_id)
        if principal.token_id
        else str(principal.user_id) if principal.user_id else "workspace"
    )
    identity = f"{principal.workspace.id}:{principal.auth_kind.value}:{actor_key}"
    bucket = (
        f"{principal.workspace.id}:"
        + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    )
    limit = settings.rate_limit_default_per_minute
    async with async_session_factory() as counter_session:
        set_session_tenant(counter_session, principal.workspace.id)
        budget = await get_budget(counter_session, principal.workspace.id)
        limit = budget.max_requests_per_minute
        if principal.token_id is not None:
            token = await counter_session.get(AgentToken, principal.token_id)
            if token is not None and token.per_minute_limit:
                limit = min(limit, token.per_minute_limit)
        count = await counter_session.scalar(
            text(
                """
INSERT INTO rate_limit_windows (workspace_id, bucket, window_start, count)
VALUES (
    CAST(:workspace_id AS uuid),
    :bucket,
    to_timestamp(floor(extract(epoch from now()) / :window) * :window),
    1
)
ON CONFLICT (bucket, window_start)
DO UPDATE SET count = rate_limit_windows.count + 1
RETURNING count
"""
            ),
            {
                "workspace_id": str(principal.workspace.id),
                "bucket": bucket,
                "window": max(settings.rate_limit_window_seconds, 1),
            },
        )
        await counter_session.commit()
    if int(count or 0) > limit:
        raise RateLimited(settings.rate_limit_window_seconds)


async def enforce_embedding_budget(
    session: AsyncSession, workspace_id: uuid.UUID, chars: int
) -> None:
    """Bound embedding spend per workspace per minute.

    This counter is committed in its own transaction: provider calls already
    incurred the cost even if the surrounding document write later rolls back.
    """
    if not settings.rate_limit_enabled or chars <= 0:
        return
    window = 60
    bucket = f"embedding-bytes:{workspace_id}"[:160]
    async with async_session_factory() as counter_session:
        count = await counter_session.scalar(
            text(
                """
INSERT INTO rate_limit_windows (workspace_id, bucket, window_start, count)
VALUES (
    CAST(:workspace_id AS uuid),
    :bucket,
    to_timestamp(floor(extract(epoch from now()) / :window) * :window),
    :chars
)
ON CONFLICT (bucket, window_start)
DO UPDATE SET count = rate_limit_windows.count + EXCLUDED.count
RETURNING count
"""
            ),
            {
                "workspace_id": str(workspace_id),
                "bucket": bucket,
                "window": window,
                "chars": chars,
            },
        )
        await counter_session.commit()
    if int(count or 0) > settings.embedding_bytes_per_minute:
        raise ApiError(
            429,
            "embedding_budget_exceeded",
            "workspace embedding byte budget is exhausted; retry later",
            details={
                "window_seconds": window,
                "limit_bytes": settings.embedding_bytes_per_minute,
            },
        )


async def record_usage(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    agent_signature: str | None,
    category: str,
    amount: int = 0,
    cost_usd: float = 0.0,
    metadata: dict[str, Any] | None = None,
) -> UsageLedger:
    entry = UsageLedger(
        workspace_id=workspace_id,
        agent_signature=agent_signature,
        category=category[:32],
        amount=amount,
        cost_usd=cost_usd,
        metadata_=metadata or {},
        created_at=datetime.now(timezone.utc),
    )
    session.add(entry)
    return entry


async def month_usage(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    token_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(days=30)
    query = select(
        func.coalesce(func.sum(UsageLedger.amount), 0).label("tokens"),
        func.coalesce(func.sum(UsageLedger.cost_usd), 0).label("cost"),
        func.count(UsageLedger.id).label("entries"),
    ).where(
        UsageLedger.workspace_id == workspace_id,
        UsageLedger.created_at >= since,
    )
    if token_id is not None:
        query = query.where(
            UsageLedger.metadata_["token_id"].astext == str(token_id)
        )
    row = (await session.execute(query)).one()
    budget = await get_budget(session, workspace_id)
    return {
        "tokens_used": int(row.tokens or 0),
        "cost_usd_used": float(row.cost or 0),
        "entries": int(row.entries or 0),
        "tokens_limit": int(budget.monthly_token_budget),
        "cost_usd_limit": float(budget.monthly_cost_budget_usd),
        "requests_limit_per_minute": int(budget.max_requests_per_minute),
        "max_concurrent_leases": int(budget.max_concurrent_leases),
        "period_days": 30,
    }


async def assert_budget(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    token_delta: int = 0,
    cost_delta: float = 0.0,
    token_id: uuid.UUID | None = None,
) -> None:
    summary = await month_usage(session, workspace_id, token_id=token_id)
    token_limit = summary["tokens_limit"]
    cost_limit = summary["cost_usd_limit"]
    if token_id is not None:
        token = await session.get(AgentToken, token_id)
        if token is not None and token.monthly_token_budget is not None:
            token_limit = min(token_limit, token.monthly_token_budget)
    if token_delta and summary["tokens_used"] + token_delta > token_limit:
        raise BudgetExceeded(
            "monthly_token", summary["tokens_used"] + token_delta, token_limit
        )
    if cost_delta and summary["cost_usd_used"] + cost_delta > cost_limit:
        raise BudgetExceeded(
            "monthly_cost_usd",
            summary["cost_usd_used"] + cost_delta,
            cost_limit,
        )
