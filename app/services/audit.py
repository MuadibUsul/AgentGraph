"""Session-batch audit queries and time-travel undo."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.embeddings import get_embedding_provider
from app.errors import ApiError, Forbidden, NotFound, UndoConflict
from app.models import (
    CollabTask,
    ContextSlot,
    SharedDocument,
    StateAuditDiff,
    TaskStatus,
    Workspace,
    WorkspaceLink,
)
from app.schemas import AuditBatchOut, AuditDiffOut, AuditListResponse, BatchUndoResponse
from app.services.common import (
    append_event,
    document_snapshot,
    record_audit,
    slot_snapshot,
    task_snapshot,
    utcnow,
)
from app.services.documents import _record_version
from app.services.external_actions import schedule_compensations_for_batch
from app.services.retrieval import reindex_document_chunks
from app.services.taint import (
    propagate_from_document,
    refresh_document_taint,
    refresh_slot_taint,
    refresh_task_taint,
)

_TABLES = {
    "context_slots": ContextSlot,
    "shared_documents": SharedDocument,
    "collab_tasks": CollabTask,
}

_SLOT_FIELDS = (
    "goal", "current_state", "schema_hint", "blockers", "namespace",
    "locked_by", "lock_expires_at", "locked_by_human", "human_note",
    "lease_epoch", "tainted", "taint_sources",
)
_DOC_FIELDS = (
    "title", "content", "frontmatter", "namespace", "locked_by_human",
    "human_note", "content_hash", "verification_status", "trust_level",
    "verified_version", "verified_hash", "embedding_model", "tainted",
    "taint_sources",
)
_TASK_FIELDS = (
    "title", "description", "status", "priority", "assigned_role", "namespace",
    "claimed_by", "lease_owner", "lease_expires_at", "input_context",
    "output_artifact", "handoff_brief", "artifact_uri", "artifact_version",
    "artifact_hash", "handed_off_by", "review_round", "review_history",
    "human_feedback", "reject_history",
    "ping_pong_count", "max_retries", "lease_epoch", "tainted", "taint_sources",
)


def _diff_fields(table: str, expected: dict[str, Any], current: dict[str, Any]) -> list[str]:
    if table == "context_slots":
        fields = _SLOT_FIELDS
    elif table == "shared_documents":
        fields = _DOC_FIELDS
    else:
        fields = _TASK_FIELDS
    mismatched: list[str] = []
    for field in fields:
        if field not in expected:
            continue
        left = json.dumps(expected.get(field), sort_keys=True, default=str)
        right = json.dumps(current.get(field), sort_keys=True, default=str)
        if left != right:
            mismatched.append(field)
    return mismatched


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _encode_batch_cursor(last_at: Any, batch_id: uuid.UUID) -> str:
    raw = json.dumps(
        {"last_at": last_at.isoformat(), "id": str(batch_id)},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_batch_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        )
        return datetime.fromisoformat(payload["last_at"]), uuid.UUID(payload["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ApiError(
            400, "invalid_cursor", "audit cursor is invalid"
        ) from exc


async def list_audit(
    session: AsyncSession,
    workspace: Workspace,
    *,
    limit: int = 50,
    batch_id: uuid.UUID | None = None,
    allowed_namespaces: list[str] | None = None,
    cursor: str | None = None,
) -> AuditListResponse:
    if (
        allowed_namespaces is not None
        and "*" not in allowed_namespaces
    ):
        raise Forbidden(
            "audit diffs span all namespaces; a namespace-restricted token "
            "cannot read them"
        )
    batch_query = (
        select(
            StateAuditDiff.session_batch_id,
            StateAuditDiff.agent_signature,
            func.count(StateAuditDiff.id).label("entry_count"),
            func.array_agg(func.distinct(StateAuditDiff.target_table)).label("targets"),
            func.min(StateAuditDiff.created_at).label("first_at"),
            func.max(StateAuditDiff.created_at).label("last_at"),
        )
        .where(StateAuditDiff.workspace_id == workspace.id)
        .group_by(StateAuditDiff.session_batch_id, StateAuditDiff.agent_signature)
        .order_by(
            func.max(StateAuditDiff.created_at).desc(),
            StateAuditDiff.session_batch_id.desc(),
        )
        .limit(limit + 1)
    )
    if batch_id is not None:
        batch_query = batch_query.where(StateAuditDiff.session_batch_id == batch_id)
    if cursor:
        cursor_time, cursor_id = _decode_batch_cursor(cursor)
        batch_query = batch_query.having(
            or_(
                func.max(StateAuditDiff.created_at) < cursor_time,
                and_(
                    func.max(StateAuditDiff.created_at) == cursor_time,
                    StateAuditDiff.session_batch_id < cursor_id,
                ),
            )
        )
    count_query = select(
        func.count(func.distinct(StateAuditDiff.session_batch_id))
    ).where(StateAuditDiff.workspace_id == workspace.id)
    if batch_id is not None:
        count_query = count_query.where(
            StateAuditDiff.session_batch_id == batch_id
        )
    total_batches = int(await session.scalar(count_query) or 0)
    batch_rows = (await session.execute(batch_query)).all()
    next_cursor = None
    if len(batch_rows) > limit:
        last = batch_rows[limit - 1]
        next_cursor = _encode_batch_cursor(last.last_at, last.session_batch_id)
        batch_rows = batch_rows[:limit]
    batches = [
        AuditBatchOut(
            session_batch_id=row.session_batch_id,
            agent_signature=row.agent_signature,
            entry_count=int(row.entry_count),
            targets=sorted(list(row.targets or [])),
            first_at=row.first_at,
            last_at=row.last_at,
        )
        for row in batch_rows
    ]

    diff_query = (
        select(StateAuditDiff)
        .where(StateAuditDiff.workspace_id == workspace.id)
        .order_by(StateAuditDiff.created_at.desc())
        .limit(min(limit * 4, 500))
    )
    if batch_id is not None:
        diff_query = diff_query.where(StateAuditDiff.session_batch_id == batch_id)
    diff_rows = (await session.execute(diff_query)).scalars().all()
    diffs = [
        AuditDiffOut(
            id=row.id,
            session_batch_id=row.session_batch_id,
            agent_signature=row.agent_signature,
            actor_auth_kind=row.actor_auth_kind,
            actor_token_id=row.actor_token_id,
            actor_user_id=row.actor_user_id,
            target_table=row.target_table,
            target_record_id=row.target_record_id,
            operation=row.operation,
            before_diff=row.before_diff or {},
            after_diff=row.after_diff or {},
            created_at=row.created_at,
        )
        for row in diff_rows
    ]
    return AuditListResponse(
        batches=batches,
        diffs=diffs,
        total_batches=total_batches,
        next_cursor=next_cursor,
    )


def _apply_slot_undo(slot: ContextSlot, before: dict[str, Any], actor: str) -> None:
    slot.goal = before.get("goal", slot.goal)
    slot.current_state = before.get("current_state", slot.current_state) or {}
    slot.schema_hint = before.get("schema_hint", slot.schema_hint) or {}
    slot.blockers = before.get("blockers", slot.blockers) or []
    slot.namespace = before.get("namespace", slot.namespace)
    slot.locked_by = before.get("locked_by", slot.locked_by)
    slot.lock_expires_at = _dt(before.get("lock_expires_at"))
    slot.locked_by_human = bool(before.get("locked_by_human", slot.locked_by_human))
    slot.human_note = before.get("human_note", slot.human_note)
    if "lease_epoch" in before:
        slot.lease_epoch = int(before["lease_epoch"] or 0)
    if "tainted" in before:
        slot.tainted = bool(before["tainted"])
    if "taint_sources" in before:
        slot.taint_sources = before["taint_sources"] or []
    slot.version = slot.version + 1
    slot.updated_by = actor
    slot.updated_at = utcnow()


def _apply_document_undo(
    document: SharedDocument, before: dict[str, Any], actor: str
) -> None:
    document.title = before.get("title", document.title)
    document.content = before.get("content", document.content)
    document.frontmatter = before.get("frontmatter", document.frontmatter) or {}
    document.namespace = before.get("namespace", document.namespace)
    document.locked_by_human = bool(
        before.get("locked_by_human", document.locked_by_human)
    )
    document.human_note = before.get("human_note", document.human_note)
    if "content_hash" in before:
        document.content_hash = before.get("content_hash")
    if "verification_status" in before:
        document.verification_status = before.get("verification_status") or "DRAFT"
    if "trust_level" in before:
        document.trust_level = before.get("trust_level") or "UNTRUSTED"
    if "verified_version" in before:
        document.verified_version = before.get("verified_version")
    if "verified_hash" in before:
        document.verified_hash = before.get("verified_hash")
    if "embedding_model" in before:
        document.embedding_model = before.get("embedding_model")
    if "tainted" in before:
        document.tainted = bool(before["tainted"])
    if "taint_sources" in before:
        document.taint_sources = before["taint_sources"] or []
    document.version = document.version + 1
    document.updated_by = actor
    document.updated_at = utcnow()


def _apply_task_undo(task: CollabTask, before: dict[str, Any]) -> None:
    task.title = before.get("title", task.title)
    task.description = before.get("description", task.description)
    try:
        task.status = TaskStatus(before.get("status", task.status.value))
    except ValueError:
        task.status = task.status
    task.priority = before.get("priority", task.priority)
    task.assigned_role = before.get("assigned_role", task.assigned_role)
    task.namespace = before.get("namespace", task.namespace)
    task.claimed_by = before.get("claimed_by", task.claimed_by)
    task.lease_owner = before.get("lease_owner", task.lease_owner)
    task.lease_expires_at = _dt(before.get("lease_expires_at"))
    task.input_context = before.get("input_context", task.input_context) or {}
    task.output_artifact = before.get("output_artifact", task.output_artifact) or {}
    task.handoff_brief = before.get("handoff_brief", task.handoff_brief)
    task.artifact_uri = before.get("artifact_uri", task.artifact_uri)
    if "artifact_uri" in before:
        # Snapshots older than v3.5 carry no binding; clear it so a restored
        # artifact URI can never be auto-verified against a stale pin.
        task.artifact_version = before.get("artifact_version")
        task.artifact_hash = before.get("artifact_hash")
        task.handed_off_by = before.get("handed_off_by")
    if "review_round" in before:
        task.review_round = int(before["review_round"] or 0)
        task.review_history = before.get("review_history") or []
    task.human_feedback = before.get("human_feedback", task.human_feedback)
    task.reject_history = before.get("reject_history", task.reject_history) or []
    task.ping_pong_count = before.get("ping_pong_count", task.ping_pong_count)
    task.max_retries = before.get("max_retries", task.max_retries)
    if "lease_epoch" in before:
        task.lease_epoch = int(before["lease_epoch"] or 0)
    if "tainted" in before:
        task.tainted = bool(before["tainted"])
    if "taint_sources" in before:
        task.taint_sources = before["taint_sources"] or []
    task.version = task.version + 1
    task.updated_at = utcnow()


async def undo_batch(
    session: AsyncSession,
    workspace: Workspace,
    batch_id: uuid.UUID,
    *,
    actor: str = "human_commander",
    force: bool = False,
) -> BatchUndoResponse:
    diffs = (
        await session.execute(
            select(StateAuditDiff)
            .where(
                StateAuditDiff.workspace_id == workspace.id,
                StateAuditDiff.session_batch_id == batch_id,
            )
            .order_by(StateAuditDiff.created_at.desc(), StateAuditDiff.id.desc())
            .limit(500)
        )
    ).scalars().all()
    if not diffs:
        raise NotFound("session_batch", f"no audit entries for batch {batch_id}")

    undo_batch_id = uuid.uuid4()
    rows: dict[uuid.UUID, Any] = {}
    projected: dict[uuid.UUID, dict[str, Any] | None] = {}
    conflicts: list[dict[str, Any]] = []

    # Lock each affected row once and dry-run the reverse batch in memory. This
    # correctly validates batches that update the same record multiple times.
    for entry in diffs:
        if entry.target_record_id in rows:
            continue
        model = _TABLES.get(entry.target_table)
        if model is None:
            rows[entry.target_record_id] = None
            projected[entry.target_record_id] = None
            continue
        row = (
            await session.execute(
                select(model)
                .where(
                    model.id == entry.target_record_id,
                    model.workspace_id == workspace.id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        rows[entry.target_record_id] = row
        if row is None:
            projected[entry.target_record_id] = None
        elif isinstance(row, ContextSlot):
            projected[entry.target_record_id] = slot_snapshot(row)
        elif isinstance(row, SharedDocument):
            projected[entry.target_record_id] = document_snapshot(row)
        else:
            projected[entry.target_record_id] = task_snapshot(row)

    for entry in diffs:
        row_id = entry.target_record_id
        current = projected.get(row_id)
        expected = entry.after_diff or {}
        if entry.operation == "INSERT":
            if row_id not in projected or current is None:
                continue
            fields = _diff_fields(entry.target_table, expected, current)
            if fields:
                conflicts.append(
                    {
                        "target_table": entry.target_table,
                        "target_record_id": str(row_id),
                        "operation": entry.operation,
                        "fields": fields,
                        "reason": "record changed after this insert",
                    }
                )
            projected[row_id] = None
            continue

        if row_id not in projected or current is None:
            if row_id in projected:
                conflicts.append(
                    {
                        "target_table": entry.target_table,
                        "target_record_id": str(row_id),
                        "operation": entry.operation,
                        "fields": [],
                        "reason": "record no longer exists",
                    }
                )
            continue
        fields = _diff_fields(entry.target_table, expected, current)
        if fields:
            conflicts.append(
                {
                    "target_table": entry.target_table,
                    "target_record_id": str(row_id),
                    "operation": entry.operation,
                    "fields": fields,
                    "reason": "current state differs from this batch's after-image",
                }
            )
        projected[row_id] = entry.before_diff or {}

    if conflicts and not force:
        await session.rollback()
        raise UndoConflict(str(batch_id), conflicts)

    restored = 0
    for entry in diffs:
        row = rows.get(entry.target_record_id)
        if row is None:
            continue

        if entry.operation == "INSERT":
            if isinstance(row, ContextSlot):
                current = slot_snapshot(row)
            elif isinstance(row, SharedDocument):
                current = document_snapshot(row)
            else:
                current = task_snapshot(row)
            await session.execute(
                delete(WorkspaceLink).where(
                    or_(
                        WorkspaceLink.source_id == entry.target_record_id,
                        WorkspaceLink.target_id == entry.target_record_id,
                    )
                )
            )
            await session.delete(row)
            await session.flush()
            await record_audit(
                session,
                workspace_id=workspace.id,
                session_batch_id=undo_batch_id,
                agent_signature=actor,
                target_table=entry.target_table,
                target_record_id=entry.target_record_id,
                operation="UNDO_INSERT",
                before=current,
                after={},
            )
            restored += 1
            continue

        if isinstance(row, ContextSlot):
            current = slot_snapshot(row)
            _apply_slot_undo(row, entry.before_diff or {}, actor)
            after = slot_snapshot(row)
        elif isinstance(row, SharedDocument):
            current = document_snapshot(row)
            _apply_document_undo(row, entry.before_diff or {}, actor)
            provider = get_embedding_provider()
            row.embedding = await provider.embed_one(row.content)
            row.embedding_model = provider.model
            await session.flush()
            await refresh_document_taint(session, row)
            await reindex_document_chunks(session, row)
            await _record_version(
                session,
                row,
                author=actor,
                batch_id=undo_batch_id,
                change_summary="batch undo",
            )
            after = document_snapshot(row)
        elif isinstance(row, CollabTask):
            current = task_snapshot(row)
            _apply_task_undo(row, entry.before_diff or {})
            await session.flush()
            await refresh_task_taint(session, row)
            after = task_snapshot(row)
        else:
            continue

        if isinstance(row, SharedDocument):
            await propagate_from_document(session, row)
        elif isinstance(row, ContextSlot):
            await refresh_slot_taint(session, row)
            after = slot_snapshot(row)
        await record_audit(
            session,
            workspace_id=workspace.id,
            session_batch_id=undo_batch_id,
            agent_signature=actor,
            target_table=entry.target_table,
            target_record_id=entry.target_record_id,
            operation="UNDO",
            before=current,
            after=after,
        )
        restored += 1
        await session.flush()

    compensations = await schedule_compensations_for_batch(
        session,
        workspace_id=workspace.id,
        batch_id=batch_id,
        actor=actor,
    )
    warnings = [
        "Timeline and workspace_events are append-only: event history is not deleted by undo.",
        "External actions without a compensation_payload cannot be automatically reversed.",
    ]
    if compensations:
        warnings.append(
            f"{compensations} external compensation action(s) were queued for dispatch."
        )
    if conflicts and force:
        warnings.append(
            f"{len(conflicts)} concurrent change(s) were overridden with force."
        )
    await append_event(
        session,
        workspace_id=workspace.id,
        agent=actor,
        event_type="BATCH_UNDONE",
        payload={
            "batch_id": str(batch_id),
            "undo_batch_id": str(undo_batch_id),
            "restored": restored,
            "conflicts": len(conflicts),
            "forced": bool(conflicts and force),
        },
        namespace="default",
    )
    await session.commit()
    return BatchUndoResponse(
        batch_id=batch_id,
        undo_batch_id=undo_batch_id,
        restored=restored,
        warnings=warnings,
    )


__all__ = ["list_audit", "undo_batch"]
