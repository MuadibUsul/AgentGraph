"""Task board: claims, TTL leases, L1/L2 handoffs, review and circuit breaker."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from functools import wraps
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    AuthKind,
    Principal,
    can,
    effective_agent,
    ensure_namespace,
    ensure_task_role,
    require_permission,
)
from app.config import get_settings
from app.database import async_session_factory
from app.engine.blackboard import lease_is_active, validate_handoff_brief
from app.errors import (
    ApiError,
    EscalatedToHuman,
    Forbidden,
    InvalidStateTransition,
    LeaseConflict,
    NotFound,
    TaskNotReviewable,
    VersionConflict,
)
from app.models import (
    CollabTask,
    ContextSlot,
    LinkType,
    SharedDocument,
    TaskStatus,
    WorkspaceLink,
)
from app.schemas import (
    DocumentSummary,
    SlotOut,
    TaskActionResponse,
    TaskClaimRequest,
    TaskContextResponse,
    TaskCreateRequest,
    TaskEscalateRequest,
    TaskHandoffRequest,
    TaskListResponse,
    TaskOut,
    TaskReleaseRequest,
    TaskRenewRequest,
    TaskReviewRequest,
)
from app.services.common import (
    append_event,
    db_now,
    document_summary,
    record_audit,
    slot_out,
    task_out,
    task_snapshot,
    task_summary,
    utcnow,
)
from app.services.documents import create_artifact, verify_artifact_for_review
from app.services.links import add_link
from app.services.policies import enforce_policy
from app.services.taint import refresh_task_taint
from app.tenant import activate_tenant

logger = logging.getLogger(__name__)
settings = get_settings()

_CLAIMABLE = {TaskStatus.READY, TaskStatus.HANDOFF}
_ACTIVE_LEASE_STATUSES = {TaskStatus.IN_PROGRESS, TaskStatus.REVIEWING}


async def _record_task_failure(
    principal: Principal,
    task_key: str,
    operation: str,
    exc: ApiError,
) -> None:
    """Write a rejected claim/transition to the timeline in its own transaction.

    The caller's transaction is already failing, so this uses a separate
    session and never replaces the original error if logging itself fails.
    """
    try:
        async with async_session_factory() as log_session:
            await activate_tenant(log_session, principal.workspace.id)
            task = await log_session.scalar(
                select(CollabTask).where(
                    CollabTask.workspace_id == principal.workspace.id,
                    CollabTask.task_key == task_key,
                )
            )
            await append_event(
                log_session,
                workspace_id=principal.workspace.id,
                agent=principal.agent_signature,
                event_type=f"TASK_{operation}_FAILED",
                payload={
                    "task_key": task_key,
                    "operation": operation.lower(),
                    "code": exc.code,
                    "http_status": exc.status_code,
                    "message": exc.message,
                    "details": exc.details,
                },
                namespace=task.namespace if task is not None else "default",
            )
            await log_session.commit()
    except Exception:  # noqa: BLE001 - failure telemetry must not mask the API error
        logger.exception("failed to record TASK_%s_FAILED for %s", operation, task_key)


def _task_failure_event(operation: str):
    def decorator(fn):
        @wraps(fn)
        async def wrapper(
            session: AsyncSession,
            principal: Principal,
            task_key: str,
            *args,
            **kwargs,
        ):
            try:
                return await fn(session, principal, task_key, *args, **kwargs)
            except ApiError as exc:
                await _record_task_failure(principal, task_key, operation, exc)
                raise

        return wrapper

    return decorator


async def _get_task(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    task_key: str,
    *,
    for_update: bool = False,
) -> CollabTask:
    query = select(CollabTask).where(
        CollabTask.workspace_id == workspace_id,
        CollabTask.task_key == task_key,
    )
    if for_update:
        query = query.with_for_update()
    task = (await session.execute(query)).scalar_one_or_none()
    if task is None:
        raise NotFound("collab_task", f"task '{task_key}' not found")
    return task


def _check_version(task: CollabTask, expected: int | None) -> None:
    if expected is not None and task.version != expected:
        raise VersionConflict("collab_task", task_out(task).model_dump(mode="json"))


def _check_lease(task: CollabTask, agent: str, now: Any | None = None) -> None:
    if (
        task.lease_owner
        and task.lease_owner != agent
        and lease_is_active(task.lease_owner, task.lease_expires_at, now)
    ):
        raise LeaseConflict("collab_task", task_out(task).model_dump(mode="json"))


def _require_active_lease(task: CollabTask, agent: str, now: Any) -> None:
    if task.lease_owner != agent or not lease_is_active(
        task.lease_owner, task.lease_expires_at, now
    ):
        raise LeaseConflict("collab_task", task_out(task).model_dump(mode="json"))


def _check_epoch(task: CollabTask, expected: int | None) -> None:
    if expected is not None and expected != task.lease_epoch:
        raise LeaseConflict("collab_task", task_out(task).model_dump(mode="json"))


def _require_claim_owner(task: CollabTask, principal: Principal, agent: str) -> None:
    if task.claimed_by != agent:
        raise ApiError(
            409,
            "claim_required",
            "only the agent that claimed this task may hand it off",
            details={"task_key": task.task_key, "claimed_by": task.claimed_by},
        )


def _clear_lease(task: CollabTask) -> None:
    task.claimed_by = None
    task.lease_owner = None
    task.lease_expires_at = None
    task.lease_epoch += 1


async def _upstream_briefs(
    session: AsyncSession, task: CollabTask
) -> list[dict[str, Any]]:
    target_ids = (
        await session.execute(
            select(WorkspaceLink.target_id).where(
                WorkspaceLink.workspace_id == task.workspace_id,
                WorkspaceLink.source_id == task.id,
                WorkspaceLink.relation_type == LinkType.DEPENDS_ON,
            )
        )
    ).scalars().all()
    if not target_ids:
        return []
    rows = (
        await session.execute(
            select(CollabTask).where(
                CollabTask.workspace_id == task.workspace_id,
                CollabTask.id.in_(target_ids),
            )
        )
    ).scalars().all()
    return [
        {
            "task_key": row.task_key,
            "title": row.title,
            "status": row.status.value,
            "handoff_brief": row.handoff_brief,
            "artifact_uri": row.artifact_uri,
        }
        for row in rows
    ]


async def _context_slots(
    session: AsyncSession, principal: Principal, task: CollabTask
) -> list[SlotOut]:
    if not can(principal, "state:read"):
        return []
    raw = task.input_context or {}
    keys: list[str] = []
    single = raw.get("slot_key")
    if isinstance(single, str):
        keys.append(single)
    many = raw.get("slot_keys")
    if isinstance(many, list):
        keys.extend(str(item) for item in many if isinstance(item, str))
    if not keys:
        return []
    slots = (
        await session.execute(
            select(ContextSlot).where(
                ContextSlot.workspace_id == task.workspace_id,
                ContextSlot.slot_key.in_(keys),
            )
        )
    ).scalars().all()
    return [slot_out(slot) for slot in slots]


async def _context_documents(
    session: AsyncSession, principal: Principal, task: CollabTask
) -> list[DocumentSummary]:
    if not can(principal, "doc:read"):
        return []
    raw = task.input_context or {}
    keys: list[str] = []
    single = raw.get("doc_key")
    if isinstance(single, str):
        keys.append(single)
    many = raw.get("doc_keys")
    if isinstance(many, list):
        keys.extend(str(item) for item in many if isinstance(item, str))
    if not keys:
        return []
    docs = (
        await session.execute(
            select(SharedDocument).where(
                SharedDocument.workspace_id == task.workspace_id,
                SharedDocument.doc_key.in_(keys),
            )
        )
    ).scalars().all()
    allowed = (
        None
        if principal.is_commander or "*" in principal.allowed_namespaces
        else set(principal.allowed_namespaces)
    )
    if allowed is not None:
        docs = [doc for doc in docs if doc.namespace in allowed]
    return [document_summary(doc) for doc in docs]


async def _create_task_core(
    session: AsyncSession,
    principal: Principal,
    request: TaskCreateRequest,
    *,
    agent: str,
    batch: uuid.UUID,
    commit: bool,
) -> CollabTask:
    require_permission(principal, "task:create")
    namespace = request.namespace or "default"
    ensure_namespace(principal, namespace, write=True)
    task_key = request.task_key or f"TASK-{uuid.uuid4().hex[:8].upper()}"
    await enforce_policy(
        session,
        principal,
        resource_type="task",
        key=task_key,
        action="create",
        payload=request.input_context,
    )
    existing = await session.scalar(
        select(CollabTask.id).where(
            CollabTask.workspace_id == principal.workspace.id,
            CollabTask.task_key == task_key,
        )
    )
    if existing is not None:
        raise ApiError(409, "task_exists", f"task '{task_key}' already exists")

    dependencies: list[CollabTask] = []
    for dep_key in request.depends_on:
        if dep_key == task_key:
            raise ApiError(422, "invalid_dependency", "a task cannot depend on itself")
        dependency = await _get_task(session, principal.workspace.id, dep_key)
        ensure_namespace(principal, dependency.namespace)
        dependencies.append(dependency)

    if dependencies:
        all_done = all(dep.status is TaskStatus.DONE for dep in dependencies)
        status = TaskStatus.READY if all_done else TaskStatus.TODO
    else:
        status = TaskStatus(request.status)

    assigned_role = request.assigned_role
    if assigned_role is None and not principal.is_commander:
        assigned_role = principal.role.value

    task = CollabTask(
        workspace_id=principal.workspace.id,
        task_key=task_key,
        title=request.title,
        description=request.description,
        status=status,
        priority=request.priority,
        assigned_role=assigned_role,
        namespace=namespace,
        input_context=request.input_context,
        max_retries=request.max_retries,
        version=1,
        created_by=agent,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(task)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise ApiError(409, "task_exists", f"task '{task_key}' already exists") from None

    for dependency in dependencies:
        await add_link(
            session,
            workspace_id=principal.workspace.id,
            source_id=task.id,
            target_id=dependency.id,
            relation_type=LinkType.DEPENDS_ON,
        )
    await refresh_task_taint(session, task)
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=agent,
        target_table="collab_tasks",
        target_record_id=task.id,
        operation="INSERT",
        before={},
        after=task_snapshot(task),
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=agent,
        event_type="TASK_CREATED",
        payload={
            "task_key": task_key,
            "title": task.title,
            "status": status.value,
            "assigned_role": assigned_role,
            "priority": task.priority,
        },
        namespace=namespace,
    )
    if commit:
        await session.commit()
    return task


async def create_task(
    session: AsyncSession, principal: Principal, request: TaskCreateRequest
) -> TaskOut:
    agent = effective_agent(principal, request.agent)
    batch = request.session_batch_id or uuid.uuid4()
    task = await _create_task_core(
        session, principal, request, agent=agent, batch=batch, commit=True
    )
    return task_out(task)


async def list_tasks(
    session: AsyncSession,
    principal: Principal,
    *,
    statuses: list[str] | None = None,
    assigned_role: str | None = None,
    namespace: str | None = None,
    limit: int = 100,
) -> TaskListResponse:
    require_permission(principal, "task:read")
    query = select(CollabTask).where(
        CollabTask.workspace_id == principal.workspace.id
    )
    if statuses:
        query = query.where(CollabTask.status.in_(statuses))
    if assigned_role:
        query = query.where(CollabTask.assigned_role == assigned_role)
    if namespace:
        ensure_namespace(principal, namespace)
        query = query.where(CollabTask.namespace == namespace)
    elif not principal.is_commander and "*" not in principal.allowed_namespaces:
        query = query.where(CollabTask.namespace.in_(principal.allowed_namespaces))
    tasks = (
        await session.execute(
            query.order_by(
                CollabTask.priority.desc(), CollabTask.updated_at.desc()
            ).limit(min(limit, settings.max_page_size))
        )
    ).scalars().all()
    return TaskListResponse(items=[task_summary(task) for task in tasks])


async def get_task(
    session: AsyncSession, principal: Principal, task_key: str
) -> TaskOut:
    require_permission(principal, "task:read")
    task = await _get_task(session, principal.workspace.id, task_key)
    ensure_namespace(principal, task.namespace)
    return task_out(task)


@_task_failure_event("CLAIM")
async def claim_task(
    session: AsyncSession,
    principal: Principal,
    task_key: str,
    request: TaskClaimRequest,
) -> TaskContextResponse:
    require_permission(principal, "task:claim")
    agent = effective_agent(principal, request.agent)
    batch = request.session_batch_id or uuid.uuid4()
    task = await _get_task(session, principal.workspace.id, task_key, for_update=True)
    ensure_namespace(principal, task.namespace, write=True)
    ensure_task_role(principal, task.assigned_role)
    if task.status is TaskStatus.ESCALATED_TO_HUMAN:
        raise EscalatedToHuman(task_out(task).model_dump(mode="json"))
    if task.status not in _CLAIMABLE and task.status is not TaskStatus.IN_PROGRESS:
        raise InvalidStateTransition(task.status.value, "IN_PROGRESS")
    now = await db_now(session)
    _check_lease(task, agent, now)
    before = task_snapshot(task)
    _clear_lease(task)
    task.status = TaskStatus.IN_PROGRESS
    task.claimed_by = agent
    task.lease_owner = agent
    task.lease_expires_at = now + timedelta(
        seconds=request.lease_seconds or settings.default_task_lease_seconds
    )
    task.version += 1
    task.updated_at = now
    await session.flush()
    await refresh_task_taint(session, task)
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=agent,
        target_table="collab_tasks",
        target_record_id=task.id,
        operation="UPDATE",
        before=before,
        after=task_snapshot(task),
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=agent,
        event_type="TASK_CLAIMED",
        payload={
            "task_key": task.task_key,
            "version": task.version,
            "lease_epoch": task.lease_epoch,
            "lease_expires_at": task.lease_expires_at.isoformat()
            if task.lease_expires_at
            else None,
        },
        namespace=task.namespace,
    )
    await session.commit()
    return TaskContextResponse(
        task=task_out(task),
        upstream_briefs=await _upstream_briefs(session, task),
        slots=await _context_slots(session, principal, task),
        documents=await _context_documents(session, principal, task),
        session_batch_id=batch,
    )


@_task_failure_event("RENEW")
async def renew_task(
    session: AsyncSession,
    principal: Principal,
    task_key: str,
    request: TaskRenewRequest,
) -> TaskOut:
    require_permission(principal, "task:claim")
    agent = effective_agent(principal, request.agent)
    task = await _get_task(session, principal.workspace.id, task_key, for_update=True)
    if task.status not in _ACTIVE_LEASE_STATUSES:
        raise InvalidStateTransition(task.status.value, "renew")
    now = await db_now(session)
    _check_epoch(task, request.lease_epoch)
    _require_active_lease(task, agent, now)
    task.lease_owner = agent
    task.lease_expires_at = now + timedelta(
        seconds=request.lease_seconds or settings.default_task_lease_seconds
    )
    task.claimed_by = agent
    task.version += 1
    task.updated_at = now
    await session.commit()
    return task_out(task)


@_task_failure_event("RELEASE")
async def release_task(
    session: AsyncSession,
    principal: Principal,
    task_key: str,
    request: TaskReleaseRequest,
) -> TaskOut:
    require_permission(principal, "task:transition")
    agent = effective_agent(principal, request.agent)
    batch = request.session_batch_id or uuid.uuid4()
    task = await _get_task(session, principal.workspace.id, task_key, for_update=True)
    now = await db_now(session)
    _check_epoch(task, request.lease_epoch)
    if request.force:
        if principal.auth_kind is not AuthKind.HUMAN_ADMIN:
            raise Forbidden("only the admin console may force-release a task")
    else:
        _require_active_lease(task, agent, now)
    before = task_snapshot(task)
    task.status = TaskStatus.READY
    _clear_lease(task)
    task.version += 1
    task.updated_at = now
    await session.flush()
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=agent,
        target_table="collab_tasks",
        target_record_id=task.id,
        operation="UPDATE",
        before=before,
        after=task_snapshot(task),
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=agent,
        event_type="TASK_RELEASED",
        payload={"task_key": task_key, "reason": request.reason},
        namespace=task.namespace,
    )
    await session.commit()
    return task_out(task)


async def _release_dependents(
    session: AsyncSession, completed: CollabTask
) -> None:
    dependent_ids = (
        await session.execute(
            select(WorkspaceLink.source_id).where(
                WorkspaceLink.workspace_id == completed.workspace_id,
                WorkspaceLink.target_id == completed.id,
                WorkspaceLink.relation_type == LinkType.DEPENDS_ON,
            )
        )
    ).scalars().all()
    for dependent_id in dependent_ids:
        dependent = (
            await session.execute(
                select(CollabTask)
                .where(
                    CollabTask.workspace_id == completed.workspace_id,
                    CollabTask.id == dependent_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if dependent is None or dependent.status is not TaskStatus.TODO:
            continue
        dependency_ids = (
            await session.execute(
                select(WorkspaceLink.target_id).where(
                    WorkspaceLink.workspace_id == completed.workspace_id,
                    WorkspaceLink.source_id == dependent.id,
                    WorkspaceLink.relation_type == LinkType.DEPENDS_ON,
                )
            )
        ).scalars().all()
        if not dependency_ids:
            continue
        dependencies = (
            await session.execute(
                select(CollabTask).where(
                    CollabTask.workspace_id == completed.workspace_id,
                    CollabTask.id.in_(dependency_ids),
                )
            )
        ).scalars().all()
        if not all(dep.status is TaskStatus.DONE for dep in dependencies):
            continue
        before = task_snapshot(dependent)
        dependent.status = TaskStatus.READY
        dependent.version += 1
        dependent.updated_at = utcnow()
        await session.flush()
        await record_audit(
            session,
            workspace_id=completed.workspace_id,
            session_batch_id=uuid.uuid4(),
            agent_signature="dependency_scheduler",
            target_table="collab_tasks",
            target_record_id=dependent.id,
            operation="UPDATE",
            before=before,
            after=task_snapshot(dependent),
        )
        await append_event(
            session,
            workspace_id=completed.workspace_id,
            agent="dependency_scheduler",
            event_type="TASK_READY",
            payload={
                "task_key": dependent.task_key,
                "unblocked_by": completed.task_key,
            },
            namespace=dependent.namespace,
        )


@_task_failure_event("HANDOFF")
async def handoff_task(
    session: AsyncSession,
    principal: Principal,
    task_key: str,
    request: TaskHandoffRequest,
) -> TaskActionResponse:
    require_permission(principal, "task:transition")
    agent = effective_agent(principal, request.agent)
    batch = request.session_batch_id or uuid.uuid4()
    task = await _get_task(session, principal.workspace.id, task_key, for_update=True)
    ensure_namespace(principal, task.namespace, write=True)
    ensure_task_role(principal, task.assigned_role)
    if task.status is TaskStatus.ESCALATED_TO_HUMAN:
        raise EscalatedToHuman(task_out(task).model_dump(mode="json"))
    if task.status not in (
        TaskStatus.IN_PROGRESS,
        TaskStatus.REVIEWING,
        TaskStatus.HANDOFF,
    ):
        raise InvalidStateTransition(task.status.value, request.next_status)
    _check_version(task, request.expected_version)
    now = await db_now(session)
    _check_lease(task, agent, now)
    _check_epoch(task, request.lease_epoch)
    _require_claim_owner(task, principal, agent)
    _require_active_lease(task, agent, now)
    brief = (
        validate_handoff_brief(request.brief.model_dump())
        if request.brief is not None
        else None
    )

    artifact_uri = request.artifact_uri
    if request.artifact_text is not None and request.artifact_uri is not None:
        raise ApiError(
            422,
            "ambiguous_artifact",
            "provide artifact_text or artifact_uri, not both",
        )
    if request.artifact_text:
        _, artifact_uri = await create_artifact(
            session,
            workspace=principal.workspace,
            task=task,
            agent=agent,
            artifact_text=request.artifact_text,
            risk_flag=bool(brief.get("risk_flag")) if brief else False,
            batch_id=batch,
        )

    if request.next_status == "REVIEWING" and (
        task.review_feedback is not None or task.reviewed_by is not None
    ):
        history = list(task.reject_history or [])
        history.append(
            {
                "kind": "previous_review",
                "feedback": task.review_feedback,
                "reviewed_by": task.reviewed_by,
                "at": utcnow().isoformat(),
                "version": task.version,
            }
        )
        task.reject_history = history[-settings.max_reject_history :]
        task.review_feedback = None
        task.reviewed_by = None

    before = task_snapshot(task)
    task.status = TaskStatus(request.next_status)
    task.handoff_brief = brief
    task.artifact_uri = artifact_uri
    task.output_artifact = request.output_artifact
    task.human_feedback = request.human_feedback
    _clear_lease(task)
    task.version += 1
    task.updated_at = utcnow()
    await session.flush()
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=agent,
        target_table="collab_tasks",
        target_record_id=task.id,
        operation="UPDATE",
        before=before,
        after=task_snapshot(task),
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=agent,
        event_type="TASK_HANDOFF",
        payload={
            "task_key": task.task_key,
            "status": task.status.value,
            "version": task.version,
            "summary": brief.get("summary") if brief else None,
            "artifact_uri": artifact_uri,
        },
        namespace=task.namespace,
    )
    if task.status is TaskStatus.DONE:
        await _release_dependents(session, task)

    next_task: CollabTask | None = None
    if request.create_next is not None:
        require_permission(principal, "task:create")
        next_request = TaskCreateRequest(
            task_key=request.create_next.task_key,
            title=request.create_next.title,
            assigned_role=request.create_next.assigned_role,
            priority=request.create_next.priority,
            namespace=request.create_next.namespace,
            input_context=request.create_next.input_context,
            agent=agent,
            session_batch_id=batch,
        )
        next_task = await _create_task_core(
            session, principal, next_request, agent=agent, batch=batch, commit=False
        )
        await add_link(
            session,
            workspace_id=principal.workspace.id,
            source_id=next_task.id,
            target_id=task.id,
            relation_type=LinkType.REFERENCES,
        )

    await session.commit()
    return TaskActionResponse(
        task=task_out(task),
        artifact_uri=artifact_uri,
        session_batch_id=batch,
        status=task.status,
        version=task.version,
        task_key=task.task_key,
    )


@_task_failure_event("REVIEW")
async def review_task(
    session: AsyncSession,
    principal: Principal,
    task_key: str,
    request: TaskReviewRequest,
) -> TaskActionResponse:
    if not (principal.is_commander or can(principal, "task:review")):
        require_permission(principal, "task:review")
    agent = effective_agent(principal, request.agent)
    batch = request.session_batch_id or uuid.uuid4()
    task = await _get_task(session, principal.workspace.id, task_key, for_update=True)
    ensure_namespace(principal, task.namespace, write=True)
    if task.status is not TaskStatus.REVIEWING and task.status not in (
        TaskStatus.AWAITING_HUMAN,
        TaskStatus.ESCALATED_TO_HUMAN,
    ):
        raise TaskNotReviewable(task.status.value, request.decision)
    if task.status is TaskStatus.ESCALATED_TO_HUMAN and not principal.is_commander:
        raise EscalatedToHuman(task_out(task).model_dump(mode="json"))
    _check_version(task, request.expected_version)
    _check_lease(task, agent)
    before = task_snapshot(task)
    human_decision = principal.is_human and principal.is_commander
    escalated = False

    task.review_feedback = request.feedback
    task.reviewed_by = agent
    artifact_verified = False
    if request.decision == "approve":
        next_status = request.next_status or "DONE"
        task.status = TaskStatus(next_status)
        if human_decision:
            task.human_feedback = request.feedback
        if human_decision:
            task.ping_pong_count = 0
        _clear_lease(task)
        task.version += 1
        task.updated_at = utcnow()
        event_type = "HUMAN_APPROVED" if human_decision else "TASK_APPROVED"
        if task.status is TaskStatus.DONE:
            await _release_dependents(session, task)
        if task.artifact_uri and task.artifact_uri.startswith("doc://"):
            try:
                artifact_verified = await verify_artifact_for_review(
                    session,
                    principal,
                    task.artifact_uri.removeprefix("doc://"),
                    actor=agent,
                    batch=batch,
                    task_key=task.task_key,
                    note=request.feedback,
                )
            except ApiError:
                artifact_verified = False
    else:
        task.ping_pong_count += 1
        entry = {
            "by": agent,
            "feedback": request.feedback,
            "at": utcnow().isoformat(),
            "version": task.version,
            "kind": "reject",
        }
        history = list(task.reject_history or [])
        history.append(entry)
        task.reject_history = history[-settings.max_reject_history :]
        if human_decision:
            task.human_feedback = request.feedback
            task.status = TaskStatus.READY
            event_type = "HUMAN_REJECTED"
        elif task.ping_pong_count >= task.max_retries:
            task.status = TaskStatus.ESCALATED_TO_HUMAN
            escalated = True
            event_type = "TASK_ESCALATED"
        else:
            task.status = TaskStatus.READY
            event_type = "TASK_REJECTED"
        _clear_lease(task)
        task.version += 1
        task.updated_at = utcnow()

    await session.flush()
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=agent,
        target_table="collab_tasks",
        target_record_id=task.id,
        operation="UPDATE",
        before=before,
        after=task_snapshot(task),
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=agent,
        event_type=event_type,
        payload={
            "task_key": task.task_key,
            "status": task.status.value,
            "version": task.version,
            "feedback": request.feedback,
            "ping_pong_count": task.ping_pong_count,
            "max_retries": task.max_retries,
            "artifact_verified": artifact_verified,
        },
        namespace=task.namespace,
    )
    await session.commit()
    return TaskActionResponse(
        task=task_out(task),
        escalated=escalated,
        session_batch_id=batch,
        status=task.status,
        version=task.version,
        task_key=task.task_key,
    )


@_task_failure_event("ESCALATE")
async def escalate_task(
    session: AsyncSession,
    principal: Principal,
    task_key: str,
    request: TaskEscalateRequest,
) -> TaskActionResponse:
    require_permission(principal, "task:transition")
    agent = effective_agent(principal, request.agent)
    batch = request.session_batch_id or uuid.uuid4()
    task = await _get_task(session, principal.workspace.id, task_key, for_update=True)
    ensure_namespace(principal, task.namespace, write=True)
    ensure_task_role(principal, task.assigned_role)
    _check_version(task, request.expected_version)
    if task.status in (TaskStatus.DONE, TaskStatus.FAILED):
        raise InvalidStateTransition(task.status.value, "ESCALATED_TO_HUMAN")
    before = task_snapshot(task)
    task.status = TaskStatus.ESCALATED_TO_HUMAN
    task.human_feedback = request.reason
    _clear_lease(task)
    task.version += 1
    task.updated_at = utcnow()
    await session.flush()
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=agent,
        target_table="collab_tasks",
        target_record_id=task.id,
        operation="UPDATE",
        before=before,
        after=task_snapshot(task),
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=agent,
        event_type="TASK_ESCALATED",
        payload={
            "task_key": task.task_key,
            "reason": request.reason,
            "version": task.version,
        },
        namespace=task.namespace,
    )
    await session.commit()
    return TaskActionResponse(
        task=task_out(task),
        escalated=True,
        session_batch_id=batch,
        status=task.status,
        version=task.version,
        task_key=task.task_key,
    )


async def reap_expired_task_leases(
    session: AsyncSession, limit: int = 50
) -> int:
    now = await db_now(session)
    tasks = (
        await session.execute(
            select(CollabTask)
            .where(
                CollabTask.status.in_(
                    [TaskStatus.IN_PROGRESS, TaskStatus.REVIEWING]
                ),
                CollabTask.lease_expires_at.is_not(None),
                CollabTask.lease_expires_at < now,
            )
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
    ).scalars().all()
    reaped = 0
    for task in tasks:
        before = task_snapshot(task)
        task.status = TaskStatus.READY
        _clear_lease(task)
        task.version += 1
        task.updated_at = now
        await session.flush()
        await record_audit(
            session,
            workspace_id=task.workspace_id,
            session_batch_id=uuid.uuid4(),
            agent_signature="lease_reaper",
            target_table="collab_tasks",
            target_record_id=task.id,
            operation="UPDATE",
            before=before,
            after=task_snapshot(task),
        )
        await append_event(
            session,
            workspace_id=task.workspace_id,
            agent="lease_reaper",
            event_type="LEASE_EXPIRED",
            payload={"task_key": task.task_key, "version": task.version},
            namespace=task.namespace,
        )
        reaped += 1
    if reaped:
        await session.commit()
    return reaped


async def reassign_task(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    task_key: str,
    *,
    assigned_role: str,
    reason: str | None,
    expected_version: int | None,
    reset_to_ready: bool,
    actor: str = "human_commander",
) -> TaskOut:
    task = await _get_task(session, workspace_id, task_key, for_update=True)
    _check_version(task, expected_version)
    before = task_snapshot(task)
    task.assigned_role = assigned_role
    if reset_to_ready:
        task.status = TaskStatus.READY
        _clear_lease(task)
    task.version += 1
    task.updated_at = utcnow()
    await session.flush()
    await record_audit(
        session,
        workspace_id=workspace_id,
        session_batch_id=uuid.uuid4(),
        agent_signature=actor,
        target_table="collab_tasks",
        target_record_id=task.id,
        operation="UPDATE",
        before=before,
        after=task_snapshot(task),
    )
    await append_event(
        session,
        workspace_id=workspace_id,
        agent=actor,
        event_type="TASK_REASSIGNED",
        payload={
            "task_key": task.task_key,
            "assigned_role": assigned_role,
            "status": task.status.value,
            "reason": reason,
        },
        namespace=task.namespace,
    )
    await session.commit()
    return task_out(task)


async def cancel_task(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    task_key: str,
    *,
    reason: str,
    expected_version: int | None,
    actor: str = "human_commander",
) -> TaskOut:
    task = await _get_task(session, workspace_id, task_key, for_update=True)
    _check_version(task, expected_version)
    if task.status in (TaskStatus.DONE, TaskStatus.FAILED):
        raise InvalidStateTransition(task.status.value, "FAILED")
    before = task_snapshot(task)
    task.status = TaskStatus.FAILED
    task.review_feedback = reason
    task.reviewed_by = actor
    _clear_lease(task)
    task.version += 1
    task.updated_at = utcnow()
    await session.flush()
    await record_audit(
        session,
        workspace_id=workspace_id,
        session_batch_id=uuid.uuid4(),
        agent_signature=actor,
        target_table="collab_tasks",
        target_record_id=task.id,
        operation="UPDATE",
        before=before,
        after=task_snapshot(task),
    )
    await append_event(
        session,
        workspace_id=workspace_id,
        agent=actor,
        event_type="TASK_CANCELLED",
        payload={"task_key": task.task_key, "reason": reason},
        namespace=task.namespace,
    )
    await session.commit()
    return task_out(task)
