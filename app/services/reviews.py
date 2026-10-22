"""Peer reviews bound to outputs (PRD AG-CO-06).

``request_peer_review`` opens a ``peer_review`` task on one version of a
document for a principal or a role; ``submit_peer_review`` writes the review
document ``reviews/<namespace>.<doc_key>.v<version>.<reviewer>`` and records
it on the reviewed document (``reviews``). A reviewer can be neither the
author nor inside the author's token subtree (422 ``reviewer_conflict``);
with ``independent`` reviews, another reviewer's review stays sealed until
one's own is submitted (403 ``review_sealed``).
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, effective_agent, require_permission
from app.errors import ApiError, Forbidden, NotFound
from app.models import CollabTask, DocumentVersion, SharedDocument, TaskStatus
from app.services import collab
from app.services.common import append_event

REVIEW_NAMESPACE = "reviews"
_SAFE = re.compile(r"[^a-z0-9_.-]+")


async def _version(session: AsyncSession, principal: Principal, ref: Any
                   ) -> tuple[SharedDocument, DocumentVersion]:
    from app.services.documents import _get_document

    if ref.version is None:
        raise ApiError(422, "validation_error", "output must name a version",
                       details={"field": "output.version"})
    document = await _get_document(session, principal.workspace.id, ref.doc_key)
    version = await session.scalar(
        select(DocumentVersion).where(DocumentVersion.document_id == document.id,
                                      DocumentVersion.version == ref.version))
    if version is None:
        raise NotFound("document_version", f"version {ref.version} not found")
    return document, version


async def _conflict(session: AsyncSession, version: DocumentVersion, reviewer: str,
                    principal: Principal | None = None) -> bool:
    """The author, or a token inside the author's subtree, cannot review."""
    author_token = (version.stamp or {}).get("token_id")
    if principal is not None:
        if principal.agent_signature == version.author and not principal.is_human:
            return True
        if author_token and author_token in (principal.lineage or []):
            return True
    kind, _, ident = reviewer.partition(":")
    if kind != "agent":
        return False
    entry = (await session.execute(
        text("SELECT agent_id, token_ids, signatures FROM agent_directory WHERE "
             "workspace_id = :ws AND (agent_id = :a OR principal_id = :a)"),
        {"ws": version.workspace_id, "a": ident})).mappings().one_or_none()
    names = {ident}
    tokens: list[str] = []
    if entry is not None:
        names |= {entry["agent_id"], *(entry["signatures"] or [])}
        tokens = list(entry["token_ids"] or [])
    if version.author in names:
        return True
    if not author_token or not tokens:
        return False
    return bool(await session.scalar(
        text("SELECT 1 FROM agent_tokens WHERE canonical_id = ANY(:tokens) AND "
             ":author = ANY(lineage)"), {"tokens": tokens, "author": author_token}))


async def _caller_ref(session: AsyncSession, principal: Principal) -> str:
    if principal.is_human and principal.user_id is not None:
        return f"human:{principal.user_id}"
    if principal.token_cid:
        agent_id = await session.scalar(
            text("SELECT agent_id FROM agent_directory WHERE :t = ANY(token_ids)"),
            {"t": principal.token_cid})
        if agent_id:
            return f"agent:{agent_id}"
    return f"agent:{principal.agent_signature}"


async def request_peer_review(session: AsyncSession, principal: Principal, *, output: Any,
                              reviewer: str, kind: str, rubric: str, independent: bool,
                              due_at: datetime | None) -> dict[str, Any]:
    from app.schemas import TaskCreateRequest
    from app.services.common import task_out
    from app.services.tasks import _create_task_core

    require_permission(principal, "task:create")
    document, version = await _version(session, principal, output)
    if reviewer.startswith("prn_"):
        reviewer = f"agent:{reviewer}"
    role = reviewer[5:] if reviewer.startswith("role:") else None
    if role is None and not reviewer.startswith(("agent:", "human:")):
        raise ApiError(422, "validation_error",
                       "reviewer is agent:<id>, human:<id> or role:<role>",
                       details={"field": "reviewer"})
    if role is None and await _conflict(session, version, reviewer):
        raise ApiError(422, "reviewer_conflict",
                       "a reviewer can be neither the author nor in the author's subtree",
                       details={"reviewer": reviewer, "author": version.author})
    ref = {"namespace": document.namespace, "doc_key": document.doc_key,
           "version": version.version}
    request = TaskCreateRequest(
        title=f"{kind.replace('_', ' ').title()}: {document.namespace}/{document.doc_key} "
              f"v{version.version}"[:256],
        description=rubric, kind="peer_review", namespace=document.namespace,
        context_docs=[ref], assignee=None if role else reviewer, required_role=role,
        input_context={"peer_review": {"output": ref, "kind": kind, "rubric": rubric,
                                       "rubric_hash": hashlib.sha256(
                                           rubric.encode()).hexdigest(),
                                       "independent": independent,
                                       "due_at": due_at.isoformat() if due_at else None,
                                       "author": version.author,
                                       "requested_by": principal.agent_signature}},
    )
    agent = effective_agent(principal, None)
    import uuid as _uuid

    task = await _create_task_core(session, principal, request, agent=agent,
                                   batch=_uuid.uuid4(), commit=True)
    return {"task": task_out(task).model_dump(mode="json"), "output": ref,
            "reviewer": reviewer, "independent": independent}


def _review_key(ref: dict[str, Any], reviewer: str) -> str:
    who = _SAFE.sub("-", reviewer.split(":", 1)[-1].lower()).strip("-") or "reviewer"
    return (f"{REVIEW_NAMESPACE}/{ref['namespace']}.{ref['doc_key'].replace('/', '.')}"
            f".v{ref['version']}.{who}")[:256]


async def submit_peer_review(session: AsyncSession, principal: Principal, task_key: str, *,
                             verdict: str, findings: list[dict[str, Any]],
                             summary: str) -> dict[str, Any]:
    from app.services.onboarding import write_platform_document
    from app.services.tasks import _get_task

    require_permission(principal, "task:transition")
    agent = effective_agent(principal, None)
    task = await _get_task(session, principal.workspace.id, task_key, for_update=True)
    meta = (task.input_context or {}).get("peer_review")
    if task.kind != "peer_review" or not meta:
        raise ApiError(409, "invalid_state_transition", "this is not a peer review task",
                       details={"kind": task.kind})
    if task.status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED):
        raise ApiError(409, "invalid_state_transition", "the review was already submitted",
                       details={"status": task.status.value})
    reviewer = await _caller_ref(session, principal)
    if not await collab.assignee_matches(session, principal, task, agent) \
            and task.lease_owner != agent:
        raise Forbidden("this review is assigned to another principal")
    document, version = await _version(session, principal, _Ref(meta["output"]))
    if await _conflict(session, version, reviewer, principal):
        raise ApiError(422, "reviewer_conflict",
                       "a reviewer can be neither the author nor in the author's subtree",
                       details={"reviewer": reviewer, "author": version.author})
    ref = meta["output"]
    key = _review_key(ref, reviewer)
    lines = [f"# Review of {ref['namespace']}/{ref['doc_key']} v{ref['version']}", "",
             f"- Verdict: **{verdict}**", f"- Reviewer: `{reviewer}`",
             f"- Kind: {meta['kind']}", "", "## Summary", "", summary or "-", "",
             "## Findings", ""]
    lines += [f"- [{item.get('severity')}] {item.get('location') or ''}: {item.get('note')}"
              for item in findings] or ["- none"]
    review = await write_platform_document(
        session, principal.workspace.id, namespace=REVIEW_NAMESPACE, doc_key=key,
        title=f"Review: {ref['doc_key']} v{ref['version']}"[:256],
        content="\n".join(lines) + "\n",
        frontmatter={"kind": "peer_review", "reviewed_ref": ref, "verdict": verdict,
                     "reviewer": reviewer, "rubric_hash": meta["rubric_hash"],
                     "independent": meta["independent"], "findings": findings,
                     "review_kind": meta["kind"]},
        author=agent, change_summary=f"peer review: {verdict}")
    entry = {"reviewer": reviewer, "verdict": verdict, "version": ref["version"],
             "review_ref": {"namespace": REVIEW_NAMESPACE, "doc_key": review.doc_key},
             "kind": meta["kind"], "at": datetime.now(timezone.utc).isoformat()}
    reviewed = await session.get(SharedDocument, document.id, with_for_update=True)
    reviewed.reviews = [*(reviewed.reviews or []), entry]
    task.status = TaskStatus.DONE
    task.output_artifact = {"verdict": verdict, "review_ref": entry["review_ref"]}
    task.version += 1
    task.updated_at = datetime.now(timezone.utc)
    await append_event(
        session, workspace_id=principal.workspace.id, agent=agent,
        event_type="PEER_REVIEW_SUBMITTED",
        payload={"doc_key": document.doc_key, "id": document.canonical_id,
                 "targets": [f"agent:{version.author}"],
                 "details": {"verdict": verdict, "reviewer": reviewer,
                             "version": ref["version"], "review_ref": entry["review_ref"]}},
        namespace=document.namespace)
    await session.commit()
    return {"review": entry, "task_key": task.task_key, "status": "DONE"}


class _Ref:
    def __init__(self, data: dict[str, Any]) -> None:
        self.namespace = data.get("namespace")
        self.doc_key = data["doc_key"]
        self.version = data.get("version")


async def check_sealed(session: AsyncSession, principal: Principal,
                       document: SharedDocument) -> None:
    """An independent reviewer reads others' reviews only after submitting."""
    frontmatter = document.frontmatter or {}
    if frontmatter.get("kind") != "peer_review" or not frontmatter.get("independent"):
        return
    if principal.is_human or principal.is_commander:
        return
    reviewed = frontmatter.get("reviewed_ref") or {}
    me = await _caller_ref(session, principal)
    if frontmatter.get("reviewer") == me:
        return
    pending = (await session.execute(
        select(CollabTask).where(
            CollabTask.workspace_id == principal.workspace.id,
            CollabTask.kind == "peer_review",
            CollabTask.status.notin_([TaskStatus.DONE, TaskStatus.FAILED,
                                      TaskStatus.CANCELLED]),
        ))).scalars().all()
    for task in pending:
        output = ((task.input_context or {}).get("peer_review") or {}).get("output") or {}
        if (output.get("doc_key"), output.get("version")) != (reviewed.get("doc_key"),
                                                              reviewed.get("version")):
            continue
        if task.assignee in (me, f"agent:{principal.agent_signature}") or \
                task.lease_owner == principal.agent_signature:
            raise ApiError(403, "review_sealed",
                           "submit your own review before reading the others",
                           details={"reviewed_ref": reviewed, "task_key": task.task_key})
