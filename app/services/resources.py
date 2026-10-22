"""Scarce resources: mutual exclusion and FIFO queues (PRD AG-CO-07).

A human defines a resource (``capacity`` slots, ``max_hold_seconds``); agents
take one slot at a time as a lease from the unified lease table
(``resource_kind='resource'``, one stable ``resource_id`` per slot, so the
"one active lease per resource" index enforces the capacity). When every slot
is taken a caller may queue: the head of the queue gets ``resource.granted``
and has ``OFFER_SECONDS`` to confirm with its ``queue_ticket``, or the offer
moves on. Heartbeats and expiry reuse AG-CO-03.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, effective_agent, require_permission
from app.errors import ApiError, Forbidden, NotFound
from app.models import Lease
from app.services.common import append_event, db_now

OFFER_SECONDS = 60
WAIT_MAX = 25
_POLL = 0.5
_KEY = r"^[a-z0-9][a-z0-9._-]{0,127}$"
RESOURCE_NAMESPACE = "system"


def _slot_ids(resource_id: uuid.UUID, capacity: int) -> list[uuid.UUID]:
    return [uuid.uuid5(resource_id, f"slot-{index}") for index in range(capacity)]


def _resource_out(row: Any, *, held: int = 0, queued: int = 0) -> dict[str, Any]:
    return {"resource_key": row["resource_key"], "capacity": row["capacity"],
            "description": row["description"], "max_hold_seconds": row["max_hold_seconds"],
            "held": held, "queued": queued, "created_by": row["created_by"],
            "created_at": row["created_at"].isoformat()}


async def define_resource(session: AsyncSession, principal: Principal, *, resource_key: str,
                          capacity: int, description: str | None,
                          max_hold_seconds: int) -> dict[str, Any]:
    import re

    from app.services.onboarding import write_platform_document

    if not principal.is_human:
        raise Forbidden("only a human may define a resource")
    if not re.match(_KEY, resource_key or ""):
        raise ApiError(422, "validation_error",
                       "resource_key is lowercase letters, digits, '.', '_' or '-'",
                       details={"field": "resource_key"})
    if not 1 <= capacity <= 100 or not 1 <= max_hold_seconds <= 3600:
        raise ApiError(422, "validation_error",
                       "capacity is 1-100 and max_hold_seconds is 1-3600",
                       details={"field": "capacity" if not 1 <= capacity <= 100
                                else "max_hold_seconds"})
    row = (await session.execute(text(
        "INSERT INTO resources (workspace_id, resource_key, capacity, description, "
        "max_hold_seconds, created_by) VALUES (:ws, :k, :c, :d, :m, :by) "
        "ON CONFLICT (workspace_id, resource_key) DO UPDATE SET capacity = EXCLUDED.capacity, "
        "description = EXCLUDED.description, max_hold_seconds = EXCLUDED.max_hold_seconds "
        "RETURNING *"),
        {"ws": principal.workspace.id, "k": resource_key, "c": capacity, "d": description,
         "m": max_hold_seconds, "by": f"human:{principal.user_id}"})).mappings().one()
    await write_platform_document(
        session, principal.workspace.id, namespace=RESOURCE_NAMESPACE,
        doc_key=f"system/resources/{resource_key}", title=f"Resource: {resource_key}",
        content=(f"# Resource `{resource_key}`\n\n{description or ''}\n\n"
                 f"- Capacity: {capacity}\n- Longest hold: {max_hold_seconds} s\n\n"
                 "Take it with `acquire_resource`; give it back with `release_resource`.\n"),
        frontmatter={"kind": "resource", "resource_key": resource_key, "capacity": capacity,
                     "max_hold_seconds": max_hold_seconds},
        author=principal.agent_signature, change_summary="resource defined")
    await session.commit()
    return _resource_out(row)


async def _load(session: AsyncSession, workspace_id: uuid.UUID, key: str, *,
                lock: bool) -> Any:
    row = (await session.execute(text(
        "SELECT * FROM resources WHERE workspace_id = :ws AND resource_key = :k"
        + (" FOR UPDATE" if lock else "")), {"ws": workspace_id, "k": key})
    ).mappings().one_or_none()
    if row is None:
        raise NotFound("resource", f"resource '{key}' is not defined")
    return row


async def list_resources(session: AsyncSession, principal: Principal) -> dict[str, Any]:
    require_permission(principal, "task:read")
    rows = (await session.execute(text(
        "SELECT r.*, (SELECT count(*) FROM leases l WHERE l.resource_kind = 'resource' "
        "AND l.resource_ref = r.resource_key AND l.workspace_id = r.workspace_id "
        "AND l.state = 'active' AND l.expires_at > NOW()) AS held, "
        "(SELECT count(*) FROM resource_queue q WHERE q.resource_id = r.id "
        "AND q.state IN ('waiting', 'offered')) AS queued "
        "FROM resources r WHERE r.workspace_id = :ws ORDER BY r.resource_key"),
        {"ws": principal.workspace.id})).mappings().all()
    return {"items": [_resource_out(row, held=row["held"], queued=row["queued"])
                      for row in rows]}


async def _expire(session: AsyncSession, resource: Any, now: Any) -> None:
    await session.execute(text(
        "UPDATE leases SET state = 'expired', ended_at = :now, end_reason = 'ttl_expired' "
        "WHERE resource_kind = 'resource' AND workspace_id = :ws AND resource_ref = :k "
        "AND state = 'active' AND expires_at <= :now"),
        {"now": now, "ws": resource["workspace_id"], "k": resource["resource_key"]})
    await session.execute(text(
        "UPDATE resource_queue SET state = 'abandoned' WHERE resource_id = :r "
        "AND state = 'offered' AND offered_at <= :cut"),
        {"r": resource["id"], "cut": now - timedelta(seconds=OFFER_SECONDS)})


async def _free_slots(session: AsyncSession, resource: Any) -> list[uuid.UUID]:
    slots = _slot_ids(resource["id"], resource["capacity"])
    taken = set((await session.execute(
        select(Lease.resource_id).where(Lease.resource_kind == "resource",
                                        Lease.resource_id.in_(slots),
                                        Lease.state == "active"))).scalars().all())
    return [slot for slot in slots if slot not in taken]


async def _live_queue(session: AsyncSession, resource: Any) -> list[Any]:
    return list((await session.execute(text(
        "SELECT * FROM resource_queue WHERE resource_id = :r AND state IN "
        "('waiting', 'offered') ORDER BY created_at, ticket"),
        {"r": resource["id"]})).mappings().all())


async def _grant(session: AsyncSession, principal: Principal, resource: Any,
                 slot: uuid.UUID, agent: str, ttl: int, now: Any) -> Lease:
    fencing = await session.scalar(
        select(func.coalesce(func.max(Lease.fencing_token), 0)).where(
            Lease.resource_kind == "resource", Lease.workspace_id == resource["workspace_id"],
            Lease.resource_ref == resource["resource_key"]))
    lease = Lease(workspace_id=resource["workspace_id"], resource_kind="resource",
                  resource_id=slot, resource_ref=resource["resource_key"],
                  fencing_token=int(fencing) + 1, holder=agent,
                  holder_token_id=principal.token_id, acquired_at=now,
                  expires_at=now + timedelta(seconds=ttl), ttl_seconds=ttl,
                  heartbeat_interval_s=max(1, ttl // 3), last_heartbeat_at=now)
    session.add(lease)
    await session.flush()
    await session.refresh(lease, ["lease_id"])
    return lease


async def advance(session: AsyncSession, resource: Any, now: Any) -> int:
    """Offer free slots to the head of the queue; returns how many were offered."""
    free = len(await _free_slots(session, resource))
    queue = await _live_queue(session, resource)
    free -= sum(1 for row in queue if row["state"] == "offered")
    offered = 0
    for row in queue:
        if free <= 0:
            break
        if row["state"] != "waiting":
            continue
        await session.execute(text(
            "UPDATE resource_queue SET state = 'offered', offered_at = :now "
            "WHERE ticket = :t"), {"now": now, "t": row["ticket"]})
        await append_event(
            session, workspace_id=resource["workspace_id"], agent="resource_queue",
            event_type="RESOURCE_GRANTED",
            payload={"targets": [f"agent:{row['holder']}"],
                     "details": {"resource_key": resource["resource_key"],
                                 "queue_ticket": row["ticket"],
                                 "confirm_within_s": OFFER_SECONDS}})
        free -= 1
        offered += 1
    return offered


def _out(lease: Lease | None = None, *, position: int | None = None,
         ticket: str | None = None, state: str | None = None) -> dict[str, Any]:
    if lease is not None:
        return {"granted": True, "lease_id": lease.lease_id,
                "fencing_token": lease.fencing_token,
                "expires_at": lease.expires_at.isoformat(), "ttl_seconds": lease.ttl_seconds,
                "heartbeat_interval_s": lease.heartbeat_interval_s,
                "queue_position": None, "queue_ticket": None}
    return {"granted": False, "lease_id": None, "queue_position": position,
            "queue_ticket": ticket, "queue_state": state}


async def _try(session: AsyncSession, principal: Principal, key: str, agent: str, ttl: int,
               ticket: str | None) -> dict[str, Any] | None:
    """One attempt under the resource lock; None means "full, not queued yet"."""
    resource = await _load(session, principal.workspace.id, key, lock=True)
    if ttl > resource["max_hold_seconds"]:
        raise ApiError(422, "validation_error",
                       f"ttl_seconds is at most {resource['max_hold_seconds']} for this resource",
                       details={"field": "ttl_seconds", "limit": resource["max_hold_seconds"]})
    now = await db_now(session)
    await _expire(session, resource, now)
    await advance(session, resource, now)
    queue = await _live_queue(session, resource)
    if ticket is not None:
        mine = await session.execute(text(
            "SELECT * FROM resource_queue WHERE ticket = :t AND resource_id = :r"),
            {"t": ticket, "r": resource["id"]})
        row = mine.mappings().one_or_none()
        if row is None or row["holder"] != agent:
            raise ApiError(404, "not_found", "queue ticket not found",
                           details={"queue_ticket": ticket})
        if row["state"] in ("abandoned", "cancelled", "granted"):
            raise ApiError(409, "queue_ticket_expired",
                           "this queue ticket is no longer live; acquire again",
                           details={"queue_ticket": ticket, "state": row["state"]})
        if row["state"] == "offered":
            free = await _free_slots(session, resource)
            if free:
                lease = await _grant(session, principal, resource, free[0], agent, ttl, now)
                await session.execute(text(
                    "UPDATE resource_queue SET state = 'granted' WHERE ticket = :t"),
                    {"t": ticket})
                return _out(lease)
        position = [item["ticket"] for item in queue].index(ticket) + 1
        return _out(position=position, ticket=ticket, state=row["state"])
    free = await _free_slots(session, resource)
    reserved = sum(1 for row in queue if row["state"] == "offered")
    waiting = any(row["state"] == "waiting" for row in queue)
    if len(free) > reserved and not waiting:
        return _out(await _grant(session, principal, resource, free[0], agent, ttl, now))
    return None


async def acquire_resource(session: AsyncSession, principal: Principal, *, resource_key: str,
                           ttl_seconds: int, wait_seconds: int = 0, queue: bool = True,
                           queue_ticket: str | None = None,
                           agent: str | None = None) -> dict[str, Any]:
    require_permission(principal, "task:claim")
    holder = effective_agent(principal, agent)
    if not 1 <= ttl_seconds <= 3600:
        raise ApiError(422, "validation_error", "ttl_seconds is 1-3600",
                       details={"field": "ttl_seconds"})
    if not 0 <= wait_seconds <= WAIT_MAX:
        raise ApiError(422, "validation_error", f"wait_seconds is 0-{WAIT_MAX}",
                       details={"field": "wait_seconds", "limit": WAIT_MAX})
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_seconds
    while True:
        result = await _try(session, principal, resource_key, holder, ttl_seconds,
                            queue_ticket)
        if result is not None and (result["granted"] or loop.time() >= deadline):
            await session.commit()
            return result
        if result is None and loop.time() >= deadline:
            break
        await session.commit()
        await asyncio.sleep(_POLL)
    if not queue:
        await session.commit()
        return _out()
    resource = await _load(session, principal.workspace.id, resource_key, lock=True)
    existing = await session.execute(text(
        "SELECT ticket FROM resource_queue WHERE resource_id = :r AND holder = :h "
        "AND state IN ('waiting', 'offered')"), {"r": resource["id"], "h": holder})
    ticket = existing.scalar() or await session.scalar(text(
        "INSERT INTO resource_queue (workspace_id, resource_id, holder, ttl_seconds) "
        "VALUES (:ws, :r, :h, :ttl) RETURNING ticket"),
        {"ws": principal.workspace.id, "r": resource["id"], "h": holder, "ttl": ttl_seconds})
    queue_rows = await _live_queue(session, resource)
    position = [row["ticket"] for row in queue_rows].index(ticket) + 1
    await session.commit()
    return _out(position=position, ticket=ticket, state="waiting")


async def release_lease(session: AsyncSession, lease: Lease, *, reason: str = "released") -> None:
    """End a resource lease and offer the slot to the queue (caller commits)."""
    now = await db_now(session)
    resource = await _load(session, lease.workspace_id, lease.resource_ref, lock=True)
    if lease.state == "active":
        lease.state = "released" if reason == "released" else "revoked"
        lease.ended_at = now
        lease.end_reason = reason
        await session.flush()
    await advance(session, resource, now)


async def release_resource(session: AsyncSession, principal: Principal, lease_id: str, *,
                           agent: str | None = None) -> dict[str, Any]:
    from app.services.leases import _get_lease, lease_out

    require_permission(principal, "task:claim")
    actor = effective_agent(principal, agent)
    lease = await _get_lease(session, principal.workspace.id, lease_id)
    if lease.resource_kind != "resource":
        raise ApiError(422, "validation_error", "this lease is not on a resource",
                       details={"lease_id": lease_id, "resource_kind": lease.resource_kind})
    if lease.holder != actor and not principal.is_human:
        raise ApiError(403, "not_lease_holder", "only the lease holder may release it",
                       details={"lease_id": lease_id, "holder": lease.holder})
    await release_lease(session, lease)
    await session.commit()
    return lease_out(lease).model_dump(mode="json")


async def reap(session: AsyncSession) -> int:
    """Expire overdue holds and stale offers, then move each queue on (platform session)."""
    now = await db_now(session)
    rows = (await session.execute(text(
        "SELECT DISTINCT r.* FROM resources r WHERE EXISTS (SELECT 1 FROM leases l "
        "WHERE l.resource_kind = 'resource' AND l.workspace_id = r.workspace_id AND "
        "l.resource_ref = r.resource_key AND l.state = 'active' AND l.expires_at <= :now) "
        "OR EXISTS (SELECT 1 FROM resource_queue q WHERE q.resource_id = r.id AND ("
        "(q.state = 'offered' AND q.offered_at <= :cut) OR q.state = 'waiting')) LIMIT 200"),
        {"now": now, "cut": now - timedelta(seconds=OFFER_SECONDS)})).mappings().all()
    moved = 0
    for row in rows:
        resource = (await session.execute(text(
            "SELECT * FROM resources WHERE id = :id FOR UPDATE SKIP LOCKED"),
            {"id": row["id"]})).mappings().one_or_none()
        if resource is None:
            continue
        await _expire(session, resource, now)
        moved += await advance(session, resource, now)
    await session.commit()
    return moved
