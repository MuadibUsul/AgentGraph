"""Timeline append/read helpers."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, effective_agent, ensure_namespace, require_permission
from app.config import get_settings
from app.errors import Forbidden
from app.models import TimelineStream, Workspace
from app.schemas import TimelineEventRequest, TimelineItemOut
from app.services.common import append_event, timeline_out, utcnow
from app.services.usage import assert_budget, record_usage

settings = get_settings()


async def record_timeline_event(
    session: AsyncSession,
    principal: Principal,
    request: TimelineEventRequest,
) -> TimelineItemOut:
    require_permission(principal, "timeline:append")
    ensure_namespace(principal, request.namespace, write=True)
    agent = effective_agent(principal, request.agent)
    await assert_budget(
        session,
        principal.workspace.id,
        token_delta=request.token_cost,
    )
    event = await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=agent,
        event_type=request.event_type,
        payload=request.payload,
        namespace=request.namespace,
        platform=request.platform,
        token_cost=request.token_cost,
        latency_ms=request.latency_ms,
    )
    if request.token_cost:
        await record_usage(
            session,
            workspace_id=principal.workspace.id,
            agent_signature=agent,
            category="timeline",
            amount=request.token_cost,
            metadata={"event_type": request.event_type},
        )
    await session.commit()
    return timeline_out(event)


async def list_timeline(
    session: AsyncSession,
    workspace: Workspace,
    *,
    limit: int = 50,
    hours: int = 24,
    event_type: str | None = None,
    namespace: str | None = None,
    allowed_namespaces: list[str] | None = None,
) -> list[TimelineItemOut]:
    since = utcnow() - timedelta(hours=hours)
    query = select(TimelineStream).where(
        TimelineStream.workspace_id == workspace.id,
        TimelineStream.created_at >= since,
    )
    if event_type:
        query = query.where(TimelineStream.event_type == event_type)
    if namespace:
        if allowed_namespaces is not None and namespace not in allowed_namespaces:
            raise Forbidden("namespace is outside this token's scope")
        query = query.where(TimelineStream.namespace == namespace)
    elif allowed_namespaces is not None:
        query = query.where(TimelineStream.namespace.in_(allowed_namespaces))
    events = (
        await session.execute(
            query.order_by(TimelineStream.created_at.desc()).limit(limit)
        )
    ).scalars().all()
    return [timeline_out(event) for event in events]


__all__ = ["append_event", "list_timeline", "record_timeline_event", "timeline_out"]
