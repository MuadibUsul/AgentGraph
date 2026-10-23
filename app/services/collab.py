"""Collaboration primitives for tasks (PRD Phase 2 R2.1).

* AG-CO-02 pointers: ``context_docs`` are pinned to versions at the first
  claim and returned with the claim (continuation first), so every holder in
  a relay reads the same versions; ``required_role`` / capabilities and a
  directory-checked ``assignee`` decide who may claim; ``output_to`` is
  checked (warning only) at handoff.
* AG-EV-03 mentions: ``agent:<id>`` / ``human:<id>`` (and ``@agent:<id>`` in
  bodies) become event targets and a ``mention.created`` event.
* AG-CO-04 continuation packs: a rendered document in ``handoff/`` that the
  task points to; ``require_continuation`` makes it mandatory before handoff.
* AG-CO-05 escalation: a question with options to a human, blocking the task
  (WAITING_HUMAN, lease paused) until resolved or a timeout default applies.
* AG-CO-08 task trees: subtasks, children summaries, rollups, cascading cancel.
"""

from __future__ import annotations

import fnmatch
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthKind, Principal, ensure_namespace
from app.errors import ApiError, Forbidden, NotFound
from app.models import CollabTask, SharedDocument, TaskStatus
from app.services.common import append_event, task_out
from app.warnings_context import add_warning

MAX_TREE_DEPTH = 4
TREE_NODE_LIMIT = 500
_MENTION = re.compile(r"@((?:agent|human):[A-Za-z0-9][A-Za-z0-9_.:@-]{1,150})")
_OPEN = (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ref_key(ref: dict[str, Any]) -> str:
    return f"{ref.get('namespace') or 'default'}:{ref['doc_key']}"


async def _directory_entry(session: AsyncSession, workspace_id: Any,
                           agent_ref: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text("SELECT d.* FROM agent_directory d WHERE d.workspace_id = :ws AND "
                 "(d.agent_id = :r OR d.principal_id = :r OR d.principal_id IN (SELECT "
                 "principal_id FROM agent_aliases a WHERE a.workspace_id = :ws AND "
                 "a.alias = :r)) LIMIT 1"),
            {"ws": workspace_id, "r": agent_ref},
        )
    ).mappings().one_or_none()
    return dict(row) if row else None


async def _my_entry(session: AsyncSession, principal: Principal) -> dict[str, Any] | None:
    if not principal.token_cid:
        return None
    row = (
        await session.execute(
            text("SELECT * FROM agent_directory WHERE :t = ANY(token_ids) LIMIT 1"),
            {"t": principal.token_cid},
        )
    ).mappings().one_or_none()
    return dict(row) if row else None


async def my_directory_entry(session: AsyncSession,
                             principal: Principal) -> dict[str, Any] | None:
    return await _my_entry(session, principal)


async def my_refs(session: AsyncSession, principal: Principal) -> list[str]:
    """Every ``agent:`` / ``human:`` ref that names the caller."""
    refs = [f"agent:{principal.agent_signature}"]
    if principal.user_id is not None:
        refs += [f"human:{principal.user_id}"]
    entry = await _my_entry(session, principal)
    if entry is not None:
        refs += [f"agent:{entry['agent_id']}", f"agent:{entry['principal_id']}"]
    return refs


async def suggest_principal(session: AsyncSession, workspace_id: Any, ref: str) -> list[str]:
    """R-05 did-you-mean: labels are case-sensitive; offer the closest known ones."""
    import difflib

    kind, _, ident = ref.partition(":")
    if kind != "agent" or not ident:
        return []
    known = [row[0] for row in await session.execute(text(
        "SELECT agent_signature FROM agent_tokens WHERE workspace_id = :ws "
        "AND revoked_at IS NULL UNION SELECT agent_id FROM agent_directory "
        "WHERE workspace_id = :ws"), {"ws": workspace_id}) if row[0]]
    exact = [name for name in known if name.lower() == ident.lower()]
    close = difflib.get_close_matches(ident, known, n=3, cutoff=0.6)
    return [f"agent:{name}" for name in dict.fromkeys(exact + close)]


async def principal_exists(session: AsyncSession, workspace_id: Any, ref: str) -> bool:
    kind, _, ident = ref.partition(":")
    if kind == "agent":
        if await _directory_entry(session, workspace_id, ident) is not None:
            return True
        # Unregistered tokens are still addressable by their signature.
        return bool(await session.scalar(
            text("SELECT 1 FROM agent_tokens WHERE workspace_id = :ws AND agent_signature = :a "
                 "AND revoked_at IS NULL"), {"ws": workspace_id, "a": ident}))
    if kind == "human":
        return bool(await session.scalar(
            text("SELECT 1 FROM human_users WHERE workspace_id = :ws AND (CAST(id AS text) = :h "
                 "OR email = :h)"), {"ws": workspace_id, "h": ident}))
    return False


# --------------------------------------------------------------- creation


async def resolve_ref(session: AsyncSession, workspace_id: Any,
                      ref: dict[str, Any]) -> SharedDocument | None:
    """B-01: a {namespace, doc_key} pointer to its document however the key was
    split (doc keys are unique per workspace and usually carry the namespace as
    a prefix), then by id, alias or redirect."""
    from app.services import addresses as address_service

    namespace = ref.get("namespace") or "default"
    key = str(ref["doc_key"])
    candidates = [key]
    if namespace != "default" and not key.startswith(f"{namespace}/"):
        candidates.insert(0, f"{namespace}/{key}")
    for candidate in candidates:
        document = await session.scalar(select(SharedDocument).where(
            SharedDocument.workspace_id == workspace_id, SharedDocument.doc_key == candidate))
        if document is not None:
            return document
    try:
        return await address_service.find_document(session, workspace_id, key)
    except ApiError:
        return None


async def validate_new_task(session: AsyncSession, principal: Principal, request: Any) -> None:
    """AG-CO-02 points 1-2: context exists, the assignee is a known principal.
    Context pointers are stored in their canonical form (B-01)."""
    if request.assignee and not await principal_exists(
            session, principal.workspace.id, request.assignee):
        raise ApiError(422, "unknown_principal",
                       "the assignee is not in the directory (labels are case-sensitive)",
                       details={"assignee": request.assignee,
                                "did_you_mean": await suggest_principal(
                                    session, principal.workspace.id, request.assignee)})
    missing = []
    for ref in request.context_docs or []:
        document = await resolve_ref(session, principal.workspace.id, ref.model_dump())
        if document is not None:
            ref.namespace, ref.doc_key = document.namespace, document.doc_key
            ensure_namespace(principal, document.namespace)
            if ref.version is None or ref.version <= document.version:
                continue
        else:
            ensure_namespace(principal, ref.namespace)
        missing.append(f"{ref.namespace}/{ref.doc_key}")
    if missing and not request.allow_missing_context:
        raise ApiError(422, "context_doc_not_found", "context documents do not exist",
                       details={"missing": missing,
                                "hint": "pass allow_missing_context=true to create anyway"})


def apply_new_fields(task: CollabTask, request: Any) -> None:
    task.context_docs = [ref.model_dump() for ref in request.context_docs]
    task.done_definition = request.done_definition
    task.output_to = request.output_to.model_dump() if request.output_to else None
    task.required_role = request.required_role
    task.required_capabilities = list(request.required_capabilities)
    task.kind = request.kind
    task.require_continuation = request.require_continuation


# ------------------------------------------------------------------ claim


async def check_claimable(session: AsyncSession, principal: Principal, task: CollabTask) -> None:
    """required_role / required_capabilities against the claimer's directory entry."""
    if not (task.required_role or task.required_capabilities) or principal.is_human:
        return
    entry = await _my_entry(session, principal)
    role = (entry or {}).get("role")
    capabilities = set((entry or {}).get("capabilities") or [])
    if task.required_role and role != task.required_role:
        raise ApiError(403, "role_mismatch", "this task needs another role",
                       details={"required_role": task.required_role, "your_role": role})
    missing = sorted(set(task.required_capabilities or []) - capabilities)
    if missing:
        raise ApiError(403, "role_mismatch", "this task needs capabilities you lack",
                       details={"required_capabilities": task.required_capabilities,
                                "missing": missing})


async def assignee_matches(session: AsyncSession, principal: Principal, task: CollabTask,
                           agent: str) -> bool:
    if not task.assignee:
        return True
    allowed = {f"agent:{agent}", f"human:{agent}"}
    if principal.user_id is not None:
        allowed.add(f"human:{principal.user_id}")
    entry = await _my_entry(session, principal)
    if entry is not None:
        allowed |= {f"agent:{entry['agent_id']}", f"agent:{entry['principal_id']}"}
    return task.assignee in allowed


async def load_context(session: AsyncSession, principal: Principal, task: CollabTask, *,
                       max_bytes: int) -> list[dict[str, Any]]:
    """Pin unversioned pointers at the first claim; return the pinned versions."""
    pinned = dict(task.context_versions_pinned or {})
    refs = list(task.context_docs or [])
    changed = False
    items: list[dict[str, Any]] = []
    budget = max_bytes
    reads: list[tuple[SharedDocument, int, bool]] = []
    pointers: list[tuple[dict[str, Any], bool]] = []
    if task.continuation:
        pointers.append((dict(task.continuation), True))
    pointers += [(ref, False) for ref in refs]
    for ref, is_continuation in pointers:
        document = await resolve_ref(session, task.workspace_id, ref)
        key = _ref_key(ref)
        if document is None:
            items.append({"doc_ref": ref, "missing": True, "is_continuation": is_continuation})
            continue
        try:
            ensure_namespace(principal, document.namespace)
        except ApiError:
            items.append({"doc_ref": ref, "forbidden": True, "is_continuation": is_continuation})
            continue
        if is_continuation:
            version = document.version
        else:
            version = ref.get("version") or pinned.get(key)
            if version is None:
                version = document.version
                pinned[key] = version
                changed = True
        row = (await session.execute(
            text("SELECT content, tainted FROM document_versions WHERE document_id = :d "
                 "AND version = :v"), {"d": document.id, "v": version},
        )).one_or_none()
        body, version_tainted = (row[0], bool(row[1])) if row else (
            document.content if version == document.version else "", bool(document.tainted))
        reads.append((document, version, version_tainted))
        raw = body.encode("utf-8")
        truncated = len(raw) > budget
        if truncated:
            body = raw[:max(0, budget)].decode("utf-8", errors="ignore")
        budget = max(0, budget - len(body.encode("utf-8")))
        untrusted = version_tainted
        item = {
            "doc_ref": {"namespace": document.namespace, "doc_key": document.doc_key,
                        "id": document.canonical_id},
            "version": version,
            "untrusted_content" if untrusted else "content": body,
            "tainted": version_tainted,
            "trust_level": document.trust_level,
            "truncated": truncated,
            "newer_version_available": document.version > version,
            "is_continuation": is_continuation,
        }
        items.append(item)
    if changed:
        task.context_versions_pinned = pinned
    from app.services.provenance import HANDLING, note_versions

    for item in items:
        if item.get("tainted"):
            item["handling"] = HANDLING
    await note_versions(principal, reads)
    return items


# ---------------------------------------------------------------- handoff


async def handoff_checks(session: AsyncSession, principal: Principal, task: CollabTask,
                         agent: str, next_status: str, brief: dict[str, Any] | None) -> None:
    """Continuation, output and rollup-summary rules before a handoff."""
    wrote_continuation = bool(task.continuation_at and task.claimed_at
                              and task.continuation_at >= task.claimed_at)
    if not wrote_continuation:
        if task.require_continuation:
            raise ApiError(422, "continuation_required",
                           "write a continuation pack before handing this task off",
                           details={"write_with": "write_continuation"})
        add_warning("continuation_missing",
                    "no continuation pack was written since the claim",
                    write_with="write_continuation")
    if task.parent_task_id and next_status in ("REVIEWING", "DONE") and not (
            brief and (brief.get("summary") or "").strip()):
        raise ApiError(422, "rollup_summary_required",
                       "a subtask's submission needs brief.summary for the parent's rollup",
                       details={"parent_task_key": task.parent_task_key})
    if task.output_to and not await _output_written(session, principal, task, agent):
        add_warning("output_missing", "nothing was written to output_to since the claim",
                    output_to=task.output_to)
    if task.parent_task_id and brief:
        task.output_summary = (brief.get("summary") or "")[:500]


async def _output_written(session: AsyncSession, principal: Principal, task: CollabTask,
                          agent: str) -> bool:
    target = task.output_to or {}
    namespace = target.get("namespace") or "default"
    since = task.claimed_at or task.updated_at
    rows = (
        await session.execute(
            text("SELECT d.doc_key FROM document_versions v JOIN shared_documents d ON "
                 "d.id = v.document_id WHERE v.workspace_id = :ws AND d.namespace = :ns AND "
                 "v.created_at >= :since AND (v.author = :agent OR "
                 "v.stamp->>'token_id' = :token)"),
            {"ws": task.workspace_id, "ns": namespace, "since": since, "agent": agent,
             "token": principal.token_cid or ""},
        )
    ).scalars().all()
    if target.get("doc_key"):
        return target["doc_key"] in rows
    if target.get("doc_key_pattern"):
        return any(fnmatch.fnmatch(key, target["doc_key_pattern"]) for key in rows)
    return bool(rows)


# ------------------------------------------------------------ continuation


def _render(task: CollabTask, author: str, data: dict[str, Any], seq: int) -> str:
    def section(title: str, items: list[str]) -> list[str]:
        return [f"## {title}", "", *([f"- {item}" for item in items] or ["- none"]), ""]

    tried = [f"{item['approach']}: **{item['result']}**"
             + (f" ({item['note']})" if item.get("note") else "")
             for item in data.get("tried") or []]
    artifacts = [f"`{ref.get('namespace') or 'default'}/{ref['doc_key']}`"
                 + (f" v{ref['version']}" if ref.get("version") else "")
                 for ref in data.get("artifacts") or []]
    lines = [f"# Continuation {seq}: {task.title}", "",
             f"- Task: `{task.canonical_id}` (`{task.task_key}`)", f"- Author: `{author}`", ""]
    lines += section("Done / 已完成", list(data.get("done") or []))
    lines += section("Tried / 试过什么", tried)
    lines += section("Avoid / 不要重复", list(data.get("avoid") or []))
    lines += section("Next steps / 下一步", list(data.get("next_steps") or []))
    lines += section("Open questions / 未决问题", list(data.get("open_questions") or []))
    lines += section("Artifacts / 产出物", artifacts)
    if data.get("state"):
        lines += ["## State", "", "```json", json.dumps(data["state"], indent=2,
                                                          ensure_ascii=False), "```", ""]
    return "\n".join(lines)


async def write_continuation(session: AsyncSession, principal: Principal, task: CollabTask,
                             agent: str, data: dict[str, Any], *,
                             auto: bool = False) -> dict[str, Any]:
    """A new version of handoff/<task> and the task's pointer to it (AG-CO-04)."""
    from app.services.leases import CONTINUATION_NAMESPACE, continuation_key
    from app.services.onboarding import _find, write_platform_document

    if not auto and not principal.is_human and task.lease_owner != agent:
        raise Forbidden("only the task's current holder (or a human) writes its continuation")
    key = continuation_key(task)
    existing = await _find(session, task.workspace_id, key)
    seq = int((existing.frontmatter or {}).get("seq", 0)) + 1 if existing else 1
    document = await write_platform_document(
        session, task.workspace_id, namespace=CONTINUATION_NAMESPACE, doc_key=key,
        title=f"Continuation: {task.title}"[:256],
        content=_render(task, agent, data, seq) + "\n",
        frontmatter={"kind": "continuation", "task_id": task.canonical_id, "author": agent,
                     "seq": seq, "auto_generated": auto,
                     "handoff_to": data.get("handoff_to")},
        author=agent, change_summary=f"continuation {seq}",
    )
    task.continuation = {"namespace": document.namespace, "doc_key": document.doc_key,
                         "version": document.version}
    task.continuation_at = _now()
    return task.continuation


async def append_to_continuation(session: AsyncSession, task: CollabTask, note: str) -> None:
    """Add a line under "Open questions" of the latest pack (escalation results)."""
    from app.services.leases import continuation_key
    from app.services.onboarding import _find

    existing = await _find(session, task.workspace_id, continuation_key(task))
    data: dict[str, Any] = {"open_questions": [note]}
    if existing is not None:
        previous = re.search(r"## Open questions / 未决问题\n\n(.*?)\n\n", existing.content,
                             re.S)
        if previous:
            data["open_questions"] = [line[2:] for line in previous.group(1).splitlines()
                                      if line.startswith("- ") and line != "- none"] + [note]
    await write_continuation(session, None, task, "system", data, auto=True)  # type: ignore[arg-type]


# ------------------------------------------------------------------ mentions


def parse_mentions(explicit: list[str] | None, body: str | None) -> list[str]:
    found = list(explicit or [])
    found += [match.group(1) for match in _MENTION.finditer(body or "")]
    clean = []
    for item in found:
        item = item.rstrip(".,;:!?)")
        if item.startswith(("agent:", "human:")) and item not in clean:
            clean.append(item)
    return clean[:20]


async def deliver_mentions(session: AsyncSession, principal: Principal, mentions: list[str],
                           *, refs: dict[str, Any], namespace: str) -> list[str]:
    """mention.created with the mentioned principals (and owners of offline ones) as targets."""
    if not mentions:
        return []
    targets: list[str] = []
    for ref in mentions:
        kind, _, ident = ref.partition(":")
        if not await principal_exists(session, principal.workspace.id, ref):
            add_warning("unknown_mention",
                        "the mentioned principal is not in the directory (labels are "
                        "case-sensitive)", ref=ref,
                        did_you_mean=await suggest_principal(session, principal.workspace.id,
                                                             ref) or None)
            targets.append(ref)
            continue
        targets.append(ref)
        if kind == "agent":
            entry = await _directory_entry(session, principal.workspace.id, ident)
            if entry is not None:
                if entry["agent_id"] != ident:
                    targets.append(f"agent:{entry['agent_id']}")
                if entry["status"] in ("offline", "retired"):
                    add_warning("target_offline", "the mentioned agent is not active; its "
                                "owner is notified too", ref=ref, status=entry["status"])
                    if entry["owner_human"]:
                        targets.append(f"human:{entry['owner_human']}")
    await append_event(session, workspace_id=principal.workspace.id,
                       agent=principal.agent_signature, event_type="MENTION_CREATED",
                       payload={**refs, "mentions": mentions,
                                "targets": list(dict.fromkeys(targets))},
                       namespace=namespace)
    return mentions


# --------------------------------------------------------------- task trees


async def children_summary(session: AsyncSession, task: CollabTask) -> dict[str, Any] | None:
    rows = (
        await session.execute(
            select(CollabTask.status, func.count(), func.max(CollabTask.updated_at))
            .where(CollabTask.parent_task_id == task.id).group_by(CollabTask.status)
        )
    ).all()
    if not rows:
        return None
    by_status = {status.value: count for status, count, _ in rows}
    return {
        "total": sum(by_status.values()),
        "by_status": by_status,
        "blocked": by_status.get("WAITING_HUMAN", 0) + by_status.get("AWAITING_HUMAN", 0),
        "orphaned": by_status.get("ESCALATED_TO_HUMAN", 0),
        "last_activity_at": max(seen for _, _, seen in rows),
    }


def may_lead(principal: Principal, task: CollabTask, agent: str) -> bool:
    if principal.is_human or principal.auth_kind is AuthKind.WORKSPACE:
        return True
    return agent in (task.lease_owner, task.claimed_by) or (
        task.lead is not None and task.lead in (agent, f"agent:{agent}"))


async def on_done(session: AsyncSession, task: CollabTask) -> None:
    """Register the rollup on the parent; tell the lead when all children are done."""
    if task.parent_task_id is None:
        return
    await session.flush()
    parent = await session.get(CollabTask, task.parent_task_id, with_for_update=True)
    if parent is None:
        return
    output_ref = None
    if task.output_to and task.output_to.get("doc_key"):
        output_ref = f"{task.output_to.get('namespace') or 'default'}/{task.output_to['doc_key']}"
    elif task.artifact_uri:
        output_ref = task.artifact_uri
    rollup = [item for item in (parent.rollup or []) if item.get("task_id") != task.canonical_id]
    rollup.append({"task_id": task.canonical_id, "task_key": task.task_key,
                   "output_ref": output_ref,
                   "summary": task.output_summary or (task.handoff_brief or {}).get("summary")})
    parent.rollup = rollup
    remaining = await session.scalar(
        select(func.count()).select_from(CollabTask).where(
            CollabTask.parent_task_id == parent.id,
            CollabTask.status.notin_([TaskStatus.DONE, TaskStatus.CANCELLED]),
        )
    )
    if not remaining and not parent.children_done:
        parent.children_done = True
        await append_event(session, workspace_id=parent.workspace_id, agent="task_tree",
                           event_type="TASK_CHILDREN_DONE",
                           payload={"task_key": parent.task_key, "id": parent.canonical_id,
                                    "children": len(rollup),
                                    "targets": [parent.lead] if parent.lead else []},
                           namespace=parent.namespace)
    parent.version += 1
    parent.updated_at = _now()


async def cancel_children(session: AsyncSession, parent: CollabTask, reason: str) -> list[str]:
    """Cancelling a parent cancels its unfinished descendants and frees their leases."""
    from app.services import leases as lease_service

    cancelled: list[str] = []
    frontier = [parent.id]
    while frontier:
        children = (
            await session.execute(
                select(CollabTask).where(CollabTask.parent_task_id.in_(frontier))
                .with_for_update()
            )
        ).scalars().all()
        frontier = [child.id for child in children]
        for child in children:
            if child.status in _OPEN:
                continue
            lease = await lease_service.active_task_lease(session, child)
            if lease is not None:
                lease.state = "released"
                lease.ended_at = _now()
                lease.end_reason = "parent_cancelled"
            lease_service.clear_task_lease(child)
            child.status = TaskStatus.CANCELLED
            child.review_feedback = reason
            child.version += 1
            child.updated_at = _now()
            cancelled.append(child.task_key)
            await append_event(session, workspace_id=child.workspace_id, agent="task_tree",
                               event_type="TASK_CANCELLED",
                               payload={"task_key": child.task_key, "reason": reason,
                                        "parent": parent.task_key},
                               namespace=child.namespace)
    return cancelled


async def task_tree(session: AsyncSession, principal: Principal, root: CollabTask, *,
                    depth: int, include: list[str]) -> dict[str, Any]:
    nodes = 0

    async def build(task: CollabTask, level: int) -> dict[str, Any]:
        nonlocal nodes
        nodes += 1
        node: dict[str, Any] = {"task_key": task.task_key, "id": task.canonical_id,
                                "title": task.title, "status": task.status.value,
                                "lead": task.lead, "children_done": task.children_done}
        summary = await children_summary(session, task)
        if "summary" in include or level >= depth:
            node["children_summary"] = summary
        if "rollup" in include:
            node["rollup"] = task.rollup or []
        if summary and level < depth and nodes < TREE_NODE_LIMIT:
            children = (
                await session.execute(
                    select(CollabTask).where(CollabTask.parent_task_id == task.id)
                    .order_by(CollabTask.created_at)
                )
            ).scalars().all()
            node["children"] = []
            for child in children:
                if nodes >= TREE_NODE_LIMIT:
                    node["truncated"] = True
                    break
                node["children"].append(await build(child, level + 1))
        return node

    tree = await build(root, 0)
    return {"root": tree, "nodes": nodes, "depth": depth,
            "truncated": nodes >= TREE_NODE_LIMIT}


# --------------------------------------------------------------- escalation


async def escalate(session: AsyncSession, principal: Principal, task: CollabTask | None,
                   agent: str, request: Any) -> dict[str, Any]:
    """AG-CO-05: a question for a human; blocking pauses the task and its lease."""
    option_ids = [option.id for option in request.options]
    if request.recommended_option and request.recommended_option not in option_ids:
        raise ApiError(422, "validation_error", "recommended_option must be one of the options",
                       details={"field": "recommended_option", "options": option_ids})
    if request.default_on_timeout and request.default_on_timeout not in option_ids:
        raise ApiError(422, "validation_error", "default_on_timeout must be one of the options",
                       details={"field": "default_on_timeout", "options": option_ids})
    route_to = request.route_to
    if route_to is None:
        entry = await _my_entry(session, principal)
        route_to = f"human:{entry['owner_human']}" if entry and entry["owner_human"] else None
    tainted = False
    for ref in request.context_docs:
        flag = await session.scalar(
            select(SharedDocument.tainted).where(
                SharedDocument.workspace_id == principal.workspace.id,
                SharedDocument.doc_key == ref.doc_key)
        )
        tainted = tainted or bool(flag)
    from app.models import CollabTask as TaskModel

    key = f"ESC-{uuid.uuid4().hex[:8].upper()}"
    escalation_task = TaskModel(
        workspace_id=principal.workspace.id, task_key=key,
        title=f"Decision needed: {request.question[:200]}",
        description=request.question, status=TaskStatus.AWAITING_HUMAN, priority=50,
        namespace=task.namespace if task else "default", kind="escalation",
        assignee=route_to, version=1, created_by=agent, created_at=_now(),
        updated_at=_now(), input_context={"options": [option.model_dump()
                                                      for option in request.options]},
        context_docs=[ref.model_dump() for ref in request.context_docs],
        tainted=tainted,
    )
    session.add(escalation_task)
    await session.flush()
    blocking = bool(getattr(request, "blocking", False)) and task is not None
    escalation_id = await session.scalar(
        text("INSERT INTO escalations (workspace_id, task_id, escalation_task_id, raised_by, "
             "raised_by_token, question, options, recommended_option, context_docs, "
             "tainted_context, blocking, due_at, default_on_timeout, route_to) VALUES "
             "(:ws, :task, :esc, :by, :token, :q, CAST(:opts AS jsonb), :rec, "
             "CAST(:ctx AS jsonb), :tainted, :blocking, :due, :default, :route) RETURNING id"),
        {"ws": principal.workspace.id, "task": task.id if task else None,
         "esc": escalation_task.id, "by": agent, "token": principal.token_cid,
         "q": request.question,
         "opts": json.dumps([option.model_dump() for option in request.options]),
         "rec": request.recommended_option,
         "ctx": json.dumps([ref.model_dump() for ref in request.context_docs]),
         "tainted": tainted, "blocking": blocking, "due": request.due_at,
         "default": request.default_on_timeout, "route": route_to},
    )
    if blocking:
        task.status = TaskStatus.WAITING_HUMAN
        task.lease_paused_at = _now()
        task.version += 1
        task.updated_at = _now()
    targets = [route_to] if route_to else []
    if not targets:
        targets = [f"human:{row}" for row in (
            await session.execute(text("SELECT id FROM human_users WHERE workspace_id = :ws "
                                       "AND is_active"), {"ws": principal.workspace.id})
        ).scalars().all()]
    await append_event(session, workspace_id=principal.workspace.id, agent=agent,
                       event_type="TASK_ESCALATED",
                       payload={"task_key": task.task_key if task else None,
                                "escalation_id": escalation_id, "question": request.question,
                                "escalation_task": key, "blocking": blocking,
                                "tainted_context": tainted, "targets": targets},
                       namespace=task.namespace if task else "default")
    return {"escalation_id": escalation_id, "escalation_task_key": key, "blocking": blocking,
            "route_to": route_to, "tainted_context": tainted,
            "task": task_out(task).model_dump(mode="json") if task else None}


async def _holder_alive(session: AsyncSession, escalation: dict[str, Any]) -> bool:
    if not escalation["raised_by_token"]:
        return True
    row = (
        await session.execute(
            text("SELECT revoked_at, is_active, expires_at FROM agent_tokens "
                 "WHERE canonical_id = :t"), {"t": escalation["raised_by_token"]})
    ).one_or_none()
    if row is None:  # the workspace key or the root: always alive
        return True
    revoked, active, expires = row
    return revoked is None and active and (expires is None or expires > _now())


async def resolve(session: AsyncSession, principal: Principal | None, escalation_id: str, *,
                  option_id: str | None, answer: str | None, note: str | None,
                  resolved_by: str | None = None) -> dict[str, Any]:
    from app.services import leases as lease_service

    if principal is not None and not principal.is_human:
        raise Forbidden("only a human resolves an escalation")
    row = (
        await session.execute(
            text("SELECT * FROM escalations WHERE id = :id FOR UPDATE"), {"id": escalation_id})
    ).mappings().one_or_none()
    if row is None or (principal is not None and row["workspace_id"] != principal.workspace.id):
        raise NotFound("escalation", f"escalation '{escalation_id}' not found")
    escalation = dict(row)
    if escalation["status"] == "resolved":
        raise ApiError(409, "request_closed", "this escalation is already resolved",
                       details={"resolved_at": escalation["resolved_at"].isoformat()})
    option_ids = [option["id"] for option in escalation["options"] or []]
    if option_id is not None and option_id not in option_ids:
        raise ApiError(422, "validation_error", "option_id must be one of the options",
                       details={"field": "option_id", "options": option_ids})
    if option_id is None and not answer:
        raise ApiError(422, "validation_error", "choose an option or answer in text",
                       details={"fields": ["option_id", "answer"]})
    by = resolved_by or (f"human:{principal.user_id}" if principal and principal.user_id
                         else "human:admin")
    await session.execute(
        text("UPDATE escalations SET status = 'resolved', option_id = :o, answer = :a, "
             "note = :n, resolved_by = :by, resolved_at = NOW() WHERE id = :id"),
        {"o": option_id, "a": answer, "n": note, "by": by, "id": escalation_id})
    if escalation["escalation_task_id"]:
        esc_task = await session.get(CollabTask, escalation["escalation_task_id"])
        if esc_task is not None:
            esc_task.status = TaskStatus.DONE
            esc_task.output_artifact = {"option_id": option_id, "answer": answer, "note": note,
                                        "resolved_by": by}
            esc_task.version += 1
            esc_task.updated_at = _now()
    outcome = None
    if escalation["task_id"]:
        task = await session.get(CollabTask, escalation["task_id"], with_for_update=True)
        if task is not None:
            decision = f"{escalation['question']} → {option_id or answer}" + (
                f" ({note})" if note else "") + f" — {by}"
            await append_to_continuation(session, task, decision)
            if task.status is TaskStatus.WAITING_HUMAN:
                if await _holder_alive(session, escalation):
                    paused = _now() - (task.lease_paused_at or _now())
                    if task.lease_expires_at is not None:
                        task.lease_expires_at = task.lease_expires_at + paused
                    lease = await lease_service.active_task_lease(session, task)
                    if lease is not None and lease.expires_at is not None:
                        lease.expires_at = lease.expires_at + paused
                    task.status = TaskStatus.IN_PROGRESS
                    outcome = "IN_PROGRESS"
                else:
                    await lease_service.orphan_task(session, task, state="revoked",
                                                    reason="holder_gone", actor="escalation")
                    outcome = task.status.value
                task.lease_paused_at = None
                task.version += 1
                task.updated_at = _now()
    await append_event(
        session, workspace_id=escalation["workspace_id"], agent="escalation",
        event_type="ESCALATION_RESOLVED",
        payload={"escalation_id": escalation_id, "targets": [f"agent:{escalation['raised_by']}"],
                 "details": {"option_id": option_id, "answer": answer, "resolved_by": by},
                 "task_status": outcome})
    await session.commit()
    return {"escalation_id": escalation_id, "status": "resolved", "option_id": option_id,
            "answer": answer, "resolved_by": by, "task_status": outcome}


async def escalation_upkeep(session: AsyncSession) -> int:
    """Timeout defaults; reminders every 24 hours (AG-CO-05 point 5)."""
    rows = (
        await session.execute(
            text("SELECT * FROM escalations WHERE status = 'open' AND due_at IS NOT NULL AND "
                 "due_at < NOW() ORDER BY due_at LIMIT 50")
        )
    ).mappings().all()
    done = 0
    for row in rows:
        if row["default_on_timeout"]:
            await resolve(session, None, row["id"], option_id=row["default_on_timeout"],
                          answer=None, note="no decision before due_at",
                          resolved_by="timeout_default")
            done += 1
        elif row["last_reminded_at"] is None or row["last_reminded_at"] < _now() - timedelta(
                hours=24):
            await session.execute(
                text("UPDATE escalations SET last_reminded_at = NOW() WHERE id = :id"),
                {"id": row["id"]})
            await append_event(session, workspace_id=row["workspace_id"], agent="escalation",
                               event_type="ESCALATION_REMINDER",
                               payload={"escalation_id": row["id"],
                                        "targets": [row["route_to"]] if row["route_to"] else []})
            await session.commit()
    return done


async def context_grants(session: AsyncSession, principal: Principal) -> frozenset[str]:
    """B-02: being assigned a task (or holding it) grants reading its context
    documents whatever their review state; the canonical keys of those."""
    if principal.is_commander or principal.is_human:
        return frozenset()
    names = [f"agent:{principal.agent_signature}"]
    entry = await _my_entry(session, principal)
    if entry is not None:
        names += [f"agent:{entry['agent_id']}", f"agent:{entry['principal_id']}"]
    rows = (await session.execute(text(
        "SELECT context_docs FROM collab_tasks WHERE workspace_id = :ws "
        "AND status NOT IN ('DONE', 'FAILED', 'CANCELLED') "
        "AND jsonb_array_length(context_docs) > 0 "
        "AND (claimed_by = :me OR assignee = ANY(CAST(:names AS text[]))) LIMIT 200"),
        {"ws": principal.workspace.id, "me": principal.agent_signature,
         "names": names})).scalars().all()
    keys: set[str] = set()
    for refs in rows:
        for ref in refs or []:
            if not isinstance(ref, dict) or not ref.get("doc_key"):
                continue
            key, namespace = str(ref["doc_key"]), ref.get("namespace") or "default"
            keys.add(key)
            if namespace != "default" and not key.startswith(f"{namespace}/"):
                keys.add(f"{namespace}/{key}")
    return frozenset(keys)
