"""Serialization, audit, events, and snapshots shared by the service layer."""

from __future__ import annotations

import json
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.blackboard import lease_is_active
from app.engine.formatter import render_document
from app.models import (
    AgentToken,
    CollabTask,
    ContextSlot,
    LinkType,
    SharedDocument,
    StateAuditDiff,
    TimelineStream,
    Workspace,
)
from app.schemas import (
    AgentTokenOut,
    DocumentOut,
    DocumentSummary,
    LinkOut,
    SlotOut,
    SlotSummary,
    TaskOut,
    TaskSummary,
    TimelineItemOut,
    WorkspaceInfo,
)

_EVENT_PLATFORM: ContextVar[str] = ContextVar("agentgraph_event_platform", default="api")


def set_event_platform(platform: str) -> None:
    _EVENT_PLATFORM.set(platform)




# Platform-written documents (created_by "system") in these namespaces are
# readable by every agent role: the constitution and namespace readmes, and
# the continuation packs a successor needs after a lease is lost.
PLATFORM_DOC_NAMESPACES = ("system", "handoff")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def db_now(session: AsyncSession) -> datetime:
    """Use the database clock for lease comparisons and expiry."""
    value = await session.scalar(select(func.now()))
    return value or utcnow()


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def workspace_info(workspace: Workspace) -> WorkspaceInfo:
    return WorkspaceInfo(
        id=workspace.id,
        canonical_id=workspace.canonical_id,
        name=workspace.name,
        tier=workspace.tier,
        created_at=workspace.created_at,
    )


def slot_out(slot: ContextSlot) -> SlotOut:
    return SlotOut(
        id=slot.id,
        canonical_id=slot.canonical_id,
        slot_key=slot.slot_key,
        goal=slot.goal,
        current_state=slot.current_state or {},
        schema_hint=slot.schema_hint or {},
        blockers=slot.blockers or [],
        namespace=slot.namespace,
        version=slot.version,
        lease_epoch=slot.lease_epoch,
        locked_by=slot.locked_by,
        lock_expires_at=slot.lock_expires_at,
        lease_active=lease_is_active(slot.locked_by, slot.lock_expires_at),
        locked_by_human=slot.locked_by_human,
        human_note=slot.human_note,
        tainted=slot.tainted,
        taint_sources=slot.taint_sources or [],
        updated_by=slot.updated_by,
        updated_at=slot.updated_at,
        updated_stamp=slot.updated_stamp,
    )


def slot_summary(slot: ContextSlot) -> SlotSummary:
    return SlotSummary(
        id=slot.id,
        canonical_id=slot.canonical_id,
        slot_key=slot.slot_key,
        namespace=slot.namespace,
        version=slot.version,
        lease_epoch=slot.lease_epoch,
        locked_by=slot.locked_by,
        lock_expires_at=slot.lock_expires_at,
        locked_by_human=slot.locked_by_human,
        tainted=slot.tainted,
        taint_sources=slot.taint_sources or [],
        updated_by=slot.updated_by,
        updated_at=slot.updated_at,
    )


def task_summary(task: CollabTask) -> TaskSummary:
    brief = task.handoff_brief if isinstance(task.handoff_brief, dict) else {}
    return TaskSummary(
        id=task.id,
        canonical_id=task.canonical_id,
        alias=task.task_key,
        task_key=task.task_key,
        title=task.title,
        status=task.status,
        priority=task.priority,
        assigned_role=task.assigned_role,
        namespace=task.namespace,
        claimed_by=task.claimed_by,
        lease_expires_at=task.lease_expires_at,
        lease_active=lease_is_active(task.lease_owner, task.lease_expires_at),
        handoff_summary=brief.get("summary") if brief else None,
        artifact_uri=task.artifact_uri,
        human_feedback=task.human_feedback,
        review_feedback=task.review_feedback,
        reviewed_by=task.reviewed_by,
        ping_pong_count=task.ping_pong_count,
        max_retries=task.max_retries,
        version=task.version,
        lease_epoch=task.lease_epoch,
        fencing_token=task.lease_epoch if task.lease_owner else None,
        assignee=task.assignee,
        orphaned_from=task.orphaned_from,
        orphan_count=task.orphan_count or 0,
        tainted=task.tainted,
        taint_sources=task.taint_sources or [],
        updated_at=task.updated_at,
        updated_stamp=task.updated_stamp,
    )


def task_out(task: CollabTask) -> TaskOut:
    summary = task_summary(task)
    return TaskOut(
        **summary.model_dump(),
        description=task.description,
        input_context=task.input_context or {},
        output_artifact=task.output_artifact or {},
        handoff_brief=task.handoff_brief,
        reject_history=task.reject_history or [],
        review_round=task.review_round,
        review_history=task.review_history or [],
        created_by=task.created_by,
        created_at=task.created_at,
    )


def _compact_frontmatter(value: dict[str, Any], limit: int = 8_192) -> dict[str, Any]:
    if len(json.dumps(value, ensure_ascii=False, default=str)) <= limit:
        return value
    return {
        "_truncated": True,
        "_bytes": len(json.dumps(value, ensure_ascii=False, default=str)),
        "_keys": list(value)[:50],
    }


def document_summary(
    document: SharedDocument, *, compact_frontmatter: bool = False
) -> DocumentSummary:
    frontmatter = document.frontmatter or {}
    if compact_frontmatter:
        frontmatter = _compact_frontmatter(frontmatter)
    return DocumentSummary(
        id=document.id,
        canonical_id=document.canonical_id,
        alias=document.doc_key,
        doc_key=document.doc_key,
        title=document.title,
        namespace=document.namespace,
        version=document.version,
        frontmatter=frontmatter,
        verification_status=document.verification_status,
        trust_level=document.trust_level,
        verified_version=document.verified_version,
        verified_hash=document.verified_hash,
        content_hash=document.content_hash,
        embedding_model=document.embedding_model,
        locked_by_human=document.locked_by_human,
        human_note=document.human_note,
        tainted=document.tainted,
        taint_sources=document.taint_sources or [],
        archived_at=document.archived_at,
        archived_by=document.archived_by,
        archive_reason=document.archive_reason,
        updated_by=document.updated_by,
        updated_at=document.updated_at,
        updated_stamp=document.updated_stamp,
        **getattr(document, "_index_status", {}),
    )


def document_out(
    document: SharedDocument,
    *,
    include_content: bool = True,
    content_chars: int | None = None,
) -> DocumentOut:
    summary = document_summary(document)
    return DocumentOut(
        **summary.model_dump(),
        content=document.content,
        created_by=document.created_by,
        created_at=document.created_at,
        created_stamp=document.created_stamp,
        agent_view=render_document(
            document,
            include_content=include_content,
            content_chars=content_chars,
        ),
    )


def timeline_out(event: TimelineStream) -> TimelineItemOut:
    return TimelineItemOut(
        id=event.id,
        agent_signature=event.agent_signature,
        platform=event.platform,
        event_type=event.event_type,
        payload=event.payload or {},
        namespace=event.namespace,
        token_cost=event.token_cost,
        latency_ms=event.latency_ms,
        created_at=event.created_at,
    )


def link_out(link: Any) -> LinkOut:
    return LinkOut(
        id=link.id,
        source_id=link.source_id,
        target_id=link.target_id,
        source_type=getattr(link, "source_type", None),
        target_type=getattr(link, "target_type", None),
        relation_type=LinkType(link.relation_type.value),
        confidence=link.confidence,
        metadata=link.metadata_ or {},
        created_at=link.created_at,
    )


def agent_token_out(token: AgentToken) -> AgentTokenOut:
    return AgentTokenOut(
        id=token.id,
        canonical_id=token.canonical_id,
        agent_signature=token.agent_signature,
        role_scope=token.role_scope.value,
        allowed_namespaces=list(token.allowed_namespaces or ["*"]),
        is_active=token.is_active,
        created_at=token.created_at,
        last_seen_at=token.last_seen_at,
        expires_at=token.expires_at,
        revoked_at=token.revoked_at,
        per_minute_limit=token.per_minute_limit,
        monthly_token_budget=token.monthly_token_budget,
    )


def slot_snapshot(slot: ContextSlot) -> dict[str, Any]:
    return {
        "goal": slot.goal,
        "current_state": slot.current_state or {},
        "schema_hint": slot.schema_hint or {},
        "blockers": slot.blockers or [],
        "namespace": slot.namespace,
        "version": slot.version,
        "lease_epoch": slot.lease_epoch,
        "locked_by": slot.locked_by,
        "lock_expires_at": iso(slot.lock_expires_at),
        "locked_by_human": slot.locked_by_human,
        "human_note": slot.human_note,
        "tainted": slot.tainted,
        "taint_sources": slot.taint_sources or [],
        "updated_by": slot.updated_by,
        "updated_at": iso(slot.updated_at),
    }


def document_snapshot(document: SharedDocument) -> dict[str, Any]:
    return {
        "title": document.title,
        "content": document.content,
        "frontmatter": document.frontmatter or {},
        "namespace": document.namespace,
        "version": document.version,
        "content_hash": document.content_hash,
        "verification_status": document.verification_status,
        "trust_level": document.trust_level,
        "verified_version": document.verified_version,
        "verified_hash": document.verified_hash,
        "embedding_model": document.embedding_model,
        "locked_by_human": document.locked_by_human,
        "human_note": document.human_note,
        "tainted": document.tainted,
        "taint_sources": document.taint_sources or [],
        "policy_flags": document.policy_flags or {},
        "archived_at": iso(document.archived_at) if document.archived_at else None,
        "archived_by": document.archived_by,
        "archive_reason": document.archive_reason,
        "updated_by": document.updated_by,
        "updated_at": iso(document.updated_at),
    }


def task_snapshot(task: CollabTask) -> dict[str, Any]:
    return {
        "title": task.title,
        "description": task.description,
        "status": task.status.value,
        "priority": task.priority,
        "assigned_role": task.assigned_role,
        "namespace": task.namespace,
        "claimed_by": task.claimed_by,
        "lease_owner": task.lease_owner,
        "lease_expires_at": iso(task.lease_expires_at),
        "input_context": task.input_context or {},
        "output_artifact": task.output_artifact or {},
        "handoff_brief": task.handoff_brief,
        "artifact_uri": task.artifact_uri,
        "artifact_version": task.artifact_version,
        "artifact_hash": task.artifact_hash,
        "handed_off_by": task.handed_off_by,
        "review_round": task.review_round,
        "review_history": task.review_history or [],
        "handoff_lease_expires_at": iso(task.handoff_lease_expires_at),
        "human_feedback": task.human_feedback,
        "review_feedback": task.review_feedback,
        "reviewed_by": task.reviewed_by,
        "reject_history": task.reject_history or [],
        "ping_pong_count": task.ping_pong_count,
        "max_retries": task.max_retries,
        "version": task.version,
        "lease_epoch": task.lease_epoch,
        "tainted": task.tainted,
        "taint_sources": task.taint_sources or [],
        "updated_at": iso(task.updated_at),
    }


_audit_actor: ContextVar[tuple[str | None, uuid.UUID | None, uuid.UUID | None]] = (
    ContextVar("agentgraph_audit_actor", default=(None, None, None))
)


def set_audit_actor_values(
    auth_kind: str | None,
    token_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
) -> None:
    _audit_actor.set((auth_kind, token_id, user_id))


def set_audit_actor(principal: Any) -> None:
    set_audit_actor_values(
        getattr(principal.auth_kind, "value", None),
        getattr(principal, "token_id", None),
        getattr(principal, "user_id", None),
    )


async def record_audit(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    session_batch_id: uuid.UUID,
    agent_signature: str,
    target_table: str,
    target_record_id: uuid.UUID,
    operation: str,
    before: dict[str, Any],
    after: dict[str, Any],
) -> StateAuditDiff:
    auth_kind, token_id, user_id = _audit_actor.get()
    entry = StateAuditDiff(
        workspace_id=workspace_id,
        session_batch_id=session_batch_id,
        agent_signature=agent_signature,
        actor_auth_kind=auth_kind,
        actor_token_id=token_id,
        actor_user_id=user_id,
        target_table=target_table,
        target_record_id=target_record_id,
        operation=operation,
        before_diff=before,
        after_diff=after,
        created_at=utcnow(),
    )
    session.add(entry)
    await session.flush()
    return entry


async def append_event(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    agent: str,
    event_type: str,
    payload: dict[str, Any],
    namespace: str = "default",
    platform: str | None = None,
    token_cost: int = 0,
    latency_ms: int = 0,
) -> TimelineStream:
    platform = platform or _EVENT_PLATFORM.get()
    event = TimelineStream(
        workspace_id=workspace_id,
        agent_signature=agent,
        platform=platform,
        event_type=event_type,
        payload=payload,
        namespace=namespace,
        token_cost=token_cost,
        latency_ms=latency_ms,
        created_at=utcnow(),
    )
    session.add(event)
    await session.flush()
    return event


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in vector) + "]"


def parse_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


_DOMAIN_EVENTS = {
    ("shared_documents", "INSERT"): "DOC_CREATED",
    ("shared_documents", "UPDATE"): "DOC_UPDATED",
    ("collab_tasks", "INSERT"): "TASK_CREATED",
    ("collab_tasks", "UPDATE"): "TASK_UPDATED",
    ("context_slots", "INSERT"): "SLOT_CREATED",
    ("context_slots", "UPDATE"): "SLOT_UPDATED",
    ("workspace_links", "INSERT"): "LINK_CREATED",
    ("document_proposals", "INSERT"): "PROPOSAL_CREATED",
    ("document_proposals", "UPDATE"): "PROPOSAL_UPDATED",
    ("external_actions", "INSERT"): "EXTERNAL_ACTION_CREATED",
    ("external_actions", "UPDATE"): "EXTERNAL_ACTION_UPDATED",
}


def domain_event_type(table: str | None, action: str | None, payload: Any = None) -> str:
    if isinstance(payload, dict) and payload.get("event_type"):
        return str(payload["event_type"]).upper()
    return _DOMAIN_EVENTS.get(
        ((table or ""), (action or "").upper()),
        f"{table or 'unknown'}.{(action or 'unknown').upper()}",
    )


def event_identity(event_id: int) -> dict[str, Any]:
    """Protocol identity of an outbox event (PRD AG-ST-18 point 2).

    ``event_id`` is globally unique and ``seq`` never reused, so consumers of
    at-least-once delivery deduplicate by ``event_id`` and resume by ``seq``.
    """
    return {"event_id": f"evt_{int(event_id)}", "seq": int(event_id)}

