"""Replayable SSE over the transactional workspace_events outbox."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.auth import (
    Principal,
    RoleScope,
    extract_bearer_token,
    principal_for_admin,
    require_admin,
    resolve_principal,
)
from app.config import get_settings
from app.database import async_session_factory
from app.errors import ApiError
from app.events import event_bus
from app.models import WorkspaceEvent
from app.services.common import domain_event_type
from app.services.workspace import get_workspace
from app.tenant import set_session_tenant

logger = logging.getLogger("agentgraph.stream")
settings = get_settings()

router = APIRouter(prefix="/v1", tags=["Realtime"])
admin_router = APIRouter(
    prefix="/v1/admin", tags=["Realtime"], dependencies=[Depends(require_admin)]
)

ALLOWED_TABLES = {
    "context_slots",
    "collab_tasks",
    "shared_documents",
    "timeline_streams",
    "workspace_links",
    "document_proposals",
    "external_actions",
}


class StreamFilters:
    def __init__(
        self,
        tables: str | None,
        status: str | None,
        assigned_role: str | None,
        namespace: str | None,
    ) -> None:
        self.tables = _split(tables)
        if self.tables:
            unknown = self.tables - ALLOWED_TABLES
            if unknown:
                raise ApiError(
                    422,
                    "invalid_stream_filter",
                    f"unknown table filter: {', '.join(sorted(unknown))}",
                )
        self.statuses = _split(status, upper=True)
        self.assigned_roles = _split(assigned_role, upper=True)
        self.namespace = namespace


def _split(value: str | None, *, upper: bool = False) -> set[str] | None:
    if not value:
        return None
    items = {item.strip() for item in value.split(",") if item.strip()}
    if upper:
        items = {item.upper() for item in items}
    return items or None


async def get_stream_principal(request: Request) -> Principal:
    """Resolve auth with a short-lived session so SSE doesn't pin the pool."""
    raw_key = extract_bearer_token(request)
    if not raw_key:
        raise ApiError(401, "unauthorized", "missing bearer API key")
    async with async_session_factory() as session:
        principal = await resolve_principal(
            session, raw_key, agent_hint=request.headers.get("X-Agent-Signature")
        )
    if principal is None:
        raise ApiError(401, "unauthorized", "invalid or inactive API key")
    return principal


def _event_allowed(event: dict[str, Any], principal: Principal, filters: StreamFilters) -> bool:
    if str(event.get("workspace_id")) != str(principal.workspace.id):
        return False
    table = event.get("table", "")
    if principal.role is RoleScope.SCOUT and table in (
        "context_slots",
        "timeline_streams",
    ):
        return False
    if filters.tables and table not in filters.tables:
        return False
    if filters.statuses and (event.get("status") or "").upper() not in filters.statuses:
        return False
    if (
        filters.assigned_roles
        and (event.get("assigned_role") or "").upper() not in filters.assigned_roles
    ):
        return False
    if filters.namespace and event.get("namespace") != filters.namespace:
        return False
    if table in ("shared_documents", "timeline_streams"):
        allowed = principal.allowed_namespaces
        if not principal.is_commander and "*" not in allowed:
            if event.get("namespace") not in allowed:
                return False
    return True


def _orm_event(row: WorkspaceEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "table": row.source_table,
        "action": row.action,
        "workspace_id": str(row.workspace_id),
        "record_id": str(row.record_id) if row.record_id else None,
        "actor": row.actor,
        "version": row.version,
        "status": row.status,
        "slot_key": row.slot_key,
        "task_key": row.task_key,
        "doc_key": row.doc_key,
        "namespace": row.namespace,
        "assigned_role": row.assigned_role,
        "locked_by_human": row.locked_by_human,
        "event_type": domain_event_type(
            row.source_table, row.action, row.payload
        ),
        "timestamp": row.created_at.isoformat(),
    }


def _parse_last_event_id(request: Request, query_value: str | None) -> int | None:
    raw = request.headers.get("Last-Event-ID") or query_value
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiError(
            422, "invalid_last_event_id", "Last-Event-ID must be an integer"
        ) from exc
    if value < 0:
        raise ApiError(422, "invalid_last_event_id", "Last-Event-ID must be >= 0")
    return value


async def _replay(
    principal: Principal,
    filters: StreamFilters,
    last_event_id: int,
) -> list[dict[str, Any]]:
    async with async_session_factory() as session:
        set_session_tenant(session, principal.workspace.id)
        rows = (
            await session.execute(
                select(WorkspaceEvent)
                .where(
                    WorkspaceEvent.workspace_id == principal.workspace.id,
                    WorkspaceEvent.id > last_event_id,
                )
                .order_by(WorkspaceEvent.id.asc())
                .limit(settings.sse_replay_limit)
            )
        ).scalars().all()
    return [
        _orm_event(row)
        for row in rows
        if _event_allowed(_orm_event(row), principal, filters)
    ]


def _frame(event: dict[str, Any]) -> str:
    return (
        f"id: {event['id']}\n"
        "event: workspace\n"
        f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
    )


async def _event_stream(
    principal: Principal,
    filters: StreamFilters,
    last_event_id: int | None,
) -> AsyncIterator[str]:
    cursor = 0
    async with event_bus.subscribe() as queue:
        yield (
            "retry: 3000\n"
            "event: connected\n"
            f"data: {json.dumps({'workspace_id': str(principal.workspace.id)})}\n\n"
        )

        if last_event_id is not None:
            for event in await _replay(principal, filters, last_event_id):
                cursor = max(cursor, int(event["id"]))
                yield _frame(event)

        while True:
            try:
                payload = await asyncio.wait_for(
                    queue.get(), timeout=settings.sse_heartbeat_seconds
                )
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            try:
                event = json.loads(payload)
            except (TypeError, ValueError):
                continue
            event_id = int(event.get("id") or 0)
            if event_id <= cursor:
                continue
            if not _event_allowed(event, principal, filters):
                # Still advance the cursor; the client is not entitled to it.
                cursor = max(cursor, event_id)
                continue
            cursor = max(cursor, event_id)
            yield _frame(event)


def _streaming_response(
    principal: Principal,
    filters: StreamFilters,
    last_event_id: int | None,
) -> StreamingResponse:
    return StreamingResponse(
        _event_stream(principal, filters, last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/stream",
    summary="Replayable SSE workspace event stream (agent/commander token)",
)
async def stream(
    request: Request,
    tables: str | None = Query(default=None),
    status: str | None = Query(default=None),
    assigned_role: str | None = Query(default=None),
    namespace: str | None = Query(default=None),
    last_event_id: str | None = Query(default=None),
    principal: Principal = Depends(get_stream_principal),
) -> StreamingResponse:
    return _streaming_response(
        principal,
        StreamFilters(tables, status, assigned_role, namespace),
        _parse_last_event_id(request, last_event_id),
    )


@admin_router.get(
    "/workspaces/{workspace_id}/stream",
    summary="Replayable SSE workspace event stream (human console)",
)
async def admin_stream(
    request: Request,
    workspace_id: uuid.UUID,
    tables: str | None = Query(default=None),
    status: str | None = Query(default=None),
    assigned_role: str | None = Query(default=None),
    namespace: str | None = Query(default=None),
    last_event_id: str | None = Query(default=None),
) -> StreamingResponse:
    async with async_session_factory() as session:
        workspace = await get_workspace(session, workspace_id)
    return _streaming_response(
        principal_for_admin(workspace),
        StreamFilters(tables, status, assigned_role, namespace),
        _parse_last_event_id(request, last_event_id),
    )
