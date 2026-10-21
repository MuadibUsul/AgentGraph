"""Workspace provisioning, L0 map, stats, and scoped agent tokens."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    PERMISSIONS,
    Principal,
    can,
    generate_agent_key,
    generate_api_key,
    hash_api_key,
    require_permission,
)
from app.config import get_settings
from app.engine.embeddings import configured_embedding_model
from app.errors import ApiError, NotFound
from app.models import (
    AgentRole,
    AgentToken,
    CollabTask,
    ContextSlot,
    SharedDocument,
    TaskStatus,
    Workspace,
)
from app.schemas import (
    AdminWorkspaceOut,
    AgentTokenCreatedResponse,
    AgentTokenCreateRequest,
    AgentTokenListResponse,
    AgentTokenUpdateRequest,
    QuotaUsage,
    WhoAmIResponse,
    WorkspaceCreatedResponse,
    WorkspaceInfo,
    WorkspaceMapResponse,
    WorkspaceOutput,
    WorkspaceStatsResponse,
)
from app.services.common import (
    agent_token_out,
    document_summary,
    slot_summary,
    task_summary,
    workspace_info,
)

settings = get_settings()


async def whoami(principal: Principal) -> WhoAmIResponse:
    return WhoAmIResponse(
        workspace=workspace_info(principal.workspace),
        role=principal.role.value,
        agent_signature=principal.agent_signature,
        auth_kind=principal.auth_kind.value,
        allowed_namespaces=principal.allowed_namespaces,
        permissions=sorted(PERMISSIONS.get(principal.role, set())),
        default_lease_seconds=settings.default_task_lease_seconds,
        embedding_model=configured_embedding_model(),
        embedding_dimensions=settings.embedding_dimensions,
        version=settings.app_version,
        human_role=principal.human_role,
    )


async def workspace_map(
    session: AsyncSession, principal: Principal
) -> WorkspaceMapResponse:
    require_permission(principal, "workspace:read")
    workspace_id = principal.workspace.id
    docs_query = select(SharedDocument).where(
        SharedDocument.workspace_id == workspace_id
    )
    doc_count_query = select(func.count(SharedDocument.id)).where(
        SharedDocument.workspace_id == workspace_id
    )
    if not principal.is_commander and "*" not in principal.allowed_namespaces:
        docs_query = docs_query.where(
            SharedDocument.namespace.in_(principal.allowed_namespaces)
        )
        doc_count_query = doc_count_query.where(
            SharedDocument.namespace.in_(principal.allowed_namespaces)
        )
    documents = (
        await session.execute(
            docs_query.order_by(SharedDocument.updated_at.desc()).limit(
                settings.max_workspace_map_items
            )
        )
    ).scalars().all()
    document_count = int(await session.scalar(doc_count_query) or 0)

    tasks = (
        await session.execute(
            select(CollabTask)
            .where(CollabTask.workspace_id == workspace_id)
            .order_by(CollabTask.priority.desc(), CollabTask.updated_at.desc())
            .limit(settings.max_workspace_map_items)
        )
    ).scalars().all()
    task_count = int(
        await session.scalar(
            select(func.count(CollabTask.id)).where(
                CollabTask.workspace_id == workspace_id
            )
        )
        or 0
    )

    slots = []
    slot_count = 0
    if can(principal, "state:read"):
        slot_query = select(ContextSlot).where(
            ContextSlot.workspace_id == workspace_id
        )
        slot_count_query = select(func.count(ContextSlot.id)).where(
            ContextSlot.workspace_id == workspace_id
        )
        if not principal.is_commander and "*" not in principal.allowed_namespaces:
            slot_query = slot_query.where(
                ContextSlot.namespace.in_(principal.allowed_namespaces)
            )
            slot_count_query = slot_count_query.where(
                ContextSlot.namespace.in_(principal.allowed_namespaces)
            )
        slots = (
            await session.execute(
                slot_query.order_by(ContextSlot.updated_at.desc()).limit(
                    settings.max_workspace_map_items
                )
            )
        ).scalars().all()
        slot_count = int(await session.scalar(slot_count_query) or 0)

    counts = {
        "documents": document_count,
        "tasks": task_count,
        "slots": slot_count,
    }
    return WorkspaceMapResponse(
        workspace=workspace_info(principal.workspace),
        role=principal.role.value,
        counts=counts,
        task_statuses=[status.value for status in TaskStatus],
        roles=[role.value for role in AgentRole],
        tasks=[task_summary(task) for task in tasks],
        slots=[slot_summary(slot) for slot in slots],
        documents=[document_summary(document) for document in documents],
        outputs=[
            WorkspaceOutput(
                task_key=task.task_key,
                title=task.title,
                status=task.status.value,
                assigned_role=task.assigned_role,
                artifact_uri=task.artifact_uri,
                handoff_summary=(task.handoff_brief or {}).get("summary")
                if isinstance(task.handoff_brief, dict)
                else None,
                reviewed_by=task.reviewed_by,
            )
            for task in tasks
            if task.status in (TaskStatus.DONE, TaskStatus.FAILED)
        ],
    )


async def workspace_stats(
    session: AsyncSession, workspace: Workspace
) -> WorkspaceStatsResponse:
    counts = (
        await session.execute(
            text(
                """
SELECT
    (SELECT count(*) FROM shared_documents
        WHERE workspace_id = CAST(:workspace_id AS uuid)) AS documents,
    (SELECT count(*) FROM context_slots
        WHERE workspace_id = CAST(:workspace_id AS uuid)) AS slots,
    (SELECT count(*) FROM collab_tasks
        WHERE workspace_id = CAST(:workspace_id AS uuid)) AS tasks,
    (SELECT count(*) FROM agent_tokens
        WHERE workspace_id = CAST(:workspace_id AS uuid)) AS agent_tokens,
    (SELECT count(*) FROM timeline_streams
        WHERE workspace_id = CAST(:workspace_id AS uuid)) AS timeline_events,
    (SELECT count(*) FROM state_audit_diffs
        WHERE workspace_id = CAST(:workspace_id AS uuid)) AS audit_entries,
    (SELECT count(*) FROM collab_tasks
        WHERE workspace_id = CAST(:workspace_id AS uuid)
          AND status = 'ESCALATED_TO_HUMAN') AS escalated_tasks,
    (SELECT COALESCE(SUM(token_cost), 0) FROM timeline_streams
        WHERE workspace_id = CAST(:workspace_id AS uuid)) AS tokens_logged,
    (SELECT COALESCE(AVG(NULLIF(latency_ms, 0)), 0) FROM timeline_streams
        WHERE workspace_id = CAST(:workspace_id AS uuid)) AS avg_latency_ms,
    (SELECT count(*) FROM document_proposals
        WHERE workspace_id = CAST(:workspace_id AS uuid)
          AND status = 'PENDING') AS pending_proposals,
    (SELECT count(*) FROM external_actions
        WHERE workspace_id = CAST(:workspace_id AS uuid)
          AND status IN ('PENDING_APPROVAL', 'APPROVED', 'EXECUTING'))
        AS pending_external_actions,
    (SELECT count(*) FROM webhook_rules
        WHERE workspace_id = CAST(:workspace_id AS uuid)
          AND active) AS webhook_rules,
    (SELECT count(*) FROM shared_documents
        WHERE workspace_id = CAST(:workspace_id AS uuid)
          AND tainted) AS tainted_documents,
    (SELECT COALESCE(SUM(amount), 0) FROM usage_ledger
        WHERE workspace_id = CAST(:workspace_id AS uuid)
          AND created_at >= NOW() - INTERVAL '30 days') AS tokens_used_30d,
    (SELECT MAX(created_at) FROM timeline_streams
        WHERE workspace_id = CAST(:workspace_id AS uuid)) AS last_activity_at
"""
            ),
            {"workspace_id": str(workspace.id)},
        )
    ).mappings().one()
    doc_limit, slot_limit = settings.tier_limits(workspace.tier)
    documents = int(counts["documents"])
    slots = int(counts["slots"])
    return WorkspaceStatsResponse(
        workspace=workspace_info(workspace),
        documents=documents,
        slots=slots,
        tasks=int(counts["tasks"]),
        agent_tokens=int(counts["agent_tokens"]),
        timeline_events=int(counts["timeline_events"]),
        audit_entries=int(counts["audit_entries"]),
        escalated_tasks=int(counts["escalated_tasks"]),
        tokens_logged=int(counts["tokens_logged"]),
        avg_latency_ms=round(float(counts["avg_latency_ms"]), 2),
        last_activity_at=counts["last_activity_at"],
        document_quota=QuotaUsage(
            used=documents,
            limit=doc_limit,
            percentage=round(documents / doc_limit * 100, 2),
        ),
        slot_quota=QuotaUsage(
            used=slots,
            limit=slot_limit,
            percentage=round(slots / slot_limit * 100, 2),
        ),
        embedding_provider=settings.embedding_provider,
        embedding_model=configured_embedding_model(),
        embedding_dimensions=settings.embedding_dimensions,
        pending_proposals=int(counts["pending_proposals"]),
        pending_external_actions=int(counts["pending_external_actions"]),
        webhook_rules=int(counts["webhook_rules"]),
        tainted_documents=int(counts["tainted_documents"]),
        tokens_used_30d=int(counts["tokens_used_30d"]),
    )


async def create_workspace(
    session: AsyncSession, *, name: str, tier: str
) -> WorkspaceCreatedResponse:
    raw_key = generate_api_key()
    workspace = Workspace(
        name=name,
        api_key_hash=hash_api_key(raw_key),
        tier=tier,
    )
    session.add(workspace)
    await session.commit()
    return WorkspaceCreatedResponse(
        workspace=workspace_info(workspace), api_key=raw_key
    )


async def rotate_workspace_key(
    session: AsyncSession, workspace_id: uuid.UUID
) -> tuple[WorkspaceInfo, str]:
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise NotFound("workspace")
    raw_key = generate_api_key()
    workspace.api_key_hash = hash_api_key(raw_key)
    await session.commit()
    return workspace_info(workspace), raw_key


async def update_workspace(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    name: str | None,
    tier: str | None,
) -> WorkspaceInfo:
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise NotFound("workspace")
    if name is not None:
        workspace.name = name
    if tier is not None:
        workspace.tier = tier
    await session.commit()
    return workspace_info(workspace)


async def get_workspace(
    session: AsyncSession, workspace_id: uuid.UUID
) -> Workspace:
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise NotFound("workspace")
    return workspace


async def list_workspaces(session: AsyncSession) -> list[AdminWorkspaceOut]:
    rows = (
        await session.execute(
            text(
                """
SELECT
    w.id, w.name, w.tier, w.created_at,
    (SELECT count(*) FROM shared_documents d
        WHERE d.workspace_id = w.id) AS documents,
    (SELECT count(*) FROM context_slots s
        WHERE s.workspace_id = w.id) AS slots,
    (SELECT count(*) FROM collab_tasks t
        WHERE t.workspace_id = w.id) AS tasks,
    (SELECT count(*) FROM timeline_streams tl
        WHERE tl.workspace_id = w.id) AS timeline_events,
    (SELECT MAX(created_at) FROM timeline_streams tl
        WHERE tl.workspace_id = w.id) AS last_activity_at
FROM workspaces w
ORDER BY w.created_at DESC
"""
            )
        )
    ).mappings().all()
    return [
        AdminWorkspaceOut(
            id=row["id"],
            name=row["name"],
            tier=row["tier"],
            created_at=row["created_at"],
            documents=int(row["documents"]),
            slots=int(row["slots"]),
            tasks=int(row["tasks"]),
            timeline_events=int(row["timeline_events"]),
            last_activity_at=row["last_activity_at"],
        )
        for row in rows
    ]


async def create_agent_token(
    session: AsyncSession,
    workspace: Workspace,
    request: AgentTokenCreateRequest,
) -> AgentTokenCreatedResponse:
    existing = await session.scalar(
        select(AgentToken.id).where(
            AgentToken.workspace_id == workspace.id,
            AgentToken.agent_signature == request.agent_signature,
        )
    )
    if existing is not None:
        raise ApiError(
            409,
            "agent_token_exists",
            f"agent '{request.agent_signature}' already has a token",
        )
    raw_key = generate_agent_key()
    token = AgentToken(
        workspace_id=workspace.id,
        agent_signature=request.agent_signature,
        api_key_hash=hash_api_key(raw_key),
        role_scope=AgentRole(request.role_scope),
        allowed_namespaces=request.allowed_namespaces,
        is_active=request.is_active,
        expires_at=(
            datetime.now(timezone.utc) + timedelta(days=request.expires_in_days)
            if request.expires_in_days
            else None
        ),
        per_minute_limit=request.per_minute_limit,
        monthly_token_budget=request.monthly_token_budget,
    )
    session.add(token)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise ApiError(409, "agent_token_exists", "agent token already exists") from None
    await session.commit()
    return AgentTokenCreatedResponse(token=agent_token_out(token), api_key=raw_key)


async def list_agent_tokens(
    session: AsyncSession, workspace: Workspace
) -> AgentTokenListResponse:
    tokens = (
        await session.execute(
            select(AgentToken)
            .where(AgentToken.workspace_id == workspace.id)
            .order_by(AgentToken.created_at.desc())
        )
    ).scalars().all()
    return AgentTokenListResponse(items=[agent_token_out(token) for token in tokens])


async def rotate_agent_token(
    session: AsyncSession,
    workspace: Workspace,
    token_id: uuid.UUID,
) -> AgentTokenCreatedResponse:
    """Issue a new secret for an existing agent identity and invalidate the old one."""
    token = await session.get(AgentToken, token_id)
    if token is None or token.workspace_id != workspace.id:
        raise NotFound("agent_token")
    raw_key = generate_agent_key()
    token.api_key_hash = hash_api_key(raw_key)
    token.is_active = True
    token.revoked_at = None
    await session.commit()
    return AgentTokenCreatedResponse(token=agent_token_out(token), api_key=raw_key)


async def update_agent_token(
    session: AsyncSession,
    workspace: Workspace,
    token_id: uuid.UUID,
    request: AgentTokenUpdateRequest,
) -> AgentToken:
    token = await session.get(AgentToken, token_id)
    if token is None or token.workspace_id != workspace.id:
        raise NotFound("agent_token")
    if request.role_scope is not None:
        token.role_scope = AgentRole(request.role_scope)
    if request.allowed_namespaces is not None:
        token.allowed_namespaces = request.allowed_namespaces
    if request.is_active is not None:
        token.is_active = request.is_active
        token.revoked_at = None if request.is_active else datetime.now(timezone.utc)
    if request.expires_in_days is not None:
        token.expires_at = datetime.now(timezone.utc) + timedelta(
            days=request.expires_in_days
        )
        token.revoked_at = None
    if request.per_minute_limit is not None:
        token.per_minute_limit = request.per_minute_limit
    if request.monthly_token_budget is not None:
        token.monthly_token_budget = request.monthly_token_budget
    await session.commit()
    return token

async def delete_workspace(
    session: AsyncSession, workspace_id: uuid.UUID, *, confirm_name: str
) -> None:
    """Irreversibly delete a workspace and all tenant-owned rows.

    The exact workspace name must be supplied as confirmation. This is intended
    for disposable test workspaces, not routine production cleanup.
    """
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise NotFound("workspace")
    if confirm_name != workspace.name:
        raise ApiError(
            409,
            "workspace_confirmation_mismatch",
            "confirm must exactly match the workspace name",
            details={"workspace": workspace.name},
        )
    await session.delete(workspace)
    await session.commit()
