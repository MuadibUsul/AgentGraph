"""Approved external-action outbox with retries and compensation."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session_factory
from app.errors import ApiError, ExternalActionError, NotFound
from app.models import ExternalAction, ExternalActionAttempt
from app.schemas import (
    ExternalActionCompensateRequest,
    ExternalActionCreateRequest,
    ExternalActionListResponse,
    ExternalActionOut,
    ExternalActionRejectRequest,
)
from app.services.common import append_event, db_now, record_audit, utcnow
from app.services.outbound import send_json, validate_target
from app.services.usage import assert_budget, record_usage

logger = logging.getLogger("agentgraph.actions")
settings = get_settings()


def action_out(action: ExternalAction) -> ExternalActionOut:
    return ExternalActionOut(
        id=action.id,
        action_type=action.action_type,
        target_url=action.target_url,
        method=action.method,
        idempotency_key=action.idempotency_key,
        status=action.status,
        attempts=action.attempts,
        max_attempts=action.max_attempts,
        last_error=action.last_error,
        response_status=action.response_status,
        approved_by=action.approved_by,
        approved_at=action.approved_at,
        next_attempt_at=action.next_attempt_at,
        completed_at=action.completed_at,
        compensates_action_id=action.compensates_action_id,
        compensation_payload=action.compensation_payload,
        namespace=action.namespace,
        session_batch_id=action.session_batch_id,
        created_by=action.created_by,
        created_at=action.created_at,
        updated_at=action.updated_at,
    )


async def create_action(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    actor: str,
    request: ExternalActionCreateRequest,
) -> ExternalActionOut:
    existing = (
        await session.execute(
            select(ExternalAction).where(
                ExternalAction.workspace_id == workspace_id,
                ExternalAction.idempotency_key == request.idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return action_out(existing)
    await validate_target(request.target_url)
    await assert_budget(session, workspace_id)
    action = ExternalAction(
        workspace_id=workspace_id,
        action_type=request.action_type,
        target_url=request.target_url,
        method=request.method,
        headers=request.headers,
        payload=request.payload,
        idempotency_key=request.idempotency_key,
        status="PENDING_APPROVAL",
        attempts=0,
        max_attempts=settings.outbound_max_attempts,
        approved_by=None,
        approved_at=None,
        next_attempt_at=None,
        compensation_payload=request.compensation_payload,
        namespace=request.namespace,
        session_batch_id=request.session_batch_id,
        created_by=actor,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(action)
    await session.flush()
    await record_audit(
        session,
        workspace_id=workspace_id,
        session_batch_id=request.session_batch_id or uuid.uuid4(),
        agent_signature=actor,
        target_table="external_actions",
        target_record_id=action.id,
        operation="INSERT",
        before={},
        after=action_out(action).model_dump(mode="json"),
    )
    await append_event(
        session,
        workspace_id=workspace_id,
        agent=actor,
        event_type="EXTERNAL_ACTION_CREATED",
        payload={
            "action_id": str(action.id),
            "action_type": action.action_type,
            "status": action.status,
            "target": action.target_url,
        },
        namespace=action.namespace,
    )
    await session.commit()
    return action_out(action)


async def list_actions(
    session: AsyncSession, workspace_id: uuid.UUID, *, limit: int = 100
) -> ExternalActionListResponse:
    rows = (
        await session.execute(
            select(ExternalAction)
            .where(ExternalAction.workspace_id == workspace_id)
            .order_by(ExternalAction.created_at.desc())
            .limit(min(limit, 500))
        )
    ).scalars().all()
    return ExternalActionListResponse(items=[action_out(row) for row in rows])


async def _get_action(
    session: AsyncSession, workspace_id: uuid.UUID, action_id: uuid.UUID
) -> ExternalAction:
    action = await session.get(ExternalAction, action_id)
    if action is None or action.workspace_id != workspace_id:
        raise NotFound("external_action")
    return action


async def approve_action(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    action_id: uuid.UUID,
    actor: str,
) -> ExternalActionOut:
    action = await _get_action(session, workspace_id, action_id)
    if action.status != "PENDING_APPROVAL":
        raise ExternalActionError(
            f"cannot approve an action in {action.status} state"
        )
    action.status = "APPROVED"
    action.approved_by = actor
    action.approved_at = utcnow()
    action.next_attempt_at = utcnow()
    action.updated_at = utcnow()
    await append_event(
        session,
        workspace_id=workspace_id,
        agent=actor,
        event_type="EXTERNAL_ACTION_APPROVED",
        payload={"action_id": str(action.id), "action_type": action.action_type},
        namespace=action.namespace,
    )
    await session.commit()
    return action_out(action)


async def reject_action(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    action_id: uuid.UUID,
    actor: str,
    request: ExternalActionRejectRequest,
) -> ExternalActionOut:
    action = await _get_action(session, workspace_id, action_id)
    if action.status not in ("PENDING_APPROVAL", "APPROVED"):
        raise ExternalActionError(
            f"cannot reject an action in {action.status} state"
        )
    action.status = "REJECTED"
    action.last_error = request.note
    action.completed_at = utcnow()
    action.updated_at = utcnow()
    await append_event(
        session,
        workspace_id=workspace_id,
        agent=actor,
        event_type="EXTERNAL_ACTION_REJECTED",
        payload={"action_id": str(action.id), "note": request.note},
        namespace=action.namespace,
    )
    await session.commit()
    return action_out(action)


async def compensate_action(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    action_id: uuid.UUID,
    actor: str,
    request: ExternalActionCompensateRequest,
) -> ExternalActionOut:
    action = await _get_action(session, workspace_id, action_id)
    if action.status not in ("SUCCEEDED", "FAILED", "COMPENSATION_PENDING"):
        raise ExternalActionError(
            f"cannot compensate an action in {action.status} state"
        )
    payload = request.payload or action.compensation_payload
    if not payload:
        raise ExternalActionError(
            "this action has no compensation_payload; supply one explicitly"
        )
    existing = (
        await session.execute(
            select(ExternalAction).where(
                ExternalAction.workspace_id == workspace_id,
                ExternalAction.idempotency_key == f"comp:{action.id}",
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return action_out(existing)
    compensation = ExternalAction(
        workspace_id=workspace_id,
        action_type=f"compensation:{action.action_type}"[:64],
        target_url=action.target_url,
        method=action.method,
        headers=action.headers,
        payload=payload,
        idempotency_key=f"comp:{action.id}",
        status="APPROVED",
        attempts=0,
        max_attempts=settings.outbound_max_attempts,
        approved_by=actor,
        approved_at=utcnow(),
        next_attempt_at=utcnow(),
        compensates_action_id=action.id,
        namespace=action.namespace,
        session_batch_id=action.session_batch_id,
        created_by=actor,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(compensation)
    action.status = "COMPENSATION_PENDING"
    action.updated_at = utcnow()
    await session.flush()
    await append_event(
        session,
        workspace_id=workspace_id,
        agent=actor,
        event_type="EXTERNAL_COMPENSATION_SCHEDULED",
        payload={
            "action_id": str(action.id),
            "compensation_id": str(compensation.id),
        },
        namespace=action.namespace,
    )
    await session.commit()
    return action_out(compensation)


async def schedule_compensations_for_batch(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    batch_id: uuid.UUID,
    actor: str,
) -> int:
    """Create approved compensation actions for succeeded actions in a batch."""
    actions = (
        await session.execute(
            select(ExternalAction).where(
                ExternalAction.workspace_id == workspace_id,
                ExternalAction.session_batch_id == batch_id,
                ExternalAction.status == "SUCCEEDED",
                ExternalAction.compensation_payload.is_not(None),
            )
        )
    ).scalars().all()
    created = 0
    for action in actions:
        existing = await session.scalar(
            select(ExternalAction.id).where(
                ExternalAction.workspace_id == workspace_id,
                ExternalAction.idempotency_key == f"comp:{action.id}",
            )
        )
        if existing is not None:
            continue
        session.add(
            ExternalAction(
                workspace_id=workspace_id,
                action_type=f"compensation:{action.action_type}"[:64],
                target_url=action.target_url,
                method=action.method,
                headers=action.headers,
                payload=action.compensation_payload or {},
                idempotency_key=f"comp:{action.id}",
                status="APPROVED",
                attempts=0,
                max_attempts=settings.outbound_max_attempts,
                approved_by=actor,
                approved_at=utcnow(),
                next_attempt_at=utcnow(),
                compensates_action_id=action.id,
                namespace=action.namespace,
                session_batch_id=batch_id,
                created_by=actor,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        action.status = "COMPENSATION_PENDING"
        action.updated_at = utcnow()
        created += 1
    return created


async def process_actions(session: AsyncSession, limit: int = 50) -> int:
    now = await db_now(session)
    actions = (
        await session.execute(
            select(ExternalAction)
            .where(
                ExternalAction.status.in_(["APPROVED", "FAILED"]),
                ExternalAction.attempts < ExternalAction.max_attempts,
                ExternalAction.next_attempt_at.is_not(None),
                ExternalAction.next_attempt_at <= now,
            )
            .with_for_update(skip_locked=True)
            .order_by(ExternalAction.next_attempt_at.asc())
            .limit(limit)
        )
    ).scalars().all()
    completed = 0
    for action in actions:
        action.status = "EXECUTING"
        action.updated_at = utcnow()
        await session.flush()
        try:
            await validate_target(action.target_url)
            result = await send_json(
                action.target_url,
                action.payload or {},
                headers=action.headers or {},
                method=action.method,
                idempotency_key=f"ag-action-{action.id}",
            )
        except ApiError as exc:
            result = None
            error = f"{exc.code}: {exc.message}"
        except Exception as exc:  # noqa: BLE001 - outbound boundary
            result = None
            error = str(exc)
        action.attempts += 1
        attempt = ExternalActionAttempt(
            workspace_id=action.workspace_id,
            action_id=action.id,
            attempt=action.attempts,
            status="SUCCEEDED" if result and result.ok else "FAILED",
            response_status=result.status_code if result else None,
            error=None if result and result.ok else (
                result.error if result else error
            ),
            duration_ms=result.duration_ms if result else 0,
            created_at=utcnow(),
        )
        session.add(attempt)
        if result and result.ok:
            action.status = "SUCCEEDED"
            action.response_status = result.status_code
            action.response_body = result.body
            action.last_error = None
            action.completed_at = utcnow()
            action.updated_at = utcnow()
            if action.compensates_action_id:
                original = await session.get(
                    ExternalAction, action.compensates_action_id
                )
                if original is not None:
                    original.status = "COMPENSATED"
                    original.updated_at = utcnow()
            await record_usage(
                session,
                workspace_id=action.workspace_id,
                agent_signature=action.created_by,
                category="external_action",
                amount=1,
                metadata={"action_id": str(action.id), "status": "SUCCEEDED"},
            )
            await append_event(
                session,
                workspace_id=action.workspace_id,
                agent="external_action_dispatcher",
                event_type="EXTERNAL_ACTION_SUCCEEDED",
                payload={
                    "action_id": str(action.id),
                    "action_type": action.action_type,
                    "status": result.status_code,
                },
                namespace=action.namespace,
            )
            completed += 1
        else:
            action.response_status = result.status_code if result else None
            action.last_error = (
                result.error if result else error
            ) or "outbound action failed"
            if action.attempts >= action.max_attempts:
                action.status = "FAILED"
                action.completed_at = utcnow()
            else:
                action.status = "APPROVED"
                delay = min(5 * (2 ** (action.attempts - 1)), 3_600)
                action.next_attempt_at = now + timedelta(seconds=delay)
            action.updated_at = utcnow()
            await append_event(
                session,
                workspace_id=action.workspace_id,
                agent="external_action_dispatcher",
                event_type="EXTERNAL_ACTION_FAILED",
                payload={
                    "action_id": str(action.id),
                    "attempts": action.attempts,
                    "error": action.last_error,
                },
                namespace=action.namespace,
            )
    await session.commit()
    return completed


async def external_action_loop() -> None:
    import asyncio

    while True:
        try:
            async with async_session_factory() as session:
                completed = await process_actions(session)
                if completed:
                    logger.info("completed %s external action(s)", completed)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - background resilience
            logger.exception("external action dispatcher iteration failed")
        await asyncio.sleep(settings.outbound_dispatch_interval_seconds)
