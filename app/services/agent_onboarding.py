"""Agent onboarding over MCP: five protocol steps, role-aware, never a gate.

1. get_orientation
2. read system/constitution and ack_constitution
3. read the namespace readme (system/namespaces/<ns>)
4. register_agent: be findable in the directory (2026-10-23 memory seed
   spec, R-01); an entry the platform filled in (source=auto) does not count
5. the role's first read and first action:
   SCOUT    read raw_inbox (or its namespace), then write a draft or a proposal
   ANALYST  read its namespace, then create or claim a task
   AUDITOR  read the REVIEWING queue, then review a task
   EXECUTOR read claimable tasks, then claim one
   VIEWER   read its namespace (read-only role)

Progress is one row per agent token and workspace (``agent_onboarding``),
keyed by the token's canonical id, never its secret. Services report what an
agent did through ``observe``; until the agent is active, successful key tool
responses carry a small ``onboarding`` block (state, completed_steps,
next_call) through the warnings channel, so REST and MCP both show it. Nothing
here rejects a call, and a failure to record progress never fails one.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from sqlalchemy import text

from app.auth import AuthKind, Principal, RoleScope
from app.database import async_session_factory
from app.warnings_context import set_onboarding

logger = logging.getLogger("agentgraph.agent_onboarding")

TOTAL_STEPS = 5
STATES = ("new", "oriented", "needs_ack", "needs_namespace_readme", "needs_registration",
          "needs_first_action", "active")
_STEP_OF = {"new": 1, "oriented": 2, "needs_ack": 2, "needs_namespace_readme": 3,
            "needs_registration": 4, "needs_first_action": 5, "active": TOTAL_STEPS}
# What counts as the role's first action.
FIRST_ACTIONS: dict[str, frozenset[str]] = {
    "SCOUT": frozenset({"document", "proposal"}),
    "ANALYST": frozenset({"task_create", "task_claim"}),
    "AUDITOR": frozenset({"review"}),
    "EXECUTOR": frozenset({"task_claim"}),
    "VIEWER": frozenset({"read"}),
}
DO_NOT = [
    "Do not write before you have read the constitution and your namespace readme.",
    "Do not write to system/*: propose changes with propose_document_change.",
    "Do not mark your own work DONE or verify it: hand off to REVIEWING for a human.",
    "Do not retry a 409 or 429 unchanged: re-read, or wait for Retry-After.",
    "Do not read every document up front: follow required_reading, then use events.",
]
# Agents known to be active in this process: no further lookups for them.
_ACTIVE: set[tuple[str, str]] = set()
_ACTIVE_LIMIT = 50_000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def tracked(principal: Principal | None) -> bool:
    return principal is not None and principal.auth_kind is AuthKind.AGENT


def _key(principal: Principal) -> str:
    from app.services.orientation import principal_key

    return principal_key(principal)


def _role(principal: Principal) -> str:
    role = principal.role
    return role.value if isinstance(role, RoleScope) else str(role)


def _readable(principal: Principal) -> list[str] | None:
    namespaces = [ns for ns in principal.allowed_namespaces if ns and ns != "system"]
    if principal.is_commander or "*" in principal.allowed_namespaces:
        return None
    return namespaces


def primary_namespace(principal: Principal) -> str | None:
    readable = _readable(principal)
    if readable is None:
        return "default"
    return readable[0] if readable else None


# ------------------------------------------------------------------ state


def state_of(row: dict[str, Any] | None, constitution_version: int | None) -> str:
    if row is None or row.get("oriented_at") is None:
        return "new"
    if row.get("completed_at") is not None:
        return "active"
    acked = row.get("constitution_version_acked")
    if acked is None or (constitution_version is not None and acked < constitution_version):
        return "needs_ack" if row.get("constitution_read_at") else "oriented"
    if row.get("namespace_readme_read_at") is None:
        return "needs_namespace_readme"
    if row.get("registered_at") is None:
        return "needs_registration"
    return "needs_first_action"


def completed_steps(row: dict[str, Any] | None, state: str) -> list[int]:
    if state == "active":
        return list(range(1, TOTAL_STEPS + 1))
    steps = []
    if row and row.get("oriented_at"):
        steps.append(1)
    if row and row.get("constitution_version_acked") is not None and state not in (
            "oriented", "needs_ack"):
        steps.append(2)
    if row and row.get("namespace_readme_read_at"):
        steps.append(3)
    if row and row.get("registered_at"):
        steps.append(4)
    return steps


def next_call(principal: Principal, state: str, row: dict[str, Any] | None,
              constitution_version: int | None) -> dict[str, Any] | None:
    namespace = primary_namespace(principal)
    if state == "new":
        return {"tool": "get_orientation", "args": {}}
    if state in ("oriented", "needs_ack"):
        return {"tool": "ack_constitution", "args": {"version": constitution_version or 1},
                "why": "Acknowledge the workspace rules you just read."}
    if state == "needs_namespace_readme":
        if namespace is None:
            # No namespace on the token: the namespace map is the readable step.
            return {"tool": "list_namespaces", "args": {},
                    "why": "Your token has no namespace yet; see what exists and ask a "
                           "human to grant access with request_human_review."}
        return {"tool": "dereference", "args": {"uri": f"doc://system/namespaces/{namespace}"},
                "why": f"The readme says what belongs in {namespace} and how to write there."}
    if state == "needs_registration":
        from app.services.directory import default_agent_id

        return {"tool": "register_agent",
                "args": {"agent_id": default_agent_id(principal.agent_signature),
                         "capabilities": ["<what you do, in a few words>"]},
                "why": "Be findable: others address you as "
                       f"agent:{principal.agent_signature} and see what you do."}
    if state == "needs_first_action":
        return _first_action(principal, row, namespace)
    return None


def _first_action(principal: Principal, row: dict[str, Any] | None,
                  namespace: str | None) -> dict[str, Any]:
    role = _role(principal)
    read_done = bool(row and row.get("first_read_at"))
    ns = namespace or "default"
    if role == "SCOUT":
        inbox = "raw_inbox" if _readable(principal) is None or "raw_inbox" in (
            _readable(principal) or []) else ns
        if not read_done:
            return {"tool": "list_documents", "args": {"namespace": inbox},
                    "why": "See what is already collected before adding to it."}
        return {"tool": "upsert_shared_doc",
                "args": {"namespace": inbox, "doc_key": f"drafts/{principal.agent_signature}-first",
                         "content": "# First finding\n\n...", "expected_version": 0},
                "why": "Write your first draft (or propose_document_change for a protected doc)."}
    if role == "ANALYST":
        if not read_done:
            return {"tool": "list_documents", "args": {"namespace": ns},
                    "why": "Read the target namespace before planning work."}
        return {"tool": "create_task", "args": {"title": "First analysis", "namespace": ns},
                "why": "Create an analysis task, or claim one with agent_claim_and_read."}
    if role == "AUDITOR":
        if not read_done:
            return {"tool": "workspace_sync", "args": {"task_statuses": ["REVIEWING"]},
                    "why": "Find the tasks waiting for review."}
        return {"tool": "review_task",
                "args": {"task_key": "<a REVIEWING task_key>", "decision": "approve"},
                "why": "Review one task. If none is waiting, poll_events(types=['task.*'], "
                       "wait_seconds=25) until one arrives."}
    if role == "EXECUTOR":
        if not read_done:
            return {"tool": "workspace_sync", "args": {"task_statuses": ["READY", "HANDOFF"]},
                    "why": "Find tasks you can claim."}
        return {"tool": "agent_claim_and_read", "args": {"task_key": "<a READY task_key>"},
                "why": "Claim one task. If none is ready, poll_events(types=['task.created'], "
                       "wait_seconds=25)."}
    return {"tool": "list_documents", "args": {"namespace": ns},
            "why": "Read your namespace; your role is read-only."}


def light(principal: Principal, row: dict[str, Any] | None,
          constitution_version: int | None) -> dict[str, Any]:
    state = state_of(row, constitution_version)
    return {
        "state": state,
        "step": _STEP_OF[state],
        "total_steps": TOTAL_STEPS,
        "completed_steps": completed_steps(row, state),
        "next_call": next_call(principal, state, row, constitution_version),
    }


# ------------------------------------------------------------------ storage


async def _constitution_version(session, workspace_id) -> int | None:
    return await session.scalar(
        text("SELECT version FROM shared_documents WHERE workspace_id = :ws "
             "AND doc_key = 'system/constitution'"),
        {"ws": workspace_id},
    )


async def _load(session, principal: Principal) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text("SELECT * FROM agent_onboarding WHERE workspace_id = :ws "
                 "AND principal_key = :key"),
            {"ws": principal.workspace.id, "key": _key(principal)},
        )
    ).mappings().one_or_none()
    return dict(row) if row else None


async def _audit(session, principal: Principal, action: str, **details: Any) -> None:
    await session.execute(
        text("INSERT INTO access_audit (workspace_id, action, details) "
             "VALUES (:ws, :action, CAST(:details AS jsonb))"),
        {"ws": principal.workspace.id, "action": action,
         "details": _json({"principal": principal.agent_signature, "token_id": _key(principal),
                           "role": _role(principal), **details})},
    )


def _json(value: Any) -> str:
    return json.dumps(value, default=str)


_COLUMNS = {
    "oriented": "oriented_at",
    "constitution_read": "constitution_read_at",
    "readme_read": "namespace_readme_read_at",
    "read": "first_read_at",
    "document": "first_document_at",
    "proposal": "first_document_at",
    "task_create": "first_task_at",
    "task_claim": "first_task_at",
    "review": "first_review_at",
    "registered": "registered_at",
}


async def observe(
    principal: Principal | None, event: str, *, attach: bool = True, **info: Any
) -> dict[str, Any] | None:
    """Record an onboarding-relevant success; return (and attach) the next hint."""
    if not tracked(principal):
        return None
    marker = (str(principal.workspace.id), _key(principal))
    if marker in _ACTIVE:
        return None
    try:
        return await _observe(principal, event, marker, attach=attach, **info)
    except Exception:  # noqa: BLE001 - onboarding must never fail the call it follows
        logger.exception("could not record onboarding progress")
        return None


async def _observe(principal: Principal, event: str, marker: tuple[str, str], *,
                   attach: bool, **info: Any) -> dict[str, Any] | None:
    async with async_session_factory() as session:
        version = await _constitution_version(session, principal.workspace.id)
        await session.execute(
            text(
                "INSERT INTO agent_onboarding (workspace_id, principal_key, token_id, "
                "agent_signature, role) VALUES (:ws, :key, :token, :agent, :role) "
                "ON CONFLICT (workspace_id, principal_key) DO NOTHING"
            ),
            {"ws": principal.workspace.id, "key": marker[1],
             "token": marker[1] if marker[1].startswith("tok_") else None,
             "agent": principal.agent_signature, "role": _role(principal)},
        )
        row = (
            await session.execute(
                text("SELECT * FROM agent_onboarding WHERE workspace_id = :ws "
                     "AND principal_key = :key FOR UPDATE"),
                {"ws": principal.workspace.id, "key": marker[1]},
            )
        ).mappings().one()
        row = dict(row)
        before = state_of(row, version)
        now = _now()
        changes: dict[str, Any] = {}
        if row.get("registered_at") is None and event != "registered" and await session.scalar(
                text("SELECT 1 FROM agent_directory WHERE :t = ANY(token_ids) "
                     "AND source <> 'auto'"), {"t": principal.token_cid or ""}):
            changes["registered_at"] = now  # registered at birth (issue_child_token)
        column = _COLUMNS.get(event)
        if event == "acked":
            if row.get("constitution_version_acked") != info.get("version"):
                changes["constitution_version_acked"] = info.get("version")
                changes["acked_at"] = now
                if row.get("constitution_read_at") is None:
                    changes["constitution_read_at"] = now
        elif column and row.get(column) is None:
            changes[column] = now
            if event == "readme_read":
                changes["readme_namespace"] = info.get("namespace")
        first_action = event in FIRST_ACTIONS.get(_role(principal), frozenset())
        if first_action and row.get("first_action_kind") is None:
            changes["first_action_kind"] = event
        row.update(changes)
        candidate = state_of(row, version)
        if candidate == "needs_first_action" and row.get("first_action_kind") and (
                row.get("first_read_at") or _role(principal) == "VIEWER"
                or row.get("first_action_kind") in ("task_claim", "review")):
            changes["completed_at"] = now
            row["completed_at"] = now
        after = state_of(row, version)
        if changes:
            changes["state"] = after
            changes["updated_at"] = now
            assignments = ", ".join(f"{name} = :{name}" for name in changes)
            await session.execute(
                text(f"UPDATE agent_onboarding SET {assignments} WHERE workspace_id = :ws "
                     "AND principal_key = :key"),
                {**changes, "ws": principal.workspace.id, "key": marker[1]},
            )
            await _audit_transition(session, principal, event, before, after, changes, info)
        hint = light(principal, row, version)
        if after != "active" and row.get("memory_seed_version") is None:
            # The memory seed rides along until the agent stores it (or is done).
            seed = await memory_seed(session, principal, version, row)
            hint["memory_seed"] = compact_seed(seed)
        if after != "active":
            await session.execute(
                text("UPDATE agent_onboarding SET last_prompted_at = :now "
                     "WHERE workspace_id = :ws AND principal_key = :key"),
                {"now": now, "ws": principal.workspace.id, "key": marker[1]},
            )
        await session.commit()
    if after == "active":
        _remember_active(marker)
        if before == "active":
            return None
        hint["message"] = "Onboarding complete; hints stop here."
    if attach:
        set_onboarding(hint)
    return hint


async def _audit_transition(session, principal, event, before, after, changes, info) -> None:
    if "oriented_at" in changes:
        await _audit(session, principal, "agent.oriented", state=after)
    if "constitution_version_acked" in changes:
        await _audit(session, principal, "agent.acknowledged",
                     version=changes["constitution_version_acked"])
    if "namespace_readme_read_at" in changes:
        await _audit(session, principal, "agent.namespace_read",
                     namespace=info.get("namespace"))
    if "first_action_kind" in changes:
        await _audit(session, principal, "agent.first_action", kind=changes["first_action_kind"],
                     ref=info.get("ref"))
    if "registered_at" in changes:
        await _audit(session, principal, "agent.registered")
    if "completed_at" in changes:
        await _audit(session, principal, "agent.onboarded", state=after)


def _remember_active(marker: tuple[str, str]) -> None:
    if len(_ACTIVE) >= _ACTIVE_LIMIT:
        _ACTIVE.clear()
    _ACTIVE.add(marker)


async def observe_read(principal: Principal | None, documents: list[Any]) -> None:
    """A successful document read: constitution, a namespace readme or other content."""
    if not tracked(principal):
        return
    keys = [getattr(document, "doc_key", None) for document in documents]
    readable = _readable(principal)
    for doc_key in keys:
        if doc_key == "system/constitution":
            await observe(principal, "constitution_read")
            return
        if doc_key and doc_key.startswith("system/namespaces/"):
            namespace = doc_key.split("/", 2)[2]
            if readable is None or namespace in readable or not readable:
                await observe(principal, "readme_read", namespace=namespace)
                return
    if any(key and not key.startswith("system/") for key in keys):
        await observe(principal, "read")


def records(event: str):
    """Decorate a service ``fn(session, principal, ...)``: record ``event`` on success."""

    def decorate(fn):
        @wraps(fn)
        async def wrapper(session, principal, *args, **kwargs):
            result = await fn(session, principal, *args, **kwargs)
            await observe(principal, event)
            return result

        return wrapper

    return decorate


def records_read(fn):
    """Decorate a document read: constitution, a namespace readme, or other content."""

    @wraps(fn)
    async def wrapper(session, principal, *args, **kwargs):
        result = await fn(session, principal, *args, **kwargs)
        document = getattr(result, "document", None) or result
        await observe_read(principal, [document])
        return result

    return wrapper


# ------------------------------------------------------------------ orientation


async def orientation_block(
    session, principal: Principal, constitution: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The onboarding section of get_orientation (records step 1 for agents)."""
    if not tracked(principal):
        return None
    version = (constitution or {}).get("version")
    previous = await _load(session, principal)
    first_time = previous is None or previous.get("oriented_at") is None
    await observe(principal, "oriented", attach=False)
    async with async_session_factory() as platform:
        row = await _load(platform, principal)
    state = "new" if first_time else state_of(row, version)
    block = light(principal, row, version)
    if first_time:
        # This call is step 1: report the state the agent arrived in and what's next.
        block["state"] = "new"
        block["step"] = 1
        block["completed_steps"] = [1]
    namespace = primary_namespace(principal)
    reading = [{"uri": "doc://system/constitution",
                "why": "The workspace rules; acknowledge them with ack_constitution."}]
    if namespace:
        reading.append({"uri": f"doc://system/namespaces/{namespace}",
                        "why": f"What belongs in {namespace} and how to write there."})
    reading.append({"uri": "agentgraph://docs/agent-guide",
                    "why": "The full contract, read when needed (also get_api_docs)."})
    block.update({
        "role": _role(principal),
        "first_action": _role_first_action_text(_role(principal)),
        "required_reading": reading[:3],
        "do_not": DO_NOT if state != "active" else [],
        "first_time": first_time,
    })
    return block


def _role_first_action_text(role: str) -> str:
    return {
        "SCOUT": "Read raw_inbox, then write a draft or propose a change.",
        "ANALYST": "Read your target namespace, then create or claim an analysis task.",
        "AUDITOR": "Read the REVIEWING queue, then review one task.",
        "EXECUTOR": "Read claimable tasks, then claim one.",
        "VIEWER": "Read your namespace; your role cannot write.",
    }.get(role, "Read your namespace, then take the first action your role allows.")


# ------------------------------------------------------------------ humans


async def overview(session, workspace_id) -> dict[str, Any]:
    """Read-only progress per agent token for the console (no secrets)."""
    version = await _constitution_version(session, workspace_id)
    rows = (
        await session.execute(
            text(
                "SELECT t.canonical_id AS token_id, t.agent_signature, t.role_scope::text AS role, "
                "t.is_active, o.principal_key, o.oriented_at, o.constitution_read_at, "
                "o.constitution_version_acked, o.namespace_readme_read_at, o.first_read_at, "
                "o.registered_at, o.first_action_kind, o.completed_at, o.updated_at, "
                "o.memory_seed_version "
                "FROM agent_tokens t LEFT JOIN agent_onboarding o "
                "ON o.workspace_id = t.workspace_id AND o.principal_key = t.canonical_id "
                # Delegated child tokens are short-lived helpers, not identities
                # people onboard: progress covers console-issued tokens only.
                "WHERE t.workspace_id = :ws AND t.depth <= 1 ORDER BY t.created_at"
            ),
            {"ws": workspace_id},
        )
    ).mappings().all()
    items = []
    for record in rows:
        record = dict(record)
        has_row = record.get("principal_key") is not None
        state = state_of(record if has_row else None, version)
        items.append({
            "token_id": record["token_id"],
            "agent_signature": record["agent_signature"],
            "role": record["role"],
            "is_active": record["is_active"],
            "state": state,
            "step": _STEP_OF[state],
            "total_steps": TOTAL_STEPS,
            "completed_steps": completed_steps(record if has_row else None, state),
            "first_action_kind": record.get("first_action_kind"),
            "memory_seed_version": record.get("memory_seed_version"),
            "started_at": record.get("oriented_at"),
            "completed_at": record.get("completed_at"),
            "last_activity_at": record.get("updated_at"),
        })
    oriented = [item for item in items if item["state"] != "new"]
    acked = [item for item in items if 2 in item["completed_steps"]]
    done = [item for item in items if item["state"] == "active"]
    durations = [
        (item["completed_at"] - item["started_at"]).total_seconds()
        for item in done if item["completed_at"] and item["started_at"]
    ]
    total = len(items) or 1
    return {
        "items": items,
        "summary": {
            "tokens": len(items),
            "by_state": {state: sum(item["state"] == state for item in items)
                         for state in STATES},
            "orientation_rate": round(len(oriented) / total, 3),
            "constitution_ack_rate": round(len(acked) / total, 3),
            "first_action_rate": round(len(done) / total, 3),
            "avg_onboarding_seconds": round(sum(durations) / len(durations), 1)
            if durations else None,
        },
    }


# ------------------------------------------------------------------ memory seed

SEED_MAX_CHARS = 1_500  # D-1
RULES_MAX_CHARS = 600
# L1: the hard rules, verbatim (D-2: rules and rulings only, no tutorials).
RULES_DIGEST = (
    "1. Do not write before reading the constitution and your namespace readme.\n"
    "2. system/* is proposal-only: propose_document_change, never a direct write.\n"
    "3. Never mark your own work DONE: hand off to REVIEWING.\n"
    "4. Never retry a 409 or 429 unchanged: re-read, or wait for Retry-After.\n"
    "5. Do not pre-read every document: follow required_reading and events.\n"
    "未读宪法与 readme 前不写；system/* 只提案不直写；不自标 DONE，交 REVIEWING；"
    "409/429 不原样重试；不预读全部文档，按必读清单与事件工作。"
)
PERSIST_INSTRUCTION = (
    "Store this block in your long-term memory. Each time you come in, compare "
    "seed_version with the one you stored; if it changed, re-read the pointers whose "
    "version changed, update your memory and call ack_memory_seed(seed_version). "
    "Keep pointers and versions only, never document text."
)


async def _doc_version(session, workspace_id, doc_key: str) -> int:
    return int(await session.scalar(
        text("SELECT version FROM shared_documents WHERE workspace_id = :ws "
             "AND doc_key = :k"), {"ws": workspace_id, "k": doc_key}) or 0)


async def memory_seed(session, principal: Principal, constitution_version: int | None,
                      row: dict[str, Any] | None = None) -> dict[str, Any]:
    """The standard payload an agent writes into its own long-term memory: who it
    is, the rules (L1) and pointers with versions (L2). No document text."""
    namespace = primary_namespace(principal) or "default"
    readme_key = f"system/namespaces/{namespace}"
    readme_version = await _doc_version(session, principal.workspace.id, readme_key)
    constitution = int(constitution_version or 0)
    pointers = [
        {"uri": "doc://system/constitution", "version": constitution,
         "purpose": "The workspace rules; re-read when the version changes."},
        {"uri": f"doc://{readme_key}", "version": readme_version,
         "purpose": f"What belongs in {namespace} and how to write there."},
        {"uri": "task://board", "version": 0,
         "purpose": "Your work: list_tasks(claimable_by_me=true), list_inbox."},
        {"uri": "task://proposals", "version": 0,
         "purpose": "Feedback and changes: propose_document_change, "
                    "list_document_proposals."},
    ]
    # Any pointer's version bump moves the seed (A-02), so the sum, not the max.
    seed_version = sum(pointer["version"] for pointer in pointers)
    stored = (row or {}).get("memory_seed_version")
    return {
        "seed_version": seed_version,
        "stored_version": stored,
        "stale": stored is not None and stored != seed_version,
        "identity": {"label": f"agent:{principal.agent_signature}", "role": _role(principal),
                     "home_namespace": namespace, "token_depth": principal.token_depth},
        "rules_digest": RULES_DIGEST[:RULES_MAX_CHARS],
        "pointers": pointers,
        "persist_instruction": PERSIST_INSTRUCTION,
        "ack_with": "ack_memory_seed",
    }


def compact_seed(seed: dict[str, Any]) -> dict[str, Any]:
    """What rides in the warnings channel until the seed is stored."""
    return {"seed_version": seed["seed_version"], "identity": seed["identity"],
            "pointers": [{"uri": item["uri"], "version": item["version"]}
                         for item in seed["pointers"]],
            "ack_with": "ack_memory_seed", "full": "get_orientation(sections=['memory_seed'])"}


async def seed_for(session, principal: Principal,
                   constitution_version: int | None) -> dict[str, Any] | None:
    if not tracked(principal):
        return None
    async with async_session_factory() as platform:
        row = await _load(platform, principal)
    return await memory_seed(session, principal, constitution_version, row)


async def ack_memory_seed(session, principal: Principal, version: int) -> dict[str, Any]:
    """Record the seed version the agent stored; the seed stops riding along (A-03)."""
    from app.errors import ApiError

    if not tracked(principal):
        raise ApiError(403, "forbidden", "only agent tokens keep a memory seed")
    constitution = await _constitution_version(session, principal.workspace.id)
    current = await memory_seed(session, principal, constitution)
    async with async_session_factory() as platform:
        await platform.execute(
            text("INSERT INTO agent_onboarding (workspace_id, principal_key, token_id, "
                 "agent_signature, role) VALUES (:ws, :key, :token, :agent, :role) "
                 "ON CONFLICT (workspace_id, principal_key) DO NOTHING"),
            {"ws": principal.workspace.id, "key": _key(principal),
             "token": _key(principal) if _key(principal).startswith("tok_") else None,
             "agent": principal.agent_signature, "role": _role(principal)})
        await platform.execute(
            text("UPDATE agent_onboarding SET memory_seed_version = :v, "
                 "memory_seed_acked_at = NOW(), updated_at = NOW() "
                 "WHERE workspace_id = :ws AND principal_key = :key"),
            {"v": version, "ws": principal.workspace.id, "key": _key(principal)})
        await platform.commit()
    return {"stored_version": version, "seed_version": current["seed_version"],
            "current": version == current["seed_version"]}
