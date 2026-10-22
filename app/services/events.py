"""Unified event log: poll_events, subscriptions, sync changes (PRD AG-EV-01/02).

Every change is one ``workspace_events`` row written by the outbox trigger in
the writer's transaction, with a per-workspace ``seq`` that is gap-free and
in commit order. Events carry pointers (refs), never bodies. One opaque
cursor format (it encodes a seq) serves ``poll_events`` and
``workspace_sync(since=...)``; delivery is at least once, so consumers
deduplicate by ``event_id``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, effective_agent, require_permission
from app.config import get_settings
from app.database import async_session_factory
from app.errors import ApiError, NotFound
from app.models import EventSubscription, SharedDocument, Tombstone, WorkspaceEvent

settings = get_settings()

POLL_WAIT_MAX = 25
POLL_LIMIT_MAX = 500
SUBSCRIPTION_PREFIXES_MAX = 50
_POLL_INTERVAL = 0.2

# Every type the outbox trigger can emit (sql/init.sql, notify_workspace_change).
# timeline.<name> carries application events that repeat a row change.
EVENT_TYPES: dict[str, str] = {
    "doc.created": "A document was created.",
    "doc.updated": "A new version of a document.",
    "doc.renamed": "A document's alias changed (the id did not).",
    "doc.archived": "A document was archived.",
    "doc.restored": "An archived document was restored.",
    "doc.locked": "A human locked a document.",
    "doc.unlocked": "A human unlocked a document.",
    "doc.trust_changed": "Verification or taint state changed without a new version.",
    "doc.indexed": "A version became searchable (AG-ST-13).",
    "doc.index_failed": "Indexing failed after retries; the document stays readable.",
    "doc.purged": "A document was purged; a tombstone remains.",
    "task.created": "A task was created.",
    "task.assigned": "A task was assigned (targets: the assignee).",
    "task.claimed": "A task was claimed under a new lease.",
    "task.status_changed": "A task changed status.",
    "task.updated": "A task changed without a status change.",
    "task.handoff": "A task was handed off.",
    "task.reviewed": "A task was approved or rejected.",
    "task.heartbeat_missed": "A lease holder missed a heartbeat (targets: the holder).",
    "task.lease_expired": "A lease expired; the task is back on the board.",
    "escalation.created": "A task was escalated to a human.",
    "human_review.requested": "A human review was requested.",
    "proposal.created": "A change proposal was created.",
    "proposal.voted": "A proposal received a vote.",
    "proposal.resolved": "A proposal was applied or rejected.",
    "proposal.updated": "A proposal changed.",
    "slot.created": "A state slot was created.",
    "slot.updated": "A state slot changed.",
    "action.created": "An external action was drafted.",
    "action.updated": "An external action changed state.",
    "budget.circuit_open": "Writes from a token are paused by a circuit breaker.",
    "responsibilities.transferred": "Open work moved to a successor.",
    "security.incident_notice": "A security incident notice for this organisation "
                                "(targets: the notified owners).",
    "timeline.*": "Application event that repeats a row change (e.g. timeline.task_claimed).",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ cursors


def encode_cursor(seq: int) -> str:
    raw = json.dumps({"v": 1, "seq": int(seq)}, separators=(",", ":")).encode()
    return "ev1." + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int | None:
    """The seq an event cursor points at, or None if it is not an event cursor."""
    if not cursor or not cursor.startswith("ev1."):
        return None
    body = cursor[4:]
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return int(payload["seq"])
    except Exception as exc:  # noqa: BLE001 - malformed cursors are the client's
        raise ApiError(400, "cursor_invalid", "event cursor is not valid",
                       details={"field": "cursor"}) from exc


async def current_seq(session: AsyncSession, workspace_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            text("SELECT seq FROM workspace_event_seq WHERE workspace_id = :ws"),
            {"ws": workspace_id},
        ) or 0
    )


async def _oldest_seq(session: AsyncSession, workspace_id: uuid.UUID) -> int | None:
    return await session.scalar(
        select(func.min(WorkspaceEvent.seq)).where(WorkspaceEvent.workspace_id == workspace_id)
    )


async def check_cursor(session: AsyncSession, workspace_id: uuid.UUID, seq: int) -> int:
    """410 when events after ``seq`` were already pruned; returns the oldest seq."""
    oldest = await _oldest_seq(session, workspace_id)
    head = await current_seq(session, workspace_id)
    if seq > head:
        raise ApiError(400, "cursor_invalid", "the cursor is ahead of this workspace's log",
                       details={"field": "cursor", "head_seq": head})
    floor = oldest if oldest is not None else head + 1
    if seq + 1 < floor:
        raise ApiError(410, "cursor_expired", "events after this cursor are no longer kept",
                       details={"oldest_available_seq": floor,
                                "retention_days": settings.event_retention_days,
                                "resync": "workspace_sync(since=null)"})
    return floor


# ------------------------------------------------------------------ filtering


def _identities(principal: Principal) -> list[str]:
    me = principal.agent_signature
    names = [me, f"agent:{me}", f"human:{me}"]
    if principal.user_id is not None:
        names.append(f"human:{principal.user_id}")
    return names


def _readable(principal: Principal) -> list[str] | None:
    if principal.is_commander or "*" in principal.allowed_namespaces:
        return None
    return list(principal.allowed_namespaces)


def _type_condition(types: list[str]) -> Any:
    clauses = []
    for item in types:
        if item.endswith("*"):
            clauses.append(WorkspaceEvent.type.like(item[:-1].replace("%", r"\%") + "%"))
        else:
            clauses.append(WorkspaceEvent.type == item)
    return or_(*clauses)


def _prefix_condition(prefixes: list[str]) -> Any:
    """``namespace/`` or ``namespace/key-prefix`` (also matched against full aliases)."""
    alias = func.coalesce(WorkspaceEvent.doc_key, WorkspaceEvent.task_key,
                          WorkspaceEvent.slot_key, "")
    clauses = []
    for prefix in prefixes:
        namespace, _, rest = prefix.partition("/")
        clauses.append(
            and_(WorkspaceEvent.shard_key == namespace, alias.like(_like(rest) + "%"))
        )
        clauses.append(alias.like(_like(prefix) + "%"))
    return or_(*clauses)


def _like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _targets_me(principal: Principal) -> Any:
    return WorkspaceEvent.targets.op("?|")(
        text("CAST(:me AS text[])").bindparams(me=_identities(principal))
    )


def event_out(event: WorkspaceEvent) -> dict[str, Any]:
    stamp = event.stamp or (event.payload or {}).get("actor_stamp") or {}
    return {
        "event_id": event.event_id,
        "seq": event.seq,
        "type": event.type,
        "at": event.created_at.isoformat(),
        "room_id": None,
        "namespace": event.namespace,
        "actor": {
            "principal_id": stamp.get("principal_id") or event.actor,
            "type": stamp.get("kind") or "system",
            "chain": stamp.get("chain") or [],
        },
        "targets": event.targets or [],
        "refs": event.refs or {},
        "summary": event.summary,
    }


# ------------------------------------------------------------------ subscriptions


def _validate_prefixes(prefixes: list[str]) -> list[str]:
    if len(prefixes) > SUBSCRIPTION_PREFIXES_MAX:
        raise ApiError(422, "validation_error",
                       f"at most {SUBSCRIPTION_PREFIXES_MAX} prefixes",
                       details={"field": "prefixes", "limit": SUBSCRIPTION_PREFIXES_MAX})
    cleaned = []
    for prefix in prefixes:
        prefix = prefix.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}(/[A-Za-z0-9_./\-]{0,256})?", prefix):
            raise ApiError(422, "validation_error",
                           "a prefix is namespace/ or namespace/doc_key_prefix",
                           details={"field": "prefixes", "value": prefix})
        cleaned.append(prefix if "/" in prefix else prefix + "/")
    return cleaned


def subscription_out(subscription: EventSubscription, cursor: str | None = None) -> dict:
    return {
        "subscription_id": subscription.id,
        "name": subscription.name,
        "prefixes": subscription.prefixes,
        "types": subscription.types,
        "is_default": subscription.is_default,
        "cursor": cursor,
        "created_at": subscription.created_at.isoformat() if subscription.created_at else None,
    }


async def subscribe(
    session: AsyncSession, principal: Principal, *, prefixes: list[str],
    types: list[str] | None, name: str, is_default: bool = False,
) -> dict[str, Any]:
    require_permission(principal, "events:read")
    prefixes = _validate_prefixes(prefixes)
    readable = _readable(principal)
    if readable is not None:
        outside = [p for p in prefixes if p.split("/", 1)[0] not in readable]
        if outside:
            raise ApiError(403, "forbidden", "you cannot read these namespaces",
                           details={"prefixes": outside})
    owner = effective_agent(principal, None)
    subscription = await session.scalar(
        select(EventSubscription).where(
            EventSubscription.workspace_id == principal.workspace.id,
            EventSubscription.owner == owner,
            EventSubscription.name == name,
        )
    )
    if subscription is None:
        subscription = EventSubscription(
            workspace_id=principal.workspace.id, owner=owner,
            owner_token_id=principal.token_id, name=name,
        )
        session.add(subscription)
    subscription.prefixes = prefixes
    subscription.types = types
    subscription.is_default = is_default
    await session.flush()
    await session.refresh(subscription, ["id", "created_at"])
    cursor = encode_cursor(await current_seq(session, principal.workspace.id))
    await session.commit()
    return subscription_out(subscription, cursor)


async def default_subscription(session: AsyncSession, principal: Principal) -> dict[str, Any]:
    """The subscription every agent starts with: what its token may read (AG-EV-02 3)."""
    readable = _readable(principal)
    prefixes = [f"{namespace}/" for namespace in readable] if readable is not None else []
    return await subscribe(session, principal, prefixes=prefixes, types=None,
                           name="default", is_default=True)


async def list_subscriptions(session: AsyncSession, principal: Principal) -> dict[str, Any]:
    require_permission(principal, "events:read")
    rows = (
        await session.execute(
            select(EventSubscription).where(
                EventSubscription.workspace_id == principal.workspace.id,
                EventSubscription.owner == effective_agent(principal, None),
            ).order_by(EventSubscription.created_at)
        )
    ).scalars().all()
    return {"items": [subscription_out(row) for row in rows]}


async def delete_subscription(
    session: AsyncSession, principal: Principal, subscription_id: str
) -> None:
    subscription = await _subscription(session, principal, subscription_id)
    await session.delete(subscription)
    await session.commit()


async def _subscription(
    session: AsyncSession, principal: Principal, subscription_id: str
) -> EventSubscription:
    subscription = await session.get(EventSubscription, subscription_id)
    if (
        subscription is None
        or subscription.workspace_id != principal.workspace.id
        or subscription.owner != effective_agent(principal, None)
    ):
        raise NotFound("subscription", f"subscription '{subscription_id}' not found")
    return subscription


# ------------------------------------------------------------------ poll


async def poll_events(
    principal: Principal,
    *,
    cursor: str | None = None,
    subscription_id: str | None = None,
    types: list[str] | None = None,
    namespaces: list[str] | None = None,
    for_me: bool = False,
    written_by_subtree: str | None = None,
    wait_seconds: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    """Events after the cursor, waiting up to ``wait_seconds`` for the first one.

    Uses short platform sessions so a long poll never holds a connection.
    """
    require_permission(principal, "events:read")
    if not 0 <= wait_seconds <= POLL_WAIT_MAX:
        raise ApiError(422, "validation_error", f"wait_seconds must be 0-{POLL_WAIT_MAX}",
                       details={"field": "wait_seconds", "limit": POLL_WAIT_MAX})
    if not 1 <= limit <= POLL_LIMIT_MAX:
        raise ApiError(422, "validation_error", f"limit must be 1-{POLL_LIMIT_MAX}",
                       details={"field": "limit", "limit": POLL_LIMIT_MAX})
    workspace_id = principal.workspace.id
    conditions: list[Any] = [WorkspaceEvent.workspace_id == workspace_id,
                             WorkspaceEvent.type.is_not(None)]
    readable = _readable(principal)
    if readable is not None:
        conditions.append(WorkspaceEvent.shard_key.in_(readable))
    async with async_session_factory() as session:
        if subscription_id:
            subscription = await _subscription(session, principal, subscription_id)
            scope = []
            if subscription.prefixes:
                scope.append(_prefix_condition(subscription.prefixes))
            if scope:
                # Addressed events always reach their target (AG-EV-02 point 2).
                conditions.append(or_(*scope, _targets_me(principal)))
            if subscription.types:
                conditions.append(or_(_type_condition(subscription.types),
                                      _targets_me(principal)))
        if types:
            conditions.append(_type_condition(types))
        if namespaces:
            conditions.append(WorkspaceEvent.shard_key.in_(namespaces))
        if for_me:
            conditions.append(_targets_me(principal))
        if written_by_subtree:
            from app.services.lineage import subtree_token_ids

            token_ids = await subtree_token_ids(session, principal, written_by_subtree)
            conditions.append(
                WorkspaceEvent.stamp["chain"].op("?|")(
                    text("CAST(:chain AS text[])").bindparams(chain=token_ids)
                )
            )
        seq = decode_cursor(cursor)
        if cursor and seq is None:
            raise ApiError(400, "cursor_invalid", "event cursor is not valid",
                           details={"field": "cursor"})
        if seq is None:
            seq = await current_seq(session, workspace_id)
            oldest = await _oldest_seq(session, workspace_id) or seq + 1
        else:
            oldest = await check_cursor(session, workspace_id, seq)
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while True:
        async with async_session_factory() as session:
            rows = (
                await session.execute(
                    select(WorkspaceEvent)
                    .where(*conditions, WorkspaceEvent.seq > seq)
                    .order_by(WorkspaceEvent.seq)
                    .limit(limit + 1)
                )
            ).scalars().all()
            head = await current_seq(session, workspace_id) if not rows else None
            if subscription_id:
                await session.execute(
                    text("UPDATE event_subscriptions SET last_polled_at = NOW(), last_seq = :seq "
                         "WHERE id = :id"),
                    {"seq": rows[-1].seq if rows else seq, "id": subscription_id},
                )
                await session.commit()
        if rows or asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(min(_POLL_INTERVAL, max(0.0, deadline - asyncio.get_running_loop()
                                                    .time())))
    has_more = len(rows) > limit
    rows = rows[:limit]
    # With no matching events the cursor still moves to the head: nothing up to
    # it can match this filter, so the next poll starts there.
    next_seq = rows[-1].seq if rows else max(seq, head or seq)
    return {
        "events": [event_out(row) for row in rows],
        "next_cursor": encode_cursor(next_seq),
        "has_more": has_more,
        "oldest_available_seq": oldest,
    }


# ------------------------------------------------------------------ sync


async def namespace_counts(
    session: AsyncSession, principal: Principal
) -> list[dict[str, Any]]:
    """Exact per-namespace counts, the same population as list_documents' census."""
    query = (
        select(
            SharedDocument.namespace,
            func.count(SharedDocument.id).filter(SharedDocument.archived_at.is_(None)),
            func.count(SharedDocument.id).filter(SharedDocument.archived_at.is_not(None)),
            func.count(SharedDocument.id).filter(
                SharedDocument.verification_status != "VERIFIED",
                SharedDocument.archived_at.is_(None),
            ),
            func.count(SharedDocument.id).filter(
                SharedDocument.tainted.is_(True), SharedDocument.archived_at.is_(None)
            ),
            func.max(SharedDocument.updated_at),
        )
        .where(SharedDocument.workspace_id == principal.workspace.id)
        .group_by(SharedDocument.namespace)
        .order_by(SharedDocument.namespace)
    )
    readable = _readable(principal)
    if readable is not None:
        query = query.where(SharedDocument.namespace.in_(readable))
    return [
        {"namespace": namespace, "documents": int(documents), "archived": int(archived),
         "unverified": int(unverified), "tainted": int(tainted),
         "last_updated_at": last.isoformat() if last else None}
        for namespace, documents, archived, unverified, tainted, last
        in (await session.execute(query)).all()
    ]


_OPS = {"doc.archived": "archive", "doc.restored": "restore", "doc.purged": "purge"}


async def document_changes(
    session: AsyncSession, principal: Principal, since_seq: int, *,
    namespaces: list[str] | None, include_archived: bool,
) -> list[dict[str, Any]]:
    """Each changed document once, at its latest state (AG-EV-01 point 5)."""
    await check_cursor(session, principal.workspace.id, since_seq)
    conditions: list[Any] = [
        WorkspaceEvent.workspace_id == principal.workspace.id,
        WorkspaceEvent.seq > since_seq,
        WorkspaceEvent.type.like("doc.%"),
    ]
    readable = _readable(principal)
    if readable is not None:
        conditions.append(WorkspaceEvent.shard_key.in_(readable))
    if namespaces:
        conditions.append(WorkspaceEvent.shard_key.in_(namespaces))
    events = (
        await session.execute(
            select(WorkspaceEvent.record_id, WorkspaceEvent.type, WorkspaceEvent.refs,
                   WorkspaceEvent.namespace, WorkspaceEvent.doc_key)
            .where(*conditions).order_by(WorkspaceEvent.seq)
        )
    ).all()
    last: dict[str, tuple[str, dict, str | None, str | None]] = {}
    for record_id, event_type, refs, namespace, doc_key in events:
        if event_type in ("doc.indexed", "doc.index_failed"):
            continue
        key = str(record_id) if record_id else (refs or {}).get("id") or doc_key
        last.pop(key, None)  # keep insertion order by latest change
        last[key] = (event_type, refs or {}, namespace, doc_key)
    ids = [uuid.UUID(key) for key in last if _is_uuid(key)]
    documents = {
        str(row.id): row for row in (
            await session.execute(select(SharedDocument).where(SharedDocument.id.in_(ids)))
        ).scalars().all()
    } if ids else {}
    changes = []
    for key, (event_type, refs, namespace, doc_key) in last.items():
        document = documents.get(key)
        if document is None:
            tombstone = await session.scalar(
                select(Tombstone).where(
                    Tombstone.workspace_id == principal.workspace.id,
                    Tombstone.canonical_id == refs.get("id"),
                )
            ) if refs.get("id") else None
            changes.append({
                "op": "purge", "id": refs.get("id"), "namespace": namespace,
                "doc_key": refs.get("alias") or doc_key,
                "version": tombstone.last_version if tombstone else None,
                "content_hash": tombstone.last_content_hash if tombstone else None,
                "updated_at": tombstone.purged_at.isoformat() if tombstone else None,
                "updated_by": tombstone.purged_by if tombstone else None,
            })
            continue
        if document.archived_at is not None and not include_archived and \
                event_type != "doc.archived":
            continue
        op = _OPS.get(event_type, "archive" if document.archived_at else "upsert")
        changes.append({
            "op": op, "id": document.canonical_id, "namespace": document.namespace,
            "doc_key": document.doc_key, "version": document.version,
            "content_hash": document.content_hash,
            "updated_at": document.updated_at.isoformat(), "updated_by": document.updated_by,
        })
    return changes


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def event_type_catalog() -> dict[str, Any]:
    return {
        "types": EVENT_TYPES,
        "delivery": "at least once; deduplicate by event_id; seq is per workspace, "
                    "gap-free and in commit order",
        "cursor": "opaque; poll_events and workspace_sync(since=) accept each other's cursor",
        "retention_days": settings.event_retention_days,
        "poll": {"wait_seconds_max": POLL_WAIT_MAX, "limit_max": POLL_LIMIT_MAX},
    }
