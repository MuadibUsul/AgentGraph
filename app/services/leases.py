"""Unified leases: heartbeats, fencing, expiry, orphans and handover (PRD AG-CO-03).

The task row stays the fast path for lease checks (``lease_owner``,
``lease_expires_at``, ``lease_epoch``); each grant is also a ``leases`` row
with an address (``lease_<ULID>``), heartbeat history and how it ended. The
fencing token is the task's ``lease_epoch`` at grant time: it grows with every
grant, so a holder whose lease expired cannot write over its successor.
A trigger (v3_15) ends the previous lease row whenever the epoch moves.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, effective_agent, require_permission
from app.config import get_settings
from app.errors import ApiError, Forbidden, LeaseConflict
from app.models import AgentToken, CollabTask, Lease, SharedDocument, TaskStatus
from app.schemas import (
    LeaseHeartbeatRequest,
    LeaseHeartbeatResponse,
    LeaseListResponse,
    LeaseOut,
    TransferResponsibilitiesRequest,
    TransferResponsibilitiesResponse,
)
from app.services.common import (
    append_event,
    db_now,
    record_audit,
    task_out,
    task_snapshot,
)
from app.versioning import LATEST_VERSION, at_least

settings = get_settings()

HEARTBEAT_HISTORY = 20
CONTINUATION_NAMESPACE = "handoff"
_LIST_MAX = 200


def lease_out(lease: Lease) -> LeaseOut:
    return LeaseOut(
        lease_id=lease.lease_id,
        fencing_token=lease.fencing_token,
        resource_kind=lease.resource_kind,
        resource_ref=lease.resource_ref,
        holder=lease.holder,
        acquired_at=lease.acquired_at,
        expires_at=lease.expires_at,
        ttl_seconds=lease.ttl_seconds,
        heartbeat_interval_s=lease.heartbeat_interval_s,
        last_heartbeat_at=lease.last_heartbeat_at,
        heartbeat_note=lease.heartbeat_note,
        progress=lease.progress,
        state=lease.state,
        ended_at=lease.ended_at,
        end_reason=lease.end_reason,
    )


def ttl_for(lease_ttl_seconds: int | None, lease_seconds: int | None) -> int:
    """Explicit TTL wins; 2026-11 defaults to 900 s, the baseline keeps its default."""
    if lease_ttl_seconds:
        return lease_ttl_seconds
    if lease_seconds:
        return lease_seconds
    if at_least(LATEST_VERSION):
        return settings.lease_default_ttl_seconds
    return settings.default_task_lease_seconds


def continuation_key(task: CollabTask) -> str:
    return f"{CONTINUATION_NAMESPACE}/{task.canonical_id.lower()}"


def clear_task_lease(task: CollabTask) -> None:
    task.claimed_by = None
    task.lease_owner = None
    task.lease_expires_at = None
    task.lease_epoch += 1


# ------------------------------------------------------------------ granting


async def grant_task_lease(
    session: AsyncSession, principal: Principal, task: CollabTask, agent: str, ttl: int
) -> Lease:
    """Record the lease the task row now carries (call after the claim flush)."""
    now = await db_now(session)
    lease = Lease(
        workspace_id=task.workspace_id,
        resource_kind="task",
        resource_id=task.id,
        resource_ref=task.canonical_id,
        fencing_token=task.lease_epoch,
        holder=agent,
        holder_token_id=principal.token_id,
        acquired_at=now,
        expires_at=task.lease_expires_at,
        ttl_seconds=ttl,
        heartbeat_interval_s=max(1, ttl // 3),
        last_heartbeat_at=now,
    )
    session.add(lease)
    await session.flush()
    await session.refresh(lease, ["lease_id"])
    return lease


async def active_task_lease(session: AsyncSession, task: CollabTask) -> Lease | None:
    return await session.scalar(
        select(Lease).where(
            Lease.resource_kind == "task",
            Lease.resource_id == task.id,
            Lease.state == "active",
            Lease.fencing_token == task.lease_epoch,
        )
    )


async def _get_lease(
    session: AsyncSession, workspace_id: uuid.UUID, lease_id: str
) -> Lease:
    lease = await session.scalar(
        select(Lease)
        .where(Lease.workspace_id == workspace_id, Lease.lease_id == lease_id)
        .with_for_update()
    )
    if lease is None:
        raise ApiError(404, "lease_not_found", f"lease '{lease_id}' not found",
                       details={"lease_id": lease_id})
    return lease


# ------------------------------------------------------------------ checking


def check_fencing(task: CollabTask, presented: int | None) -> None:
    """Refuse a write carrying an older fencing token than the task's current one."""
    if presented is None or presented == task.lease_epoch:
        return
    raise ApiError(
        409,
        "stale_fencing_token",
        "this fencing token belongs to an earlier lease on the task",
        details={"presented": presented, "current": task.lease_epoch,
                 "task_key": task.task_key},
    )


def _lost(task: CollabTask, lease: Lease | None) -> ApiError:
    holder = task.lease_owner if task.lease_expires_at else None
    return ApiError(
        409,
        "lease_lost",
        "your lease on this task ended; claim it again if it is still available",
        details={
            "current_holder": holder,
            "task_status": task.status.value,
            "lease_id": lease.lease_id if lease else None,
            "ended": lease.state if lease else None,
            "end_reason": lease.end_reason if lease else None,
        },
    )


async def require_holder(
    session: AsyncSession, task: CollabTask, agent: str, now: datetime
) -> None:
    """The caller must hold a live lease on the task.

    Under 2026-11 a caller whose own lease expired or was revoked gets
    ``lease_lost`` (with the current holder); otherwise ``lease_conflict``.
    """
    live = (
        task.lease_owner == agent
        and task.lease_expires_at is not None
        and task.lease_expires_at > now
    )
    if live:
        return
    if at_least(LATEST_VERSION):
        previous = await session.scalar(
            select(Lease)
            .where(
                Lease.resource_kind == "task",
                Lease.resource_id == task.id,
                Lease.holder == agent,
            )
            .order_by(Lease.fencing_token.desc())
            .limit(1)
        )
        if previous is not None and (
            previous.state in ("expired", "revoked")
            or (previous.state == "active" and previous.expires_at <= now)
        ):
            raise _lost(task, previous)
    raise LeaseConflict("collab_task", task_out(task).model_dump(mode="json"))


async def touch(session: AsyncSession, task: CollabTask, now: datetime) -> None:
    """Any holder write on the task counts as a heartbeat."""
    await session.execute(
        update(Lease)
        .where(
            Lease.resource_kind == "task",
            Lease.resource_id == task.id,
            Lease.state == "active",
            Lease.fencing_token == task.lease_epoch,
        )
        .values(last_heartbeat_at=now, heartbeat_missed_at=None)
    )


# ------------------------------------------------------------------ holder API


async def heartbeat(
    session: AsyncSession, principal: Principal, lease_id: str, request: LeaseHeartbeatRequest
) -> LeaseHeartbeatResponse:
    require_permission(principal, "task:claim")
    agent = effective_agent(principal, request.agent)
    lease = await _get_lease(session, principal.workspace.id, lease_id)
    if lease.holder != agent:
        raise ApiError(403, "not_lease_holder", "only the lease holder may heartbeat",
                       details={"lease_id": lease_id, "holder": lease.holder})
    task = await session.get(CollabTask, lease.resource_id, with_for_update=True)
    now = await db_now(session)
    if (
        task is None
        or lease.state != "active"
        or lease.expires_at <= now
        or task.lease_epoch != lease.fencing_token
        or task.lease_owner != agent
    ):
        if task is None:
            raise ApiError(409, "lease_lost", "the leased task no longer exists",
                           details={"lease_id": lease_id})
        raise _lost(task, lease)
    expires = now + timedelta(seconds=lease.ttl_seconds)
    task.lease_expires_at = expires
    lease.expires_at = expires
    lease.last_heartbeat_at = now
    lease.heartbeat_missed_at = None
    if request.note is not None:
        lease.heartbeat_note = request.note
    if request.progress is not None:
        lease.progress = request.progress
    if request.note is not None or request.progress is not None:
        history = list(lease.heartbeats or [])
        history.append({"at": now.isoformat(), "note": request.note,
                        "progress": request.progress})
        lease.heartbeats = history[-HEARTBEAT_HISTORY:]
    await session.commit()
    return LeaseHeartbeatResponse(
        lease_id=lease.lease_id, expires_at=expires, fencing_token=lease.fencing_token,
        heartbeat_interval_s=lease.heartbeat_interval_s,
    )


async def release(
    session: AsyncSession, principal: Principal, lease_id: str, *, reason: str | None,
    agent: str | None,
) -> LeaseOut:
    require_permission(principal, "task:transition")
    actor = effective_agent(principal, agent)
    lease = await _get_lease(session, principal.workspace.id, lease_id)
    if lease.holder != actor and not principal.is_human:
        raise ApiError(403, "not_lease_holder", "only the lease holder may release it",
                       details={"lease_id": lease_id, "holder": lease.holder})
    if lease.state != "active":
        return lease_out(lease)
    now = await db_now(session)
    lease.state = "released"
    lease.ended_at = now
    lease.end_reason = "released"
    task = await session.get(CollabTask, lease.resource_id, with_for_update=True)
    await session.flush()
    if task is not None and task.lease_epoch == lease.fencing_token and task.lease_owner:
        before = task_snapshot(task)
        if task.status in (TaskStatus.IN_PROGRESS, TaskStatus.REVIEWING):
            task.status = TaskStatus.READY
        clear_task_lease(task)
        task.version += 1
        task.updated_at = now
        await session.flush()
        await record_audit(
            session, workspace_id=task.workspace_id, session_batch_id=uuid.uuid4(),
            agent_signature=actor, target_table="collab_tasks", target_record_id=task.id,
            operation="UPDATE", before=before, after=task_snapshot(task),
        )
        await append_event(
            session, workspace_id=task.workspace_id, agent=actor, event_type="TASK_RELEASED",
            payload={"task_key": task.task_key, "reason": reason, "lease_id": lease.lease_id},
            namespace=task.namespace,
        )
    await session.commit()
    return lease_out(lease)


def _encode_cursor(lease: Lease) -> str:
    raw = f"{lease.acquired_at.isoformat()}|{lease.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        stamp, ident = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(stamp), uuid.UUID(ident)
    except Exception as exc:  # noqa: BLE001 - any malformed cursor is the client's
        raise ApiError(422, "invalid_cursor", "cursor is not valid",
                       details={"field": "cursor"}) from exc


async def list_leases(
    session: AsyncSession,
    principal: Principal,
    *,
    holder: str | None,
    state: str | None,
    cursor: str | None,
    limit: int,
) -> LeaseListResponse:
    require_permission(principal, "task:read")
    me = effective_agent(principal, None)
    if holder == "me":
        holder = me
    if not (principal.is_human or principal.is_commander) and holder != me:
        # Agents see their own leases only.
        holder = me
    query = select(Lease).where(Lease.workspace_id == principal.workspace.id)
    if holder:
        query = query.where(Lease.holder == holder)
    if state:
        query = query.where(Lease.state == state)
    if cursor:
        at, ident = _decode_cursor(cursor)
        query = query.where(
            or_(Lease.acquired_at < at, and_(Lease.acquired_at == at, Lease.id < ident))
        )
    limit = max(1, min(limit, _LIST_MAX))
    rows = (
        await session.execute(
            query.order_by(Lease.acquired_at.desc(), Lease.id.desc()).limit(limit + 1)
        )
    ).scalars().all()
    page = rows[:limit]
    return LeaseListResponse(
        items=[lease_out(row) for row in page],
        next_cursor=_encode_cursor(page[-1]) if len(rows) > limit else None,
    )


# ------------------------------------------------------------------ orphans


async def _auto_continuation(
    session: AsyncSession, task: CollabTask, lease: Lease | None, holder: str, reason: str
) -> str | None:
    """Minimal continuation pack when the holder left none (AG-CO-03 4b)."""
    from app.services.onboarding import _find, write_platform_document

    since = lease.acquired_at if lease else task.updated_at
    key = continuation_key(task)
    existing = await _find(session, task.workspace_id, key)
    if existing is not None and existing.updated_at >= since and existing.updated_by == holder:
        return None  # the holder wrote one during this lease
    written = (
        await session.execute(
            select(SharedDocument.namespace, SharedDocument.doc_key, SharedDocument.version)
            .where(
                SharedDocument.workspace_id == task.workspace_id,
                SharedDocument.updated_by == holder,
                SharedDocument.updated_at >= since,
            )
            .order_by(SharedDocument.updated_at.desc())
            .limit(50)
        )
    ).all()
    notes = list(lease.heartbeats or []) if lease else []
    lines = [
        f"# Continuation: {task.title}",
        "",
        "Generated by the platform because the lease ended without a continuation.",
        "",
        f"- Task: `{task.canonical_id}` (`{task.task_key}`)",
        f"- Previous holder: `{holder}`",
        f"- Claimed at: {since.isoformat()}",
        f"- Lease ended: {reason}",
        "",
        "## Heartbeat notes",
        "",
    ]
    lines += [
        f"- {item.get('at')}: {item.get('note') or ''}"
        + (f" (progress {item['progress']:.0%})" if item.get("progress") is not None else "")
        for item in notes
    ] or ["- none"]
    lines += ["", "## Documents written by the holder since the claim", ""]
    lines += [f"- `{doc_key}` v{version} ({namespace})" for namespace, doc_key, version in written]
    if not written:
        lines.append("- none")
    document = await write_platform_document(
        session,
        task.workspace_id,
        namespace=CONTINUATION_NAMESPACE,
        doc_key=key,
        title=f"Continuation: {task.title}"[:256],
        content="\n".join(lines) + "\n",
        frontmatter={
            "kind": "continuation",
            "task_id": task.canonical_id,
            "author": holder,
            "auto_generated": True,
            "handoff_to": None,
        },
        author="system",
        change_summary=f"auto-generated continuation ({reason})",
    )
    return document.doc_key


async def orphan_task(
    session: AsyncSession, task: CollabTask, *, state: str, reason: str, actor: str
) -> None:
    """End the active lease and put the task back on the board (or escalate it)."""
    now = await db_now(session)
    lease = await active_task_lease(session, task)
    holder = task.lease_owner or (lease.holder if lease else None)
    before = task_snapshot(task)
    if lease is not None:
        lease.state = state
        lease.ended_at = now
        lease.end_reason = reason
        await session.flush()
    continuation = None
    if holder:
        continuation = await _auto_continuation(session, task, lease, holder, reason)
        task.continuation = {"namespace": CONTINUATION_NAMESPACE, "doc_key": continuation}
        task.continuation_at = now
    task.orphaned_from = holder
    task.orphan_count = (task.orphan_count or 0) + 1
    escalate = task.orphan_count >= settings.orphan_escalation_threshold
    task.status = TaskStatus.ESCALATED_TO_HUMAN if escalate else TaskStatus.READY
    clear_task_lease(task)
    task.version += 1
    task.updated_at = now
    await session.flush()
    await record_audit(
        session, workspace_id=task.workspace_id, session_batch_id=uuid.uuid4(),
        agent_signature=actor, target_table="collab_tasks", target_record_id=task.id,
        operation="UPDATE", before=before, after=task_snapshot(task),
    )
    targets = [item for item in dict.fromkeys([task.created_by, holder]) if item]
    await append_event(
        session,
        workspace_id=task.workspace_id,
        agent=actor,
        event_type="LEASE_EXPIRED",
        payload={
            "task_key": task.task_key,
            "id": task.canonical_id,
            "version": task.version,
            "lease_id": lease.lease_id if lease else None,
            "orphaned_from": holder,
            "orphan_count": task.orphan_count,
            "continuation": continuation,
            "targets": targets,
            "details": {"reason": reason},
        },
        namespace=task.namespace,
    )
    if escalate:
        await append_event(
            session,
            workspace_id=task.workspace_id,
            agent=actor,
            event_type="TASK_ESCALATED",
            payload={
                "task_key": task.task_key,
                "id": task.canonical_id,
                "reason": "repeated_orphan",
                "orphan_count": task.orphan_count,
                "version": task.version,
                "targets": [task.created_by],
            },
            namespace=task.namespace,
        )


async def flag_missed_heartbeats(session: AsyncSession, limit: int = 200) -> int:
    """One TASK_HEARTBEAT_MISSED per lease once a heartbeat interval passes silently."""
    now = await db_now(session)
    leases = (
        await session.execute(
            select(Lease)
            .where(
                Lease.state == "active",
                Lease.heartbeat_missed_at.is_(None),
                Lease.expires_at > now,
            )
            .with_for_update(skip_locked=True)
            .limit(limit * 5)
        )
    ).scalars().all()
    flagged = 0
    for lease in leases:
        last = lease.last_heartbeat_at or lease.acquired_at
        if last + timedelta(seconds=lease.heartbeat_interval_s) > now:
            continue
        task = await session.get(CollabTask, lease.resource_id)
        if task is not None and task.status is TaskStatus.WAITING_HUMAN:
            continue  # paused while a human decides (AG-CO-05)
        lease.heartbeat_missed_at = now
        await append_event(
            session,
            workspace_id=lease.workspace_id,
            agent="lease_reaper",
            event_type="TASK_HEARTBEAT_MISSED",
            payload={
                "task_key": task.task_key if task else None,
                "id": lease.resource_ref,
                "lease_id": lease.lease_id,
                "holder": lease.holder,
                "expires_at": lease.expires_at.isoformat(),
                "targets": [lease.holder],
            },
            namespace=task.namespace if task else "default",
        )
        flagged += 1
        if flagged >= limit:
            break
    return flagged


async def revoke_holder(
    session: AsyncSession, workspace_id: uuid.UUID, token: AgentToken, *, reason: str
) -> list[str]:
    """Release every lease a revoked or retired token holds (AG-CO-03 6)."""
    rows = (
        await session.execute(
            select(Lease.resource_id).where(
                Lease.workspace_id == workspace_id,
                Lease.state == "active",
                or_(Lease.holder_token_id == token.id,
                    and_(Lease.holder_token_id.is_(None),
                         Lease.holder == token.agent_signature)),
            )
        )
    ).scalars().all()
    revoked: list[str] = []
    for task_id in rows:
        task = await session.get(CollabTask, task_id, with_for_update=True)
        if task is None or not task.lease_owner:
            continue
        await orphan_task(session, task, state="revoked", reason=reason, actor="lease_revoker")
        revoked.append(task.task_key)
    return revoked


# ------------------------------------------------------------------ succession


async def transfer_responsibilities(
    session: AsyncSession, principal: Principal, request: TransferResponsibilitiesRequest
) -> TransferResponsibilitiesResponse:
    """Hand a principal's open work to a successor in one audited step (human only)."""
    from app.services.documents import human_update_frontmatter

    if not principal.is_human:
        raise Forbidden("only a human may transfer responsibilities")
    source, target = request.from_, request.to
    if source == target:
        raise ApiError(422, "validation_error", "from and to must differ",
                       details={"field": "to"})
    source_agent = source.split(":", 1)[1] if ":" in source else source
    target_agent = target.split(":", 1)[1] if ":" in target else target
    source_ref = source if ":" in source else f"agent:{source}"
    target_ref = target if ":" in target else f"agent:{target}"
    workspace_id = principal.workspace.id
    result = TransferResponsibilitiesResponse(from_=source_ref, to=target_ref)
    include = set(request.include)
    actor = principal.agent_signature
    open_statuses = [status for status in TaskStatus
                     if status not in (TaskStatus.DONE, TaskStatus.FAILED)]

    if "assigned_tasks" in include:
        tasks = (
            await session.execute(
                select(CollabTask)
                .where(
                    CollabTask.workspace_id == workspace_id,
                    CollabTask.status.in_(open_statuses),
                    or_(CollabTask.assignee == source_ref,
                        and_(CollabTask.lease_owner == source_agent,
                             CollabTask.status == TaskStatus.IN_PROGRESS)),
                )
                .with_for_update()
            )
        ).scalars().all()
        for task in tasks:
            if task.lease_owner == source_agent:
                await orphan_task(session, task, state="revoked", reason="holder_retired",
                                  actor=actor)
                result.leases_revoked.append(task.task_key)
            task.assignee = target_ref
            result.assigned_tasks.append(task.task_key)
    if "pending_reviews" in include:
        reviews = (
            await session.execute(
                select(CollabTask)
                .where(
                    CollabTask.workspace_id == workspace_id,
                    CollabTask.status == TaskStatus.REVIEWING,
                    CollabTask.lease_owner == source_agent,
                )
                .with_for_update()
            )
        ).scalars().all()
        for task in reviews:
            lease = await active_task_lease(session, task)
            if lease is not None:
                lease.state = "revoked"
                lease.ended_at = await db_now(session)
                lease.end_reason = "holder_retired"
            clear_task_lease(task)
            task.assignee = target_ref
            task.version += 1
            result.pending_reviews.append(task.task_key)
            result.leases_revoked.append(task.task_key)
    if "maintained_docs" in include:
        documents = (
            await session.execute(
                select(SharedDocument).where(
                    SharedDocument.workspace_id == workspace_id,
                    SharedDocument.frontmatter["maintainer"].astext.in_(
                        [source_ref, source_agent]
                    ),
                )
            )
        ).scalars().all()
        for document in documents:
            frontmatter = dict(document.frontmatter or {})
            frontmatter["maintainer"] = (
                target_ref if frontmatter.get("maintainer") == source_ref else target_agent
            )
            await human_update_frontmatter(
                session, principal, document, frontmatter,
                change_summary=f"maintainer transferred from {source_ref} to {target_ref}",
            )
            result.maintained_docs.append(document.doc_key)
    if "watermarks" in include:
        result.skipped["watermarks"] = "named watermarks arrive with AG-EV-01"
    await session.flush()
    await record_audit(
        session,
        workspace_id=workspace_id,
        session_batch_id=uuid.uuid4(),
        agent_signature=actor,
        target_table="responsibilities",
        target_record_id=uuid.uuid4(),
        operation="UPDATE",
        before={"holder": source_ref},
        after={"holder": target_ref, **result.model_dump(exclude={"from_", "to"})},
    )
    await append_event(
        session,
        workspace_id=workspace_id,
        agent=actor,
        event_type="RESPONSIBILITIES_TRANSFERRED",
        payload={"from": source_ref, "to": target_ref, "reason": request.reason,
                 **result.model_dump(exclude={"from_", "to", "skipped"}),
                 "targets": [target_ref]},
    )
    await session.commit()
    return result
