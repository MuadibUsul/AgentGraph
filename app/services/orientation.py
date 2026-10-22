"""Orientation pack: an agent's first call (PRD AG-OB-01, degraded R0.2 form).

One call answers "who am I, what are the rules, who else is here, what is
mine, what should I read and call next". Until the collaborator directory
(AG-ID-06) ships, collaborators are inferred from recent writers and
``me.registered`` is false.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import agent_guide
from app.auth import AuthKind, Principal, can
from app.config import get_settings
from app.error_registry import RETRY_POLICY
from app.errors import ApiError
from app.models import (
    AccessAudit,
    CollabTask,
    ConstitutionAck,
    HumanUser,
    SharedDocument,
    TaskStatus,
    WorkspaceEvent,
)
from app.services import lineage, onboarding
from app.services.common import utcnow
from app.stamp_context import current_stamp
from app.versioning import current_version

settings = get_settings()

SCHEMA_VERSION = "1"
CONSTITUTION_INLINE_BYTES = 16 * 1024
DEFAULT_MAX_BYTES = 32_768
MAX_MAX_BYTES = 131_072
SECTIONS = (
    "server", "me", "constitution", "namespace_map", "collaborators", "my_tasks",
    "since_last_seen",
    "claimable_tasks", "required_reading", "inbox", "conventions", "events", "onboarding",
    "next_calls", "warnings", "platform_changes",
)
_RELEASE = re.compile(r"^\d+\.\d+\.\d+$")
# A new agent's first orientation stays small: identity, rules summary, three
# links and one next step; the rest is a get_api_docs or MCP resource away.
FIRST_TIME_MAX_BYTES = 12_288
_OPEN = (TaskStatus.DONE, TaskStatus.FAILED)


def _size(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))


def principal_key(principal: Principal) -> str:
    stamp = current_stamp() or {}
    return str(stamp.get("token_id") or stamp.get("principal_id") or principal.agent_signature)


async def _me(session: AsyncSession, principal: Principal) -> dict[str, Any]:
    token = None
    if principal.auth_kind in (AuthKind.AGENT, AuthKind.WORKSPACE, AuthKind.ROOT):
        view = await lineage.get_token(session, principal, "self")
        token = {
            "token_id": view["token_id"],
            "kind": view["kind"],
            "depth": view["depth"],
            "scopes": view["scopes"],
            "namespaces": view["namespaces"],
            "expires_at": view.get("expires_at"),
            "parent_token_id": view.get("parent_token_id"),
            "lineage": view.get("lineage"),
        }
    from app.auth import PERMISSIONS
    from app.services import directory

    entry = await directory.me(session, principal) if principal.token_cid else None
    owner = None
    if entry and entry["owner_human"]:
        user = await session.get(HumanUser, uuid.UUID(entry["owner_human"]))
        if user is not None:
            owner = {"user_id": str(user.id), "display_name": user.display_name}
    if entry is not None:
        return {
            "agent_id": entry["agent_id"],
            "principal_id": entry["principal_id"],
            "display_name": entry["display_name"],
            "type": "agent",
            "role": entry["role"] or principal.role.value,
            "role_scope": principal.role.value,
            "capabilities": entry["capabilities"],
            "owner_human": owner,
            "trust_level": entry["trust_level"],
            "status": entry["status"],
            "registered": True,
            "token": token,
        }
    return {
        "agent_id": principal.agent_signature,
        "display_name": principal.agent_signature,
        "type": "human" if principal.is_human else "agent",
        "role": principal.role.value,
        "role_scope": principal.role.value,
        "capabilities": sorted(PERMISSIONS.get(principal.role, set())),
        "owner_human": None,
        "trust_level": "internal",
        "status": "active",
        "registered": False,
        "token": token,
    }


async def _constitution(session: AsyncSession, principal: Principal) -> dict[str, Any]:
    document = await onboarding.ensure_constitution(session, principal.workspace.id)
    ack = await session.scalar(
        select(ConstitutionAck.version).where(
            ConstitutionAck.workspace_id == principal.workspace.id,
            ConstitutionAck.principal_key == principal_key(principal),
        )
    )
    body = document.content
    truncated = len(body.encode("utf-8")) > CONSTITUTION_INLINE_BYTES
    return {
        "namespace": onboarding.SYSTEM_NAMESPACE,
        "doc_key": document.doc_key,
        "id": document.canonical_id,
        "version": document.version,
        "content": _summary(body) if truncated else body,
        "truncated": truncated,
        "stub": bool((document.frontmatter or {}).get("stub")),
        # AG-CO-12: amendments go through proposals; a person always approves.
        "amendment": {"quorum": None,
                      **((document.frontmatter or {}).get("amendment") or {}),
                      "human_approval": "required", "via": "propose_document_change"},
        "updated_at": document.updated_at,
        "ack_required": ack != document.version,
        "acked_version": ack,
        # AG-ID-11 point 4: ancestors' sections, each with the node it comes from.
        "inherited": await _inherited_rules(principal),
    }


async def _inherited_rules(principal: Principal) -> list[dict[str, Any]]:
    from app.services import tree

    return await tree.inherited_constitution(principal.workspace.tenant_id)


def _summary(body: str, limit: int = 2_048) -> str:
    encoded = body.encode("utf-8")[:limit]
    return encoded.decode("utf-8", errors="ignore")


async def _collaborators(session: AsyncSession, principal: Principal) -> list[dict[str, Any]]:
    """The directory and the workspace's humans; inferred from writes before it exists."""
    from app.services import directory

    registry = (await directory.list_agents(session, principal, limit=200))["items"]
    me = principal.token_cid
    people = [
        {
            "agent_id": entry["agent_id"],
            "type": "agent",
            "display_name": entry["display_name"],
            "role": entry["role"],
            "capabilities": entry["capabilities"],
            "owner_human": entry["owner_human"],
            "trust_level": entry["trust_level"],
            "status": entry["status"],
            "last_seen_at": entry["last_seen_at"],
            "source": "registry",
        }
        for entry in registry
        if me not in entry["token_ids"] and entry["status"] != "retired"
    ]
    if people:
        humans = (
            await session.execute(
                select(HumanUser).where(HumanUser.workspace_id == principal.workspace.id,
                                        HumanUser.is_active.is_(True))
            )
        ).scalars().all()
        people.extend(
            {
                "user_id": str(user.id),
                "type": "human",
                "display_name": user.display_name,
                "role": user.role.lower(),
                "capabilities": [],
                "owner_human": None,
                "status": "active",
                "last_seen_at": user.last_login_at,
                "source": "registry",
            }
            for user in humans
            if user.id != principal.user_id
        )
        return people[:100]
    return await _inferred_collaborators(session, principal)


async def _inferred_collaborators(
    session: AsyncSession, principal: Principal
) -> list[dict[str, Any]]:
    since = utcnow() - timedelta(days=30)
    writers = dict(
        (
            await session.execute(
                select(SharedDocument.updated_by, func.max(SharedDocument.updated_at))
                .where(
                    SharedDocument.workspace_id == principal.workspace.id,
                    SharedDocument.updated_at >= since,
                    SharedDocument.updated_by != "system",
                )
                .group_by(SharedDocument.updated_by)
            )
        ).all()
    )
    claimers = dict(
        (
            await session.execute(
                select(CollabTask.claimed_by, func.max(CollabTask.updated_at))
                .where(
                    CollabTask.workspace_id == principal.workspace.id,
                    CollabTask.updated_at >= since,
                    CollabTask.claimed_by.is_not(None),
                )
                .group_by(CollabTask.claimed_by)
            )
        ).all()
    )
    for name, seen in claimers.items():
        writers[name] = max(seen, writers.get(name, seen))
    humans = set(
        (
            await session.execute(
                select(HumanUser.email).where(HumanUser.workspace_id == principal.workspace.id)
            )
        ).scalars()
    )
    people = [
        {
            "agent_id" if name not in humans else "user_id": name,
            "type": "human" if name in humans else "agent",
            "display_name": name,
            "role": None,
            "capabilities": [],
            "owner_human": None,
            "status": "active",
            "last_seen_at": seen,
            "source": "inferred",
        }
        for name, seen in writers.items()
        if name and name != principal.agent_signature
    ]
    people.sort(key=lambda item: item["last_seen_at"], reverse=True)
    return people[:100]


def _task_view(task: CollabTask) -> dict[str, Any]:
    context = task.input_context or {}
    docs = [context.get("doc_key")] if isinstance(context.get("doc_key"), str) else []
    docs += [key for key in context.get("doc_keys", []) if isinstance(key, str)]
    return {
        "task_id": task.canonical_id,
        "task_key": task.task_key,
        "title": task.title,
        "status": task.status.value,
        "assignee": task.claimed_by,
        "assigned_role": task.assigned_role,
        "namespace": task.namespace,
        "context_docs": docs,
        "done_definition": context.get("done_definition"),
        "output_to": context.get("output_to"),
        "lease_expires_at": task.lease_expires_at,
        "review_feedback": task.review_feedback,
    }


def _visible_namespaces(principal: Principal):
    if principal.is_commander or "*" in principal.allowed_namespaces:
        return None
    return principal.allowed_namespaces


async def _tasks(session: AsyncSession, principal: Principal) -> tuple[list, dict]:
    allowed = _visible_namespaces(principal)
    scope = [CollabTask.workspace_id == principal.workspace.id]
    if allowed is not None:
        scope.append(CollabTask.namespace.in_(allowed))
    mine = (
        await session.execute(
            select(CollabTask)
            .where(
                *scope,
                CollabTask.claimed_by == principal.agent_signature,
                CollabTask.status.not_in(_OPEN),
            )
            .order_by(CollabTask.priority.desc(), CollabTask.updated_at.desc())
            .limit(50)
        )
    ).scalars().all()
    claimable_filter = [
        *scope,
        CollabTask.status == TaskStatus.READY,
        CollabTask.claimed_by.is_(None),
    ]
    if not principal.is_commander:
        claimable_filter.append(
            or_(
                CollabTask.assigned_role.is_(None),
                CollabTask.assigned_role == principal.role.value,
            )
        )
    count = int(
        await session.scalar(select(func.count(CollabTask.id)).where(*claimable_filter)) or 0
    )
    top = (
        await session.execute(
            select(CollabTask)
            .where(*claimable_filter)
            .order_by(CollabTask.priority.desc(), CollabTask.created_at.asc())
            .limit(10)
        )
    ).scalars().all()
    return [_task_view(task) for task in mine], {
        "count": count,
        "top": [_task_view(task) for task in top],
    }


def _required_reading(
    constitution: dict[str, Any], namespaces: list[dict[str, Any]], my_tasks: list[dict]
) -> list[dict[str, Any]]:
    reading = [
        {
            "namespace": onboarding.SYSTEM_NAMESPACE,
            "doc_key": constitution["doc_key"],
            "version": constitution["version"],
            "reason": "constitution",
        }
    ]
    for entry in namespaces:
        if entry["my_access"] == "write" and entry.get("readme") and (
            entry["namespace"] != onboarding.SYSTEM_NAMESPACE
        ):
            reading.append(
                {
                    "namespace": onboarding.SYSTEM_NAMESPACE,
                    "doc_key": entry["readme"]["doc_key"],
                    "version": entry["readme"]["version"],
                    "reason": "namespace_readme",
                }
            )
    seen = {item["doc_key"] for item in reading}
    for task in my_tasks:
        for doc_key in task["context_docs"]:
            if doc_key not in seen:
                seen.add(doc_key)
                reading.append(
                    {"namespace": None, "doc_key": doc_key, "version": None,
                     "reason": "task_context"}
                )
    return reading[:30]


async def _inbox(session: AsyncSession, principal: Principal) -> dict[str, Any]:
    from app.services import events as event_service

    # The same cursor poll_events and get_orientation(since=...) accept.
    seq = await event_service.current_seq(session, principal.workspace.id)
    reviews = 0
    if principal.is_commander or can(principal, "task:review"):
        reviews = int(
            await session.scalar(
                select(func.count(CollabTask.id)).where(
                    CollabTask.workspace_id == principal.workspace.id,
                    CollabTask.status == TaskStatus.REVIEWING,
                )
            )
            or 0
        )
    from app.services import inbox

    deliveries = await inbox.summary(session, principal, human=principal.is_human,
                                     limit=inbox.ORIENTATION_ITEMS)
    mentions = sum(1 for item in deliveries["items"] if item["type"] == "mention.created")
    return {
        "unacked_count": deliveries["unacked_count"],
        "unacked_terminal_count": deliveries["unacked_terminal_count"],
        "items": deliveries["items"],
        "acked_event_seq": deliveries["acked_event_seq"],
        "ack_with": "ack_inbox(up_to_event_seq) / POST /v1/inbox/ack",
        "unread_mentions": mentions,
        "pending_reviews_for_me": reviews,
        "event_cursor": event_service.encode_cursor(seq),
        "event_seq": seq,
        "default_subscription_id": None,
    }


def _conventions(principal: Principal) -> dict[str, Any]:
    return {
        "trust_defaults": agent_guide.trust_defaults(),
        "limits": agent_guide.limits(),
        "rate_limits": {"requests_per_minute": settings.rate_limit_default_per_minute},
        "retry_policy": RETRY_POLICY,
        "first_reads": ["system/constitution", "system/namespaces/<namespace>"],
    }


def _next_calls(
    pack: dict[str, Any], writable: list[str]
) -> list[dict[str, Any]]:
    uris = [f"doc://{item['doc_key']}" for item in pack["required_reading"]][:25]
    calls: list[dict[str, Any]] = [
        {
            "tool": "dereference_many",
            "args": {"uris": uris},
            "rest": {"method": "POST", "path": "/v1/dereference-many", "body": {"uris": uris}},
            "why": "required reading, constitution first",
        }
    ] if uris else []
    constitution = pack["constitution"]
    if constitution["ack_required"]:
        calls.append(
            {
                "tool": "ack_constitution",
                "args": {"version": constitution["version"]},
                "rest": {"method": "POST", "path": "/v1/orientation/ack",
                         "body": {"version": constitution["version"]}},
                "why": "record that you read this constitution version",
            }
        )
    if pack["my_tasks"]:
        task = pack["my_tasks"][0]
        calls.append(
            {
                "tool": "agent_claim_and_read",
                "args": {"task_key": task["task_key"]},
                "why": "continue your highest-priority task",
            }
        )
    elif pack["claimable_tasks"]["top"]:
        task = pack["claimable_tasks"]["top"][0]
        calls.append(
            {
                "tool": "agent_claim_and_read",
                "args": {"task_key": task["task_key"]},
                "why": "claim the top task that matches your role",
            }
        )
    if writable:
        calls.append(
            {
                "tool": "list_documents",
                "args": {"namespace": writable[0], "limit": 50},
                "rest": {
                    "method": "GET",
                    "path": f"/v1/documents?namespace={writable[0]}&limit=50",
                },
                "why": "see what already exists before writing",
            }
        )
    return calls


def _truncate(pack: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """Shed optional detail in priority order; never me, pointers, tasks or next_calls."""
    if _size(pack) <= max_bytes:
        return pack
    if "collaborators" in pack:
        pack["collaborators"] = {"items": pack["collaborators"][:5], "truncated": True}
    if _size(pack) > max_bytes and "claimable_tasks" in pack:
        pack["claimable_tasks"] = {
            "count": pack["claimable_tasks"]["count"], "top": [], "truncated": True
        }
    if _size(pack) > max_bytes and "namespace_map" in pack:
        pack["namespace_map"] = [
            {key: entry.get(key) for key in ("namespace", "my_access", "readme")}
            | {"truncated": True}
            for entry in pack["namespace_map"]
        ]
    if _size(pack) > max_bytes and "constitution" in pack:
        constitution = pack["constitution"]
        overflow = _size(pack) - max_bytes
        keep = max(len(constitution["content"].encode("utf-8")) - overflow - 64, 0)
        constitution["content"] = _summary(constitution["content"], keep)
        constitution["truncated"] = True
    return pack


async def _since_moment(session: AsyncSession, principal: Principal,
                       since: str) -> datetime | None:
    """An event cursor or a release version, as the moment it stands for."""
    from app.services import events as event_service

    if _RELEASE.match(since):
        from app.services import releases

        return await releases.released_at(since)
    seq = event_service.decode_cursor(since)
    if seq is None:
        raise ApiError(400, "cursor_invalid", "since is an event cursor or a release version",
                       details={"field": "since"})
    return await session.scalar(
        select(WorkspaceEvent.created_at).where(
            WorkspaceEvent.workspace_id == principal.workspace.id, WorkspaceEvent.seq <= seq)
        .order_by(WorkspaceEvent.seq.desc()).limit(1))


async def _incremental(session: AsyncSession, principal: Principal, pack: dict[str, Any],
                       moment: datetime) -> dict[str, Any]:
    """Keep only what changed after ``moment`` (retest finding 3).

    The constitution and required reading come back only when changed (then
    in full); namespaces only when their readme changed or they had events;
    conventions only after a platform release. Identity, tasks, inbox and
    next calls are current state and always present.
    """
    changed_keys = set((await session.execute(
        select(SharedDocument.doc_key).where(
            SharedDocument.workspace_id == principal.workspace.id,
            SharedDocument.updated_at > moment))).scalars().all())
    active = set((await session.execute(
        select(WorkspaceEvent.shard_key).where(
            WorkspaceEvent.workspace_id == principal.workspace.id,
            WorkspaceEvent.created_at > moment).distinct())).scalars().all())
    constitution = pack["constitution"]
    if constitution["doc_key"] not in changed_keys:
        pack["constitution"] = {
            key: constitution[key]
            for key in ("namespace", "doc_key", "id", "version", "ack_required", "acked_version")
        } | {"unchanged": True}
    kept = [entry for entry in pack["namespace_map"]
            if entry["namespace"] in active
            or (entry.get("readme") or {}).get("doc_key") in changed_keys]
    pack["namespace_map_unchanged"] = len(pack["namespace_map"]) - len(kept)
    pack["namespace_map"] = kept
    pack["required_reading"] = [
        item for item in pack["required_reading"]
        if item["reason"] == "task_context" or item["doc_key"] in changed_keys
    ]
    if not pack.get("platform_changes"):
        pack["conventions"] = {"unchanged": True}
    pack["collaborators"] = pack["collaborators"][:10]
    # Identity and the subscription rarely change between sessions: keep the
    # essentials and point at the full call.
    me = pack["me"]
    pack["me"] = {key: me.get(key) for key in ("agent_id", "type", "role", "status")} | {
        "token_id": (me.get("token") or {}).get("token_id"), "full": "get_orientation()"}
    subscription = (pack.get("events") or {}).get("default_subscription") or {}
    pack["events"] = {"default_subscription_id": subscription.get("subscription_id")}
    return pack


def _quiet(pack: dict[str, Any]) -> None:
    """Incremental packs drop what is empty or only useful on a first visit."""
    for key in ("namespace_map", "collaborators", "my_tasks", "required_reading"):
        if not pack.get(key):
            pack.pop(key, None)
    if not (pack.get("claimable_tasks") or {}).get("count"):
        pack.pop("claimable_tasks", None)
    digest = pack.get("since_last_seen")
    if digest is not None and not digest.get("total_events"):
        pack["since_last_seen"] = {"total_events": 0, "cursor": digest.get("cursor")}
    pack.pop("events", None)  # inbox.default_subscription_id carries it
    server = pack["server"]
    pack["server"] = {"version": server["version"], "api_version": server["api_version"],
                      "release": (server.get("release") or {}).get("version")}
    pack["next_calls"] = [call for call in pack["next_calls"]
                          if call["tool"] != "list_documents"]


async def build(
    session: AsyncSession,
    principal: Principal,
    *,
    sections: list[str] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    since: str | None = None,
) -> dict[str, Any]:
    if max_bytes < 4_096 or max_bytes > MAX_MAX_BYTES:
        raise ApiError(
            422, "validation_error", "max_bytes must be between 4096 and 131072",
            details={"field": "max_bytes", "limit": MAX_MAX_BYTES, "actual": max_bytes},
        )
    unknown = sorted(set(sections or []) - set(SECTIONS))
    if unknown:
        raise ApiError(
            422, "validation_error", f"unknown sections: {', '.join(unknown)}",
            details={"field": "sections", "allowed": list(SECTIONS)},
        )
    namespace_map = await onboarding.namespace_overview(session, principal)
    my_tasks, claimable = await _tasks(session, principal)
    constitution = await _constitution(session, principal)
    me = await _me(session, principal)
    pack: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc),
        "server": {
            "name": settings.app_name,
            "version": settings.app_version,
            "mcp_stateless": settings.mcp_stateless_http,
            "agent_docs": "/v1/agent-docs",
            "api_version": current_version(),
        },
        "me": me,
        "constitution": constitution,
        "namespace_map": namespace_map,
        "collaborators": await _collaborators(session, principal),
        "my_tasks": my_tasks,
        "claimable_tasks": claimable,
        "inbox": await _inbox(session, principal),
        "conventions": _conventions(principal),
    }
    # AG-EV-02 point 3: every principal starts subscribed to what it may read.
    from app.services import events as event_service

    # A token without events:read still gets its pack, just no subscription.
    subscription = (await event_service.default_subscription(session, principal)
                    if can(principal, "events:read") else None)
    pack["inbox"]["default_subscription_id"] = (subscription or {}).get("subscription_id")
    pack["events"] = {
        "default_subscription": subscription,
        "how": "poll_events(subscription_id, cursor, wait_seconds=25); "
               "dedupe by event_id; pass next_cursor back",
    }
    if can(principal, "events:read"):
        # AG-EV-04 point 3: what happened since this principal last oriented.
        from app.services import digest

        pack["since_last_seen"] = await digest.get_digest(
            session, principal, since="watermark:last_seen", max_items=20,
            advance_watermark="last_seen")
    pack["required_reading"] = _required_reading(constitution, namespace_map, my_tasks)
    # Release discipline: the build in use, and what changed since `since`.
    from app.services import releases

    release = await releases.current()
    pack["server"]["release"] = (
        {"version": release["version"], "released_at": release["released_at"],
         "changelog": release["changelog"]} if release else None)
    moment = await _since_moment(session, principal, since) if since else None
    if since:
        pack["platform_changes"] = await releases.changes_since(moment)
    if moment is not None:
        pack = await _incremental(session, principal, pack, moment)
        pack["incremental"] = {"since": since, "as_of": moment.isoformat()}
    writable = [
        entry["namespace"] for entry in namespace_map
        if entry["my_access"] == "write" and entry["namespace"] != onboarding.SYSTEM_NAMESPACE
    ]
    pack["next_calls"] = _next_calls(pack, writable)
    if moment is not None:
        _quiet(pack)
    from app.services import agent_onboarding

    onboarding_block = await agent_onboarding.orientation_block(session, principal, constitution)
    if onboarding_block is not None:
        pack["onboarding"] = onboarding_block
        if onboarding_block["first_time"] and max_bytes == DEFAULT_MAX_BYTES:
            max_bytes = FIRST_TIME_MAX_BYTES
    warnings = []
    if not me["registered"] and principal.auth_kind is AuthKind.AGENT:
        warnings.append({"code": "not_registered",
                         "message": "this token has no directory entry; call register_agent",
                         "register_with": "register_agent"})
    if constitution["stub"]:
        warnings.append({"code": "constitution_draft",
                         "message": "the constitution is still the generated template"})
    expires_at = (me.get("token") or {}).get("expires_at")
    if expires_at is not None and expires_at - datetime.now(timezone.utc) < timedelta(days=1):
        warnings.append({
            "code": "token_expiring",
            "expires_in_s": int((expires_at - datetime.now(timezone.utc)).total_seconds()),
        })
    pack["warnings"] = warnings
    if sections:
        pack = {
            key: value for key, value in pack.items()
            if key in sections or key in ("schema_version", "generated_at")
        }
    if onboarding_block is not None:
        pack["onboarding"] = onboarding_block  # always present, whatever sections were asked
    pack = _truncate(json.loads(json.dumps(pack, default=str)), max_bytes)
    if onboarding_block is None:
        # Agents are audited once, on their first orientation (agent onboarding).
        session.add(
            AccessAudit(
                workspace_id=principal.workspace.id,
                action="agent.oriented",
                details={"principal": principal.agent_signature,
                         "sections": sections or "all"},
            )
        )
    await session.commit()
    return pack


async def ack_constitution(
    session: AsyncSession, principal: Principal, version: int
) -> dict[str, Any]:
    document = await onboarding.ensure_constitution(session, principal.workspace.id)
    if version != document.version:
        raise ApiError(
            409,
            "version_conflict",
            f"the current constitution is version {document.version}",
            details={"current_version": document.version, "expected_version": version,
                     "doc_key": document.doc_key},
        )
    key = principal_key(principal)
    existing = await session.scalar(
        select(ConstitutionAck).where(
            ConstitutionAck.workspace_id == principal.workspace.id,
            ConstitutionAck.principal_key == key,
        )
    )
    if existing is None:
        session.add(
            ConstitutionAck(workspace_id=principal.workspace.id, principal_key=key,
                            version=version)
        )
    else:
        existing.version = version
        existing.acked_at = utcnow()
    await session.commit()
    from app.services import agent_onboarding

    result = {"acked_version": version, "doc_key": document.doc_key, "principal": key}
    hint = await agent_onboarding.observe(principal, "acked", version=version, attach=False)
    if hint is not None:
        result["onboarding"] = hint
    return result
