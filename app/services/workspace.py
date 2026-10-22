"""Workspace provisioning, L0 map, stats, and scoped agent tokens."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

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
    TimelineStream,
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
    PLATFORM_DOC_NAMESPACES,
    agent_token_out,
    document_summary,
    slot_summary,
    task_summary,
    utcnow,
    workspace_info,
)
from app.services.lineage import snapshot_token
from app.stamp_context import current_stamp

settings = get_settings()


async def whoami(principal: Principal) -> WhoAmIResponse:
    from app.services.tenants import tenant_info

    return WhoAmIResponse(
        workspace=workspace_info(principal.workspace),
        tenant=tenant_info(principal.tenant) if principal.tenant else None,
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


def _encode_sync_cursor(value: datetime) -> str:
    raw = json.dumps({"updated_at": value.isoformat()}, separators=(",", ":")).encode(
        "utf-8"
    )
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_sync_cursor(cursor: str) -> datetime:
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        )
        value = datetime.fromisoformat(payload["updated_at"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ApiError(400, "invalid_cursor", "workspace cursor is invalid") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _document_visibility(
    principal: Principal,
    *,
    include_unverified: bool,
    include_tainted: bool,
) -> list[Any]:
    if principal.is_commander:
        return []
    conditions: list[Any] = []
    if not include_unverified and not can(principal, "doc:verify"):
        conditions.append(
            (SharedDocument.verification_status == "VERIFIED")
            | (SharedDocument.created_by == principal.agent_signature)
            | (
                SharedDocument.namespace.in_(PLATFORM_DOC_NAMESPACES)
                & (SharedDocument.created_by == "system")
            )
        )
    if not include_tainted:
        conditions.append(
            SharedDocument.tainted.is_(False)
            | (SharedDocument.created_by == principal.agent_signature)
            | (
                SharedDocument.namespace.in_(PLATFORM_DOC_NAMESPACES)
                & (SharedDocument.created_by == "system")
            )
        )
    return conditions


async def workspace_map(
    session: AsyncSession,
    principal: Principal,
    *,
    since: str | None = None,
    include_unverified: bool = False,
    include_tainted: bool = False,
) -> WorkspaceMapResponse:
    require_permission(principal, "workspace:read")
    workspace_id = principal.workspace.id
    cursor_time = min(_decode_sync_cursor(since), utcnow()) if since else None
    allowed = (
        principal.allowed_namespaces
        if not principal.is_commander and "*" not in principal.allowed_namespaces
        else None
    )

    def scoped(query: Any, namespace_column: Any) -> Any:
        if allowed is not None:
            query = query.where(namespace_column.in_(allowed))
        return query

    docs_query = scoped(
        select(SharedDocument).where(SharedDocument.workspace_id == workspace_id),
        SharedDocument.namespace,
    )
    doc_count_query = scoped(
        select(func.count(SharedDocument.id)).where(
            SharedDocument.workspace_id == workspace_id,
            SharedDocument.archived_at.is_(None),
        ),
        SharedDocument.namespace,
    )
    doc_namespace_query = scoped(
        select(SharedDocument.namespace, func.count(SharedDocument.id))
        .where(
            SharedDocument.workspace_id == workspace_id,
            SharedDocument.archived_at.is_(None),
        )
        .group_by(SharedDocument.namespace),
        SharedDocument.namespace,
    )
    if cursor_time is None:
        docs_query = docs_query.where(SharedDocument.archived_at.is_(None))
    else:
        docs_query = docs_query.where(SharedDocument.updated_at > cursor_time)
    visibility = _document_visibility(
        principal,
        include_unverified=include_unverified,
        include_tainted=include_tainted,
    )
    for condition in visibility:
        docs_query = docs_query.where(condition)
        doc_count_query = doc_count_query.where(condition)
        doc_namespace_query = doc_namespace_query.where(condition)
    documents = (
        await session.execute(
            docs_query.order_by(SharedDocument.updated_at.desc()).limit(
                settings.max_workspace_map_items
            )
        )
    ).scalars().all()
    document_count = int(await session.scalar(doc_count_query) or 0)
    document_namespaces = {
        namespace: int(count)
        for namespace, count in (await session.execute(doc_namespace_query)).all()
    }

    tasks_query = scoped(
        select(CollabTask).where(CollabTask.workspace_id == workspace_id),
        CollabTask.namespace,
    )
    task_count_query = scoped(
        select(func.count(CollabTask.id)).where(
            CollabTask.workspace_id == workspace_id
        ),
        CollabTask.namespace,
    )
    task_namespace_query = scoped(
        select(CollabTask.namespace, func.count(CollabTask.id))
        .where(CollabTask.workspace_id == workspace_id)
        .group_by(CollabTask.namespace),
        CollabTask.namespace,
    )
    if cursor_time is not None:
        tasks_query = tasks_query.where(CollabTask.updated_at > cursor_time)
    tasks = (
        await session.execute(
            tasks_query.order_by(
                CollabTask.priority.desc(), CollabTask.updated_at.desc()
            ).limit(settings.max_workspace_map_items)
        )
    ).scalars().all()
    task_count = int(await session.scalar(task_count_query) or 0)
    task_namespaces = {
        namespace: int(count)
        for namespace, count in (await session.execute(task_namespace_query)).all()
    }

    slots = []
    slot_count = 0
    slot_namespaces: dict[str, int] = {}
    timeline = []
    if can(principal, "state:read"):
        slot_query = scoped(
            select(ContextSlot).where(ContextSlot.workspace_id == workspace_id),
            ContextSlot.namespace,
        )
        slot_count_query = scoped(
            select(func.count(ContextSlot.id)).where(
                ContextSlot.workspace_id == workspace_id
            ),
            ContextSlot.namespace,
        )
        slot_namespace_query = scoped(
            select(ContextSlot.namespace, func.count(ContextSlot.id))
            .where(ContextSlot.workspace_id == workspace_id)
            .group_by(ContextSlot.namespace),
            ContextSlot.namespace,
        )
        if cursor_time is not None:
            slot_query = slot_query.where(ContextSlot.updated_at > cursor_time)
        slots = (
            await session.execute(
                slot_query.order_by(ContextSlot.updated_at.desc()).limit(
                    settings.max_workspace_map_items
                )
            )
        ).scalars().all()
        slot_count = int(await session.scalar(slot_count_query) or 0)
        slot_namespaces = {
            namespace: int(count)
            for namespace, count in (await session.execute(slot_namespace_query)).all()
        }
        timeline_query = scoped(
            select(TimelineStream).where(
                TimelineStream.workspace_id == workspace_id
            ),
            TimelineStream.namespace,
        )
        if cursor_time is not None:
            timeline_query = timeline_query.where(
                TimelineStream.created_at > cursor_time
            )
        events = (
            await session.execute(
                timeline_query.order_by(TimelineStream.created_at.desc()).limit(
                    settings.max_workspace_map_items
                )
            )
        ).scalars().all()
        timeline = [
            {
                "id": str(event.id),
                "event_type": event.event_type,
                "agent_signature": event.agent_signature,
                "namespace": event.namespace,
                "payload": event.payload or {},
                "token_cost": event.token_cost,
                "latency_ms": event.latency_ms,
                "created_at": event.created_at.isoformat(),
            }
            for event in events
        ]

    counts = {
        "documents": document_count,
        "tasks": task_count,
        "slots": slot_count,
    }
    timestamps = [item.updated_at for item in documents]
    timestamps.extend(item.updated_at for item in tasks)
    timestamps.extend(item.updated_at for item in slots)
    timestamps.extend(
        datetime.fromisoformat(item["created_at"]) for item in timeline
    )
    cursor_point = max(timestamps) if timestamps else cursor_time
    return WorkspaceMapResponse(
        workspace=workspace_info(principal.workspace),
        role=principal.role.value,
        counts=counts,
        task_statuses=[status.value for status in TaskStatus],
        roles=[role.value for role in AgentRole],
        tasks=[task_summary(task) for task in tasks],
        slots=[slot_summary(slot) for slot in slots],
        documents=[
            document_summary(document, compact_frontmatter=True)
            for document in documents
        ],
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
        namespace_counts={
            "documents": document_namespaces,
            "tasks": task_namespaces,
            "slots": slot_namespaces,
        },
        incremental=cursor_time is not None,
        since=since,
        next_cursor=_encode_sync_cursor(cursor_point or utcnow()),
        timeline=timeline,
    )


async def workspace_stats(
    session: AsyncSession,
    workspace: Workspace,
    *,
    allowed_namespaces: list[str] | None = None,
) -> WorkspaceStatsResponse:
    counts = (
        await session.execute(
            text(
                """
SELECT
    (SELECT count(*) FROM shared_documents
        WHERE workspace_id = CAST(:workspace_id AS uuid)
          AND archived_at IS NULL) AS documents,
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
    namespace_rows = (
        await session.execute(
            select(SharedDocument.namespace, func.count(SharedDocument.id))
            .where(
                SharedDocument.workspace_id == workspace.id,
                SharedDocument.archived_at.is_(None),
            )
            .group_by(SharedDocument.namespace)
        )
    ).all()
    document_namespaces = {
        namespace: int(count)
        for namespace, count in namespace_rows
        # Namespace-scoped tokens must not learn other namespaces' names.
        if allowed_namespaces is None or namespace in allowed_namespaces
    }
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
        document_namespaces=document_namespaces,
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
    await session.flush()
    from app.services.onboarding import scaffold_new_workspace

    await scaffold_new_workspace(session, workspace.id)
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
        WHERE d.workspace_id = w.id
          AND d.archived_at IS NULL) AS documents,
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
        # Branch tokens hang off the workspace root token (PRD AG-ID-03).
        parent_token_id=workspace.root_token_id,
        root_token_id=workspace.root_token_id,
        depth=1,
        issued_by_agent_id=(current_stamp() or {}).get("principal_id") or "admin",
        purpose=request.purpose,
    )
    session.add(token)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise ApiError(409, "agent_token_exists", "agent token already exists") from None
    await snapshot_token(session, workspace, token)
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
    if request.agent_signature is not None:
        duplicate = await session.scalar(
            select(AgentToken.id).where(
                AgentToken.workspace_id == workspace.id,
                AgentToken.agent_signature == request.agent_signature,
                AgentToken.id != token.id,
            )
        )
        if duplicate is not None:
            raise ApiError(
                409,
                "agent_token_exists",
                f"agent '{request.agent_signature}' already has a token",
            )
        token.agent_signature = request.agent_signature
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
    grant_changed = any(
        value is not None
        for value in (
            request.agent_signature,
            request.role_scope,
            request.allowed_namespaces,
            request.expires_in_days,
            request.per_minute_limit,
            request.monthly_token_budget,
        )
    )
    if grant_changed:
        # A changed grant is a new, immutable snapshot; old stamps keep theirs.
        await session.flush()
        await snapshot_token(session, workspace, token)
    if request.is_active is False:
        # A revoked token's leases end now, not when they would have timed out.
        from app.services.leases import revoke_holder

        await session.flush()
        await revoke_holder(session, workspace.id, token, reason="holder_revoked")
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
