"""Typed links between tasks, documents, and slots."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, ensure_namespace, require_permission
from app.errors import ApiError, NotFound
from app.models import CollabTask, ContextSlot, LinkType, SharedDocument, WorkspaceLink
from app.schemas import LinkCreateRequest, LinkListResponse
from app.services.common import append_event, link_out

_MODELS = (CollabTask, ContextSlot, SharedDocument)


async def _node_namespace(
    session: AsyncSession, workspace_id: uuid.UUID, node_id: uuid.UUID
) -> str | None:
    for model in _MODELS:
        found = await session.scalar(
            select(model.namespace).where(
                model.workspace_id == workspace_id, model.id == node_id
            )
        )
        if found is not None:
            return str(found)
    return None


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
    namespaces: list[str] = []
    for node in (request.source_id, request.target_id):
        namespace = await _node_namespace(
            session, principal.workspace.id, node
        )
        if namespace is None:
            raise NotFound(
                "workspace_node",
                "source_id and target_id must reference a task, document or slot "
                "in this workspace",
            )
        ensure_namespace(principal, namespace, write=True)
        namespaces.append(namespace)
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
        # Cross-namespace links are visible only to commander/"*" timelines.
        namespace=namespaces[0] if namespaces[0] == namespaces[1] else "*",
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
    if not principal.is_commander and "*" not in principal.allowed_namespaces:
        allowed = set(principal.allowed_namespaces)
        node_ids = {
            node_id
            for link in links
            for node_id in (link.source_id, link.target_id)
        }
        scopes: dict[uuid.UUID, str] = {}
        for model in _MODELS:
            rows = (
                await session.execute(
                    select(model.id, model.namespace).where(
                        model.workspace_id == principal.workspace.id,
                        model.id.in_(node_ids),
                    )
                )
            ).all()
            scopes.update({row.id: row.namespace for row in rows})
        links = [
            link
            for link in links
            if scopes.get(link.source_id) in allowed
            and scopes.get(link.target_id) in allowed
        ]
    return LinkListResponse(items=[link_out(link) for link in links])
