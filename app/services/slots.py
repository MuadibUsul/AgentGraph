"""Live state grid operations: JSON Patch, TTL leases, human sovereignty."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from jsonpointer import JsonPointerException, resolve_pointer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    Principal,
    effective_agent,
    ensure_namespace,
    ensure_not_human_locked,
    principal_for_admin,
    require_permission,
)
from app.config import get_settings
from app.engine.blackboard import apply_json_patch, lease_is_active
from app.errors import (
    LeaseConflict,
    NotFound,
    QuotaExceeded,
    RebaseConflict,
    VersionConflict,
)
from app.models import ContextSlot, StateAuditDiff, Workspace
from app.schemas import (
    HumanSlotUpdateRequest,
    SlotListResponse,
    SlotMutationResponse,
    SlotOut,
    SlotPatchRequest,
    SlotReplaceRequest,
)
from app.services.common import (
    append_event,
    db_now,
    record_audit,
    slot_out,
    slot_snapshot,
)
from app.services.policies import enforce_policy
from app.services.taint import refresh_slot_taint

settings = get_settings()


async def _ensure_slot_quota(session: AsyncSession, workspace: Workspace) -> None:
    _, limit = settings.tier_limits(workspace.tier)
    used = await session.scalar(
        select(func.count(ContextSlot.id)).where(
            ContextSlot.workspace_id == workspace.id
        )
    )
    if (used or 0) >= limit:
        raise QuotaExceeded("context_slots", limit)


async def _get_slot(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    slot_key: str,
    *,
    for_update: bool = False,
) -> ContextSlot:
    query = select(ContextSlot).where(
        ContextSlot.workspace_id == workspace_id,
        ContextSlot.slot_key == slot_key,
    )
    if for_update:
        query = query.with_for_update()
    slot = (await session.execute(query)).scalar_one_or_none()
    if slot is None:
        raise NotFound("context_slot", f"slot '{slot_key}' not found")
    return slot


def _check_lease(slot: ContextSlot, agent: str, now: Any | None = None) -> None:
    if (
        slot.locked_by
        and slot.locked_by != agent
        and lease_is_active(slot.locked_by, slot.lock_expires_at, now)
    ):
        raise LeaseConflict("context_slot", slot_out(slot).model_dump(mode="json"))


def _check_epoch(
    slot: ContextSlot, agent: str, expected: int | None, now: Any
) -> None:
    if (
        expected is not None
        and expected != slot.lease_epoch
        and slot.locked_by == agent
        and lease_is_active(slot.locked_by, slot.lock_expires_at, now)
    ):
        raise LeaseConflict("context_slot", slot_out(slot).model_dump(mode="json"))


def _set_lease(
    slot: ContextSlot, agent: str, seconds: int | None, now: Any
) -> None:
    if seconds is not None:
        slot.locked_by = agent
        slot.lock_expires_at = now + timedelta(seconds=seconds)
        slot.lease_epoch += 1


async def list_slots(
    session: AsyncSession,
    principal: Principal,
    *,
    namespace: str | None = None,
    limit: int = 100,
) -> SlotListResponse:
    require_permission(principal, "state:read")
    query = select(ContextSlot).where(
        ContextSlot.workspace_id == principal.workspace.id
    )
    if namespace:
        ensure_namespace(principal, namespace)
        query = query.where(ContextSlot.namespace == namespace)
    elif not principal.is_commander and "*" not in principal.allowed_namespaces:
        query = query.where(ContextSlot.namespace.in_(principal.allowed_namespaces))
    slots = (
        await session.execute(
            query.order_by(ContextSlot.updated_at.desc()).limit(min(limit, 500))
        )
    ).scalars().all()
    return SlotListResponse(items=[slot_out(slot) for slot in slots])


async def get_slot(
    session: AsyncSession, principal: Principal, slot_key: str
) -> SlotOut:
    require_permission(principal, "state:read")
    slot = await _get_slot(session, principal.workspace.id, slot_key)
    ensure_namespace(principal, slot.namespace)
    return slot_out(slot)


async def replace_slot(
    session: AsyncSession,
    principal: Principal,
    slot_key: str,
    request: SlotReplaceRequest,
) -> SlotMutationResponse:
    require_permission(principal, "state:write")
    agent = effective_agent(principal, request.agent)
    batch = request.session_batch_id or uuid.uuid4()
    existing = (
        await session.execute(
            select(ContextSlot).where(
                ContextSlot.workspace_id == principal.workspace.id,
                ContextSlot.slot_key == slot_key,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        if request.expected_version != 0:
            raise VersionConflict("context_slot", None)
        namespace = request.namespace or "default"
        ensure_namespace(principal, namespace, write=True)
        await enforce_policy(
            session,
            principal,
            resource_type="slot",
            key=slot_key,
            action="create",
            payload=request.state,
            content_bytes=len(str(request.state).encode("utf-8")),
        )
        await _ensure_slot_quota(session, principal.workspace)
        now = await db_now(session)
        slot = ContextSlot(
            workspace_id=principal.workspace.id,
            slot_key=slot_key,
            goal=request.goal,
            current_state=request.state,
            schema_hint=request.schema_hint or {},
            blockers=request.blockers or [],
            namespace=namespace,
            version=1,
            lease_epoch=0,
            locked_by=None,
            lock_expires_at=None,
            locked_by_human=False,
            human_note=None,
            updated_by=agent,
            updated_at=now,
        )
        _set_lease(slot, agent, request.lease_seconds, now)
        session.add(slot)
        await session.flush()
        await refresh_slot_taint(session, slot)
        await record_audit(
            session,
            workspace_id=principal.workspace.id,
            session_batch_id=batch,
            agent_signature=agent,
            target_table="context_slots",
            target_record_id=slot.id,
            operation="INSERT",
            before={},
            after=slot_snapshot(slot),
        )
        await append_event(
            session,
            workspace_id=principal.workspace.id,
            agent=agent,
            event_type="SLOT_CREATED",
            payload={"slot_key": slot_key, "version": 1, "namespace": slot.namespace},
            namespace=slot.namespace,
        )
        await session.commit()
        return SlotMutationResponse(slot=slot_out(slot), session_batch_id=batch)

    ensure_namespace(principal, existing.namespace, write=True)
    ensure_not_human_locked(
        principal, "context_slot", existing.locked_by_human, existing.human_note
    )
    if request.expected_version != existing.version:
        raise VersionConflict("context_slot", slot_out(existing).model_dump(mode="json"))
    await enforce_policy(
        session,
        principal,
        resource_type="slot",
        key=slot_key,
        action="write",
        payload=request.state,
        content_bytes=len(str(request.state).encode("utf-8")),
    )
    now = await db_now(session)
    _check_lease(existing, agent, now)
    _check_epoch(existing, agent, request.lease_epoch, now)
    slot = await _get_slot(
        session, principal.workspace.id, slot_key, for_update=True
    )
    if slot.version != request.expected_version:
        raise VersionConflict("context_slot", slot_out(slot).model_dump(mode="json"))
    ensure_not_human_locked(
        principal, "context_slot", slot.locked_by_human, slot.human_note
    )
    before = slot_snapshot(slot)
    slot.current_state = request.state
    if request.goal is not None:
        slot.goal = request.goal
    if request.blockers is not None:
        slot.blockers = request.blockers
    if request.schema_hint is not None:
        slot.schema_hint = request.schema_hint
    if request.namespace is not None and request.namespace != slot.namespace:
        ensure_namespace(principal, request.namespace, write=True)
        slot.namespace = request.namespace
    _set_lease(slot, agent, request.lease_seconds, now)
    slot.version += 1
    slot.updated_by = agent
    slot.updated_at = now
    await session.flush()
    await refresh_slot_taint(session, slot)
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=agent,
        target_table="context_slots",
        target_record_id=slot.id,
        operation="UPDATE",
        before=before,
        after=slot_snapshot(slot),
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=agent,
        event_type="SLOT_UPDATED",
        payload={"slot_key": slot_key, "version": slot.version},
        namespace=slot.namespace,
    )
    await session.commit()
    return SlotMutationResponse(slot=slot_out(slot), session_batch_id=batch)


_MISSING = object()


def _resolve_state(state: Any, path: str) -> Any:
    try:
        return resolve_pointer(state, path, default=_MISSING)
    except JsonPointerException:
        return _MISSING


async def _slot_base_state(
    session: AsyncSession, slot: ContextSlot, expected_version: int
) -> dict[str, Any] | None:
    entry = (
        await session.execute(
            select(StateAuditDiff)
            .where(
                StateAuditDiff.workspace_id == slot.workspace_id,
                StateAuditDiff.target_table == "context_slots",
                StateAuditDiff.target_record_id == slot.id,
                StateAuditDiff.after_diff["version"].astext
                == str(expected_version),
            )
            .order_by(StateAuditDiff.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if entry is None:
        return None
    current_state = (entry.after_diff or {}).get("current_state")
    return current_state if isinstance(current_state, dict) else None


def _state_rebase_conflicts(
    base_state: dict[str, Any],
    current_state: dict[str, Any],
    ops: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    paths: list[str] = []
    for op in ops:
        for field in ("path", "from"):
            value = op.get(field)
            if isinstance(value, str) and value not in paths:
                paths.append(value)
    conflicts: list[dict[str, Any]] = []
    for path in paths:
        base_value = _resolve_state(base_state, path)
        current_value = _resolve_state(current_state, path)
        if base_value is _MISSING:
            base_value = None
        if current_value is _MISSING:
            current_value = None
        if base_value != current_value:
            conflicts.append(
                {
                    "field": path,
                    "reason": "the JSON Patch path changed after base_version",
                    "base": base_value,
                    "current": current_value,
                }
            )
    return conflicts


async def patch_slot(
    session: AsyncSession,
    principal: Principal,
    slot_key: str,
    request: SlotPatchRequest,
) -> SlotMutationResponse:
    require_permission(principal, "state:write")
    agent = effective_agent(principal, request.agent)
    batch = request.session_batch_id or uuid.uuid4()
    slot = await _get_slot(
        session, principal.workspace.id, slot_key, for_update=True
    )
    ensure_namespace(principal, slot.namespace, write=True)
    ensure_not_human_locked(
        principal, "context_slot", slot.locked_by_human, slot.human_note
    )
    rebased = False
    if slot.version != request.expected_version:
        if not request.rebase:
            raise VersionConflict(
                "context_slot", slot_out(slot).model_dump(mode="json")
            )
        base_state = await _slot_base_state(
            session, slot, request.expected_version
        )
        if base_state is None:
            raise RebaseConflict(
                "context_slot",
                [
                    {
                        "field": "base_version",
                        "reason": "base version is not in the audit history",
                    }
                ],
            )
        conflicts = _state_rebase_conflicts(
            base_state, slot.current_state or {}, request.ops
        )
        if conflicts:
            raise RebaseConflict("context_slot", conflicts)
        rebased = True
    now = await db_now(session)
    _check_lease(slot, agent, now)
    _check_epoch(slot, agent, request.lease_epoch, now)
    before = slot_snapshot(slot)
    state, applied = apply_json_patch(slot.current_state or {}, request.ops)
    await enforce_policy(
        session,
        principal,
        resource_type="slot",
        key=slot_key,
        action="write",
        payload=state,
        content_bytes=len(str(state).encode("utf-8")),
    )
    slot.current_state = state
    _set_lease(slot, agent, request.lease_seconds, now)
    slot.version += 1
    slot.updated_by = agent
    slot.updated_at = now
    await session.flush()
    await refresh_slot_taint(session, slot)
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=agent,
        target_table="context_slots",
        target_record_id=slot.id,
        operation="UPDATE",
        before=before,
        after=slot_snapshot(slot),
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=agent,
        event_type="SLOT_PATCHED",
        payload={
            "slot_key": slot_key,
            "version": slot.version,
            "rebased": rebased,
            "ops": [
                {"op": op.get("op"), "path": op.get("path")}
                for op in applied
            ],
        },
        namespace=slot.namespace,
    )
    await session.commit()
    return SlotMutationResponse(
        slot=slot_out(slot), applied_ops=applied, session_batch_id=batch
    )


async def human_update_slot(
    session: AsyncSession,
    workspace: Workspace,
    slot_key: str,
    request: HumanSlotUpdateRequest,
    *,
    actor: str = "human_commander",
) -> SlotMutationResponse:
    batch = request.session_batch_id or uuid.uuid4()
    slot = await _get_slot(session, workspace.id, slot_key, for_update=True)
    if slot.version != request.expected_version:
        raise VersionConflict("context_slot", slot_out(slot).model_dump(mode="json"))
    if request.state is not None:
        await enforce_policy(
            session,
            principal_for_admin(workspace),
            resource_type="slot",
            key=slot_key,
            action="write",
            payload=request.state,
            content_bytes=len(str(request.state).encode("utf-8")),
        )
    now = await db_now(session)
    before = slot_snapshot(slot)
    if request.state is not None:
        slot.current_state = request.state
    if request.goal is not None:
        slot.goal = request.goal
    if request.blockers is not None:
        slot.blockers = request.blockers
    if request.schema_hint is not None:
        slot.schema_hint = request.schema_hint
    if request.human_note is not None:
        slot.human_note = request.human_note
    slot.locked_by_human = (
        False if request.unlock else (request.lock if request.lock is not None else True)
    )
    slot.locked_by = None
    slot.lock_expires_at = None
    slot.lease_epoch += 1
    slot.version += 1
    slot.updated_by = actor
    slot.updated_at = now
    await session.flush()
    await refresh_slot_taint(session, slot)
    await record_audit(
        session,
        workspace_id=workspace.id,
        session_batch_id=batch,
        agent_signature=actor,
        target_table="context_slots",
        target_record_id=slot.id,
        operation="UPDATE",
        before=before,
        after=slot_snapshot(slot),
    )
    await append_event(
        session,
        workspace_id=workspace.id,
        agent=actor,
        event_type="HUMAN_OVERRIDE",
        payload={
            "resource": "context_slot",
            "slot_key": slot_key,
            "version": slot.version,
            "locked_by_human": slot.locked_by_human,
            "note": request.human_note,
        },
        namespace=slot.namespace,
    )
    await session.commit()
    return SlotMutationResponse(slot=slot_out(slot), session_batch_id=batch)


async def reap_expired_slot_leases(
    session: AsyncSession, limit: int = 100
) -> int:
    now = await db_now(session)
    slots = (
        await session.execute(
            select(ContextSlot)
            .where(
                ContextSlot.lock_expires_at.is_not(None),
                ContextSlot.lock_expires_at < now,
                ContextSlot.locked_by.is_not(None),
            )
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
    ).scalars().all()
    for slot in slots:
        slot.locked_by = None
        slot.lock_expires_at = None
        slot.lease_epoch += 1
        slot.version += 1
        slot.updated_by = "lease_reaper"
        slot.updated_at = now
    if slots:
        await session.commit()
    return len(slots)
