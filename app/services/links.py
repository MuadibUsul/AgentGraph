"""Typed links between tasks, documents, and slots."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_permission
from app.errors import ApiError, NotFound
from app.models import CollabTask, ContextSlot, LinkType, SharedDocument, WorkspaceLink
from app.schemas import LinkCreateRequest, LinkListResponse
from app.services.common import append_event, link_out

_MODELS = (CollabTask, ContextSlot, SharedDocument)


async def _node_exists(
    session: AsyncSession, workspace_id: uuid.UUID, node_id: uuid.UUID
) -> bool:
    for model in _MODELS:
        found = await session.scalar(
            select(model.id).where(
                model.workspace_id == workspace_id, model.id == node_id
            )
        )
        if found is not None:
            return True
    return False


async def _would_create_dependency_cycle(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
) -> bool:
    found = await session.scalar(
        text(
            """
WITH RECURSIVE reachable AS (
    SELECT target_id
    FROM workspace_links
    WHERE workspace_id = CAST(:workspace_id AS uuid)
      AND source_id = CAST(:target_id AS uuid)
      AND relation_type = 'depends_on'
    UNION
    SELECT l.target_id
    FROM workspace_links l
    JOIN reachable r ON l.source_id = r.target_id
    WHERE l.workspace_id = CAST(:workspace_id AS uuid)
      AND l.relation_type = 'depends_on'
)
SELECT 1 FROM reachable
WHERE target_id = CAST(:source_id AS uuid)
LIMIT 1
"""
        ),
        {
            "workspace_id": str(workspace_id),
            "source_id": str(source_id),
            "target_id": str(target_id),
        },
    )
    return found is not None


async def add_link(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    relation_type: Any,
    confidence: float = 1.0,
    metadata: dict[str, Any] | None = None,
) -> WorkspaceLink:
    link = WorkspaceLink(
        workspace_id=workspace_id,
        source_id=source_id,
        target_id=target_id,
        relation_type=relation_type,
        confidence=confidence,
        metadata_=metadata or {},
    )
    session.add(link)
    await session.flush()
    await session.refresh(link)
    return link


async def create_link(
    session: AsyncSession, principal: Principal, request: LinkCreateRequest
) -> WorkspaceLink:
    require_permission(principal, "link:write")
    for node in (request.source_id, request.target_id):
        if not await _node_exists(session, principal.workspace.id, node):
            raise NotFound(
                "workspace_node",
                "source_id and target_id must reference a task, document or slot "
                "in this workspace",
            )
    if request.relation_type == LinkType.DEPENDS_ON and await _would_create_dependency_cycle(
        session,
        principal.workspace.id,
        request.source_id,
        request.target_id,
    ):
        raise ApiError(
            409,
            "dependency_cycle",
            "this depends_on link would create a dependency cycle",
            details={
                "source_id": str(request.source_id),
                "target_id": str(request.target_id),
            },
        )
    link = WorkspaceLink(
        workspace_id=principal.workspace.id,
        source_id=request.source_id,
        target_id=request.target_id,
        relation_type=request.relation_type,
        confidence=request.confidence,
        metadata_=request.metadata,
    )
    session.add(link)
    try:
        await session.flush()
        await session.refresh(link)
    except IntegrityError:
        await session.rollback()
        raise ApiError(409, "link_exists", "this workspace link already exists") from None
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=principal.agent_signature,
        event_type="LINK_CREATED",
        payload={
            "source_id": str(request.source_id),
            "target_id": str(request.target_id),
            "relation_type": request.relation_type.value,
        },
    )
    await session.commit()
    return link


async def list_links(
    session: AsyncSession,
    principal: Principal,
    *,
    node_id: uuid.UUID | None = None,
    limit: int = 100,
) -> LinkListResponse:
    require_permission(principal, "link:read")
    query = select(WorkspaceLink).where(
        WorkspaceLink.workspace_id == principal.workspace.id
    )
    if node_id is not None:
        query = query.where(
            or_(
                WorkspaceLink.source_id == node_id,
                WorkspaceLink.target_id == node_id,
            )
        )
    links = (
        await session.execute(
            query.order_by(WorkspaceLink.created_at.desc()).limit(min(limit, 500))
        )
    ).scalars().all()
    return LinkListResponse(items=[link_out(link) for link in links])
