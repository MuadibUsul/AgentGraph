"""Automated quality control 1.0 (工程设计 执行版 1.0).

What is controlled is the pipeline's statistics, not each output: gate results,
independent audits, sampling, escapes, a per-domain circuit breaker and
autonomy tiers. Three invariants hold throughout: author, auditor and merger
are never the same principal; a check a machine can assert is never left to an
agent's judgement; only people raise a tier, the system may lower it.

A domain (``scope_key``) is a task's namespace, or ``owner/repo:path`` for a
change made outside AgentGraph (a change claim). No ``autonomy_tiers`` row
means tier 0. Gate results, audits, claims, samples and escapes are always
recorded; ``QUALITY_CONTROL=true`` enforces the release matrix on reviews and
hand-offs (§6).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import uuid
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, RoleScope, can, ensure_namespace, require_permission
from app.config import get_settings
from app.errors import ApiError, Forbidden, NotFound
from app.models import CollabTask, TaskStatus
from app.schemas import (
    AcceptanceUpdateRequest,
    AutonomyTierRequest,
    ChangeClaimRequest,
    ChangeClaimVerifyRequest,
    EscapeCreateRequest,
    EscapeResolveRequest,
    GateResultRequest,
    PoisonTaskRequest,
    SampleVerdictRequest,
    TaskReviewRequest,
)
from app.services.common import append_event, db_now, task_out, utcnow
from app.warnings_context import add_warning

settings = get_settings()

HUMAN = "human"
AUDITOR_POOL = "AUDITOR"  # RISK samples any AUDITOR token may claim
RELEASE_GATES = {0: ("CI", "ACCEPTANCE"), 1: ("CI", "ACCEPTANCE"),
                 2: ("CI", "TESTS", "ACCEPTANCE")}
CLAIM_TIER_CAP = 1  # Q-12: no trusted runner for outside changes yet
SENSITIVE_NAMESPACES = frozenset({"system"})  # Q-8
SENSITIVE_PATHS = ("migrations/", "auth/")  # Q-8
WINDOWS = {"7d": 7, "30d": 30}


def enforced() -> bool:
    return settings.quality_control


def _is_repo_scope(scope_key: str) -> bool:
    return "/" in scope_key  # namespaces never contain "/"


def _json(value: Any) -> str:
    return json.dumps(value, default=str)


# ------------------------------------------------------------------ units


async def _task(session: AsyncSession, principal: Principal, ref: str, *,
                for_update: bool = False) -> CollabTask:
    query = select(CollabTask).where(
        CollabTask.workspace_id == principal.workspace.id,
        or_(CollabTask.task_key == ref, CollabTask.canonical_id == ref))
    if for_update:
        query = query.with_for_update()
    task = await session.scalar(query)
    if task is None:
        raise NotFound("task")
    return task


async def _claim(session: AsyncSession, principal: Principal, claim_id: Any, *,
                 for_update: bool = False) -> dict[str, Any]:
    sql = ("SELECT * FROM change_claims WHERE workspace_id = :ws AND id = :id"
           + (" FOR UPDATE" if for_update else ""))
    row = (await session.execute(text(sql), {"ws": principal.workspace.id,
                                             "id": str(claim_id)})).mappings().one_or_none()
    if row is None:
        raise NotFound("change_claim")
    return dict(row)


def _executors(task: CollabTask) -> set[str]:
    return {name for name in (task.claimed_by, task.handed_off_by) if name}


# ------------------------------------------------------------ tiers, circuit


async def configured_tier(session: AsyncSession, workspace_id: Any, scope_key: str) -> int:
    if not _is_repo_scope(scope_key):
        if scope_key in SENSITIVE_NAMESPACES:
            return 0
        tier = await session.scalar(text(
            "SELECT tier FROM autonomy_tiers WHERE workspace_id = :ws "
            "AND scope_type = 'NAMESPACE' AND scope_key = :k"),
            {"ws": workspace_id, "k": scope_key})
        return int(tier or 0)
    repo, _, path = scope_key.partition(":")
    rows = (await session.execute(text(
        "SELECT scope_key, tier FROM autonomy_tiers WHERE workspace_id = :ws "
        "AND scope_type = 'REPO_PATH' AND scope_key LIKE :prefix"),
        {"ws": workspace_id, "prefix": f"{repo}:%"})).all()
    best: tuple[int, str, int] | None = None
    for key, tier in rows:
        glob = key.partition(":")[2] or "*"
        if fnmatch.fnmatch(path, glob) and (best is None or len(glob) > best[0]):
            best = (len(glob), glob, int(tier))
    if best is None:
        return 0
    # Q-8: migrations/ and auth/ stay supervised unless a row names them.
    if any(marker in path and marker not in best[1] for marker in SENSITIVE_PATHS):
        return 0
    return best[2]


async def circuit(session: AsyncSession, workspace_id: Any, scope_key: str) -> dict[str, Any]:
    row = (await session.execute(text(
        "SELECT state, consecutive_failures, opened_at, opened_reason, degraded_tier, "
        "last_closed_at, supervised_next, opened_count FROM quality_circuit "
        "WHERE workspace_id = :ws AND scope_key = :k"),
        {"ws": workspace_id, "k": scope_key})).mappings().one_or_none()
    if row is None:
        return {"state": "CLOSED", "consecutive_failures": 0, "opened_at": None,
                "opened_reason": None, "degraded_tier": None, "last_closed_at": None,
                "supervised_next": False, "opened_count": 0}
    return dict(row)


async def domain(session: AsyncSession, workspace_id: Any, scope_key: str) -> dict[str, Any]:
    """Configured and effective tier of a domain, with its circuit."""
    tier = await configured_tier(session, workspace_id, scope_key)
    state = await circuit(session, workspace_id, scope_key)
    effective = tier
    if state["state"] == "OPEN":
        effective = max(tier - 1, 0)
    if state["supervised_next"]:
        effective = 0  # Q-5: the first change after a close is supervised
    if _is_repo_scope(scope_key):
        effective = min(effective, CLAIM_TIER_CAP)
    return {"scope_key": scope_key, "tier": tier, "effective_tier": effective,
            "circuit_state": state["state"], "supervised_next": state["supervised_next"]}


async def _recent_agents(session: AsyncSession, workspace_id: Any, scope_key: str) -> list[str]:
    if _is_repo_scope(scope_key):
        rows = await session.execute(text(
            "SELECT DISTINCT reporter FROM change_claims WHERE workspace_id = :ws "
            "AND scope_key = :k AND created_at > NOW() - INTERVAL '30 days' LIMIT 20"),
            {"ws": workspace_id, "k": scope_key})
    else:
        rows = await session.execute(text(
            "SELECT DISTINCT handed_off_by FROM collab_tasks WHERE workspace_id = :ws "
            "AND namespace = :k AND handed_off_by IS NOT NULL "
            "AND updated_at > NOW() - INTERVAL '30 days' LIMIT 20"),
            {"ws": workspace_id, "k": scope_key})
    return [row[0] for row in rows]


async def _notify(session: AsyncSession, workspace_id: Any, event_type: str, kind: str, *,
                  scope_key: str, targets: list[str], details: dict[str, Any],
                  task_key: str | None = None) -> None:
    """A quality event through the delivery layer (targets + delivery_kind)."""
    await append_event(
        session, workspace_id=workspace_id, agent="quality", event_type=event_type,
        payload={"targets": list(dict.fromkeys(targets)), "task_key": task_key,
                 "details": {"delivery_kind": kind, "priority": 0, "scope_key": scope_key,
                             **details}},
        namespace=scope_key if not _is_repo_scope(scope_key) else "default")


async def _open_circuit(session: AsyncSession, workspace_id: Any, scope_key: str,
                        reason: str) -> None:
    tier = await configured_tier(session, workspace_id, scope_key)
    opened = await session.scalar(text(
        "INSERT INTO quality_circuit (workspace_id, scope_key, state, opened_at, "
        "opened_reason, degraded_tier, opened_count) VALUES (:ws, :k, 'OPEN', NOW(), :r, :d, 1) "
        "ON CONFLICT (workspace_id, scope_key) DO UPDATE SET state = 'OPEN', "
        "opened_at = NOW(), opened_reason = :r, degraded_tier = :d, "
        "opened_count = quality_circuit.opened_count + 1 "
        "WHERE quality_circuit.state = 'CLOSED' RETURNING scope_key"),
        {"ws": workspace_id, "k": scope_key, "r": reason, "d": max(tier - 1, 0)})
    if opened is None:
        return  # already open
    await _notify(session, workspace_id, "QUALITY_CIRCUIT_OPENED", "quality.circuit_opened",
                  scope_key=scope_key,
                  targets=[HUMAN, *await _recent_agents(session, workspace_id, scope_key)],
                  details={"reason": reason, "tier": tier, "effective_tier": max(tier - 1, 0)})


async def _count_failure(session: AsyncSession, workspace_id: Any, scope_key: str,
                         reason: str) -> None:
    failures = await session.scalar(text(
        "INSERT INTO quality_circuit (workspace_id, scope_key, consecutive_failures) "
        "VALUES (:ws, :k, 1) ON CONFLICT (workspace_id, scope_key) DO UPDATE SET "
        "consecutive_failures = quality_circuit.consecutive_failures + 1 "
        "RETURNING consecutive_failures"), {"ws": workspace_id, "k": scope_key})
    if int(failures or 0) >= settings.quality_circuit_failures:
        await _open_circuit(session, workspace_id, scope_key, reason)


async def _reset_failures(session: AsyncSession, workspace_id: Any, scope_key: str) -> None:
    await session.execute(text(
        "UPDATE quality_circuit SET consecutive_failures = 0 WHERE workspace_id = :ws "
        "AND scope_key = :k AND consecutive_failures > 0"), {"ws": workspace_id, "k": scope_key})


# ------------------------------------------------------------------- gates


def _acceptance_complete(task: CollabTask, items: Any) -> tuple[list[str], list[str]]:
    """(missing, failed) acceptance item ids for a list of {item_id, result}."""
    results = {}
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("item_id"):
            results[str(item["item_id"])] = str(item.get("result", "")).upper()
    wanted = [str(item.get("id")) for item in task.acceptance or [] if isinstance(item, dict)]
    missing = [item for item in wanted if item not in results]
    failed = [item for item in wanted if results.get(item) not in (None, "PASS")]
    return missing, failed


async def gate_status(session: AsyncSession, workspace_id: Any, *,
                      task: CollabTask | None = None,
                      claim_id: Any = None) -> dict[str, dict[str, Any]]:
    """The effective result per gate kind: the latest report wins."""
    column, value = ("task_id", task.id) if task is not None else ("claim_id", claim_id)
    rows = (await session.execute(text(
        f"SELECT DISTINCT ON (gate_kind) gate_kind, status, source, evidence, reported_by, "
        f"created_at FROM quality_gate_runs WHERE workspace_id = :ws AND {column} = :v "
        f"ORDER BY gate_kind, created_at DESC, id DESC"),
        {"ws": workspace_id, "v": value})).mappings().all()
    result = {}
    for row in rows:
        entry = {"status": row["status"], "source": row["source"],
                 "reported_by": row["reported_by"], "at": row["created_at"].isoformat()}
        if row["gate_kind"] == "ACCEPTANCE" and task is not None and row["status"] == "PASS":
            missing, failed = _acceptance_complete(task, (row["evidence"] or {}).get("items"))
            if missing or failed:
                entry.update(status="FAIL", reason="acceptance_items_incomplete",
                             missing=missing, failed=failed)
        result[row["gate_kind"]] = entry
    return result


def missing_gates(status: dict[str, dict[str, Any]], required: tuple[str, ...], *,
                  runner_acceptance: bool = False) -> list[str]:
    missing = []
    for kind in required:
        entry = status.get(kind)
        if entry is None or entry["status"] != "PASS":
            missing.append(kind)
        elif runner_acceptance and kind == "ACCEPTANCE" and entry["source"] != "RUNNER":
            missing.append("ACCEPTANCE:RUNNER")
    return missing


async def record_gate_result(session: AsyncSession, principal: Principal,
                             request: GateResultRequest) -> dict[str, Any]:
    if not (principal.is_human or principal.is_commander or can(principal, "task:read")):
        require_permission(principal, "task:read")
    reporter = principal.agent_signature
    source = request.source
    if source == "RUNNER" and not (principal.is_commander or principal.is_human):
        # A trusted runner reports with the workspace key or a person's session.
        source = "AGENT_CLAIM"
        add_warning("gate_source_downgraded",
                    "only a trusted runner reports RUNNER results; recorded as AGENT_CLAIM")
    task: CollabTask | None = None
    claim: dict[str, Any] | None = None
    if request.claim_id is not None:
        claim = await _claim(session, principal, request.claim_id)
        if claim["status"] == "REFUTED":
            raise ApiError(409, "claim_already_refuted", "the claim was refuted",
                           details={"claim_id": str(claim["id"])})
        scope_key = claim["scope_key"]
    else:
        task = await _task(session, principal, request.task_key or request.task_id or "")
        ensure_namespace(principal, task.namespace)
        scope_key = task.namespace
        if (request.gate_kind == "ACCEPTANCE" and not principal.is_human
                and reporter in _executors(task)):
            raise ApiError(403, "acceptance_self_report",
                           "the executor cannot report its own acceptance",
                           details={"task_key": task.task_key, "executor": reporter})
    row_id = await session.scalar(text(
        "INSERT INTO quality_gate_runs (workspace_id, task_id, claim_id, scope_key, gate_kind, "
        "status, source, evidence, reported_by) VALUES (:ws, :t, :c, :k, :g, :s, :src, "
        "CAST(:e AS jsonb), :by) RETURNING id"),
        {"ws": principal.workspace.id, "t": task.id if task else None,
         "c": str(claim["id"]) if claim else None, "k": scope_key, "g": request.gate_kind,
         "s": request.status, "src": source, "e": _json(request.evidence), "by": reporter})
    if request.status in ("FAIL", "ERROR"):
        await _count_failure(session, principal.workspace.id, scope_key,
                             f"{settings.quality_circuit_failures} consecutive gate failures")
    elif request.status == "PASS":
        await _reset_failures(session, principal.workspace.id, scope_key)
    status = await gate_status(session, principal.workspace.id, task=task,
                               claim_id=claim["id"] if claim else None)
    await session.commit()
    return {"id": row_id, "gate_kind": request.gate_kind, "status": request.status,
            "source": source, "scope_key": scope_key,
            "effective_status": {kind: entry["status"] for kind, entry in status.items()},
            "gates": status}


# ------------------------------------------------------------ acceptance


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()[:16]


async def update_acceptance(session: AsyncSession, principal: Principal, ref: str,
                            request: AcceptanceUpdateRequest) -> dict[str, Any]:
    task = await _task(session, principal, ref, for_update=True)
    ensure_namespace(principal, task.namespace, write=True)
    if not principal.is_human:
        if task.acceptance_locked and principal.agent_signature != task.created_by:
            raise ApiError(403, "acceptance_locked",
                           "acceptance is locked after the first claim; only the task's "
                           "creator or a person can change it",
                           details={"task_key": task.task_key, "created_by": task.created_by})
        if not task.acceptance_locked:
            require_permission(principal, "task:create")
    if request.expected_version is not None and request.expected_version != task.version:
        from app.errors import VersionConflict

        raise VersionConflict(task.version, request.expected_version)
    before = task.acceptance or []
    after = [item.model_dump() for item in request.acceptance]
    if task.acceptance_locked:
        history = list(task.review_history or [])
        history.append({"type": "acceptance_change", "by": principal.agent_signature,
                        "before_hash": _hash(before), "after_hash": _hash(after),
                        "created_at": utcnow().isoformat()})
        task.review_history = history[-settings.max_reject_history:]
    task.acceptance = after
    task.version += 1
    task.updated_at = utcnow()
    await session.commit()
    return task_out(task).model_dump(mode="json")


# ------------------------------------------------------------- audit view


async def audit_view(session: AsyncSession, principal: Principal, ref: str) -> dict[str, Any]:
    """What an auditor should judge from (§5): the spec and the output, not the
    author's account of it. Opening it marks this review round as viewed."""
    require_permission(principal, "task:read")
    task = await _task(session, principal, ref)
    ensure_namespace(principal, task.namespace)
    past = (await session.execute(text(
        "SELECT verdict, cited_clauses, review_round, created_at FROM audits "
        "WHERE workspace_id = :ws AND task_id = :t ORDER BY created_at"),
        {"ws": principal.workspace.id, "t": task.id})).mappings().all()
    await session.execute(text(
        "INSERT INTO audit_view_marks (workspace_id, task_id, viewer, review_round) "
        "VALUES (:ws, :t, :v, :r) ON CONFLICT DO NOTHING"),
        {"ws": principal.workspace.id, "t": task.id, "v": principal.agent_signature,
         "r": task.review_round})
    gates = await gate_status(session, principal.workspace.id, task=task)
    await session.commit()
    return {
        "task_key": task.task_key, "status": task.status.value, "review_round": task.review_round,
        "title": task.title, "description": task.description,
        "acceptance": task.acceptance or [], "output_artifact": task.output_artifact or {},
        "artifact_uri": task.artifact_uri,
        "effective_gate_status": {kind: entry["status"] for kind, entry in gates.items()},
        "gates": gates,
        "audits": [{"verdict": row["verdict"], "cited_clauses": row["cited_clauses"] or [],
                    "review_round": row["review_round"],
                    "created_at": row["created_at"].isoformat()} for row in past],
    }


# ---------------------------------------------------------------- reviews


async def before_review(session: AsyncSession, principal: Principal, task: CollabTask,
                        request: TaskReviewRequest, agent: str) -> dict[str, Any]:
    """Checks that run before a verdict changes anything (§5, §6)."""
    viewed = bool(await session.scalar(text(
        "SELECT 1 FROM audit_view_marks WHERE workspace_id = :ws AND task_id = :t "
        "AND viewer = ANY(CAST(:v AS text[])) AND review_round = :r"),
        {"ws": principal.workspace.id, "t": task.id,
         "v": list({agent, principal.agent_signature}), "r": task.review_round}))
    context: dict[str, Any] = {"view_used": viewed, "round": task.review_round,
                               "next_status": None, "path": None, "domain": None}
    approve = request.decision == "approve"
    if approve and not principal.is_human and task.acceptance:
        missing, failed = _acceptance_complete(
            task, [item.model_dump() for item in request.items])
        if missing or failed:
            raise ApiError(422, "acceptance_items_incomplete",
                           "an approval gives a PASS result for every acceptance item",
                           details={"missing": missing, "failed": failed})
    if not enforced():
        return context
    if not principal.is_human:
        if agent in _executors(task) or principal.agent_signature in _executors(task):
            raise Forbidden("the author of a change cannot audit it")
        if not approve and not request.cited_clauses:
            raise ApiError(422, "review_feedback_required",
                           "a rejection cites the spec clauses it rests on",
                           details={"field": "cited_clauses"})
    target = request.next_status or "DONE"
    if not approve or target != "DONE":
        return context
    info = await domain(session, principal.workspace.id, task.namespace)
    context["domain"] = info
    status = await gate_status(session, principal.workspace.id, task=task)
    missing = missing_gates(status, RELEASE_GATES[min(info["effective_tier"], 1)])
    if missing:
        raise ApiError(409, "quality_gates_unmet", "required gates have not passed",
                       details={"missing": missing, "tier": info["tier"],
                                "effective_tier": info["effective_tier"],
                                "gates": {kind: entry["status"]
                                          for kind, entry in status.items()}})
    if principal.is_human:
        context["path"] = "HUMAN"
        return context
    if info["effective_tier"] == 0:
        context["next_status"] = "AWAITING_HUMAN"  # tier 0: a person releases it
        return context
    if not viewed:
        raise ApiError(422, "audit_view_required",
                       "open the audit view (get_audit_view) before approving",
                       details={"task_key": task.task_key, "review_round": task.review_round})
    context["path"] = "AUDITED"
    return context


async def after_review(session: AsyncSession, principal: Principal, task: CollabTask,
                       request: TaskReviewRequest, context: dict[str, Any]) -> None:
    """The audit row (same transaction as the verdict) and, when done, the release."""
    await session.execute(text(
        "INSERT INTO audits (workspace_id, task_id, auditor, verdict, items, cited_clauses, "
        "feedback, review_round, view_used) VALUES (:ws, :t, :a, :v, CAST(:i AS jsonb), "
        "CAST(:c AS jsonb), :f, :r, :u)"),
        {"ws": principal.workspace.id, "t": task.id, "a": principal.agent_signature,
         "v": "APPROVE" if request.decision == "approve" else "REJECT",
         "i": _json([item.model_dump() for item in request.items]),
         "c": _json(request.cited_clauses), "f": request.feedback, "r": context["round"],
         "u": context["view_used"]})
    if task.status is TaskStatus.DONE:
        await released(session, principal.workspace.id, task,
                       path=context["path"] or ("HUMAN" if principal.is_human else "AUDITED"),
                       info=context["domain"])


async def check_auto_release(session: AsyncSession, principal: Principal,
                             task: CollabTask) -> dict[str, Any]:
    """Tier 2 (§6): an executor may mark its own task DONE when every gate passed
    with acceptance from a trusted runner, under the daily cap, circuit closed."""
    info = await domain(session, principal.workspace.id, task.namespace)
    if info["effective_tier"] < 2:
        if info["tier"] == 2 and info["circuit_state"] == "OPEN":
            raise ApiError(409, "quality_circuit_open",
                           "the domain's circuit is open; its changes need an audit",
                           details={"scope_key": task.namespace, **info})
        raise Forbidden("agents must hand off to REVIEWING; only a human can mark a task DONE")
    status = await gate_status(session, principal.workspace.id, task=task)
    missing = missing_gates(status, RELEASE_GATES[2], runner_acceptance=True)
    if missing:
        raise ApiError(409, "quality_gates_unmet", "required gates have not passed",
                       details={"missing": missing, **info})
    today = await session.scalar(text(
        "SELECT COUNT(*) FROM quality_releases WHERE workspace_id = :ws AND scope_key = :k "
        "AND path = 'AUTO' AND created_at >= date_trunc('day', NOW())"),
        {"ws": principal.workspace.id, "k": task.namespace})
    if int(today or 0) >= settings.quality_auto_merge_daily_cap:
        raise ApiError(409, "auto_release_cap_reached",
                       "today's automatic releases for this domain are used up",
                       details={"cap": settings.quality_auto_merge_daily_cap, **info})
    return info


async def _artifact_bytes(session: AsyncSession, task: CollabTask) -> int:
    if not task.artifact_uri or not task.artifact_uri.startswith("doc://"):
        return 0
    size = await session.scalar(text(
        "SELECT octet_length(content) FROM shared_documents WHERE workspace_id = :ws "
        "AND doc_key = :k ORDER BY version DESC LIMIT 1"),
        {"ws": task.workspace_id, "k": task.artifact_uri.removeprefix("doc://")})
    return int(size or 0)


def _sampled(task_id: Any, rate: float) -> bool:
    """Deterministic: reproducible from the task id, not predictable without it."""
    digest = hashlib.sha256(f"sample:{task_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % 10_000 < int(rate * 10_000)


async def _sample(session: AsyncSession, workspace_id: Any, task_id: Any, reason: str,
                  assigned_to: str) -> None:
    await session.execute(text(
        "INSERT INTO audit_samples (workspace_id, task_id, reason, assigned_to) "
        "VALUES (:ws, :t, :r, :a)"),
        {"ws": workspace_id, "t": task_id, "r": reason, "a": assigned_to})


async def released(session: AsyncSession, workspace_id: Any, task: CollabTask, *, path: str,
                   info: dict[str, Any] | None = None) -> None:
    """A task reached DONE: record how, sample it, and spring a planted defect."""
    info = info or await domain(session, workspace_id, task.namespace)
    tier = info["effective_tier"]
    await session.execute(text(
        "INSERT INTO quality_releases (workspace_id, task_id, scope_key, tier, path) "
        "VALUES (:ws, :t, :k, :tier, :p)"),
        {"ws": workspace_id, "t": task.id, "k": task.namespace, "tier": tier, "p": path})
    if info.get("supervised_next"):
        await session.execute(text(
            "UPDATE quality_circuit SET supervised_next = FALSE WHERE workspace_id = :ws "
            "AND scope_key = :k"), {"ws": workspace_id, "k": task.namespace})
    if path != "HUMAN" and tier >= 1:
        rate = (settings.quality_tier2_sample_rate if tier == 2
                else settings.quality_tier1_sample_rate)
        if _sampled(task.id, rate):
            await _sample(session, workspace_id, task.id, "RANDOM", HUMAN)
        if await _artifact_bytes(session, task) > settings.quality_risk_artifact_bytes:
            await _sample(session, workspace_id, task.id, "RISK", AUDITOR_POOL)
    poison = (await session.execute(text(
        "SELECT expected_gate FROM poison_marks WHERE workspace_id = :ws AND task_id = :t"),
        {"ws": workspace_id, "t": task.id})).scalar_one_or_none()
    if poison is not None:
        await _insert_escape(
            session, workspace_id, task_id=task.id, scope_key=task.namespace,
            source="POISON_TEST", severity="S2", root_gate=poison,
            description="a planted defect passed every gate", created_by="system")
        await _count_failure(session, workspace_id, task.namespace,
                             "a planted defect passed every gate")


# -------------------------------------------------------------- escapes


async def escape_rate(session: AsyncSession, workspace_id: Any, scope_key: str,
                      days: int) -> dict[str, Any]:
    escapes = int(await session.scalar(text(
        "SELECT COUNT(*) FROM defect_escapes WHERE workspace_id = :ws AND scope_key = :k "
        "AND source <> 'POISON_TEST' AND created_at > NOW() - make_interval(days => :d)"),
        {"ws": workspace_id, "k": scope_key, "d": days}) or 0)
    if _is_repo_scope(scope_key):
        completed = await session.scalar(text(
            "SELECT COUNT(*) FROM change_claims WHERE workspace_id = :ws AND scope_key = :k "
            "AND status = 'VERIFIED' AND verified_at > NOW() - make_interval(days => :d)"),
            {"ws": workspace_id, "k": scope_key, "d": days})
    else:
        completed = await session.scalar(text(
            "SELECT COUNT(*) FROM collab_tasks WHERE workspace_id = :ws AND namespace = :k "
            "AND status = 'DONE' AND updated_at > NOW() - make_interval(days => :d)"),
            {"ws": workspace_id, "k": scope_key, "d": days})
    completed = int(completed or 0)
    rate = escapes / completed if completed else (1.0 if escapes else 0.0)
    return {"escapes": escapes, "completed": completed, "escape_rate": round(rate, 4)}


async def _insert_escape(session: AsyncSession, workspace_id: Any, *, source: str,
                         severity: str, description: str, created_by: str,
                         task_id: Any = None, claim_id: Any = None,
                         scope_key: str | None = None,
                         root_gate: str | None = None) -> str:
    escape_id = await session.scalar(text(
        "INSERT INTO defect_escapes (workspace_id, task_id, claim_id, scope_key, source, "
        "severity, description, root_gate, created_by) VALUES (:ws, :t, :c, :k, :src, :sev, "
        ":d, :g, :by) RETURNING id"),
        {"ws": workspace_id, "t": task_id, "c": str(claim_id) if claim_id else None,
         "k": scope_key, "src": source, "sev": severity, "d": description, "g": root_gate,
         "by": created_by})
    if root_gate == "AUDIT" and task_id is not None:
        await _sample(session, workspace_id, task_id, "RISK", AUDITOR_POOL)
    if scope_key is None or source == "POISON_TEST":
        return str(escape_id)
    if severity == "S1":
        await _open_circuit(session, workspace_id, scope_key, "S1 escape")
    await _maybe_demote(session, workspace_id, scope_key)
    return str(escape_id)


async def _maybe_demote(session: AsyncSession, workspace_id: Any, scope_key: str) -> None:
    """Q-7: a 30-day escape rate at or above the line lowers the tier by one."""
    tier = await configured_tier(session, workspace_id, scope_key)
    if tier == 0:
        return
    rate = await escape_rate(session, workspace_id, scope_key, 30)
    if rate["escape_rate"] < settings.quality_demotion_escape_rate:
        return
    await session.execute(text(
        "UPDATE autonomy_tiers SET tier = :t, updated_by = 'system', updated_at = NOW() "
        "WHERE workspace_id = :ws AND scope_type = :st AND scope_key = :k"),
        {"t": tier - 1, "ws": workspace_id, "k": scope_key,
         "st": "REPO_PATH" if _is_repo_scope(scope_key) else "NAMESPACE"})
    await _notify(session, workspace_id, "QUALITY_TIER_DEMOTED", "quality.tier_demoted",
                  scope_key=scope_key,
                  targets=[HUMAN, *await _recent_agents(session, workspace_id, scope_key)],
                  details={"from_tier": tier, "to_tier": tier - 1,
                           "escape_rate_30d": rate["escape_rate"]})


async def create_escape(session: AsyncSession, principal: Principal,
                        request: EscapeCreateRequest) -> dict[str, Any]:
    task_id = claim_id = scope_key = None
    if request.task_key:
        task = await _task(session, principal, request.task_key)
        task_id, scope_key = task.id, task.namespace
    elif request.claim_id:
        claim = await _claim(session, principal, request.claim_id)
        claim_id, scope_key = claim["id"], claim["scope_key"]
    escape_id = await _insert_escape(
        session, principal.workspace.id, task_id=task_id, claim_id=claim_id,
        scope_key=scope_key, source=request.source, severity=request.severity,
        description=request.description, root_gate=request.root_gate,
        created_by=principal.agent_signature)
    await session.commit()
    return await get_escape(session, principal, escape_id)


async def get_escape(session: AsyncSession, principal: Principal, escape_id: Any) -> dict:
    row = (await session.execute(text(
        "SELECT * FROM defect_escapes WHERE workspace_id = :ws AND id = :id"),
        {"ws": principal.workspace.id, "id": str(escape_id)})).mappings().one_or_none()
    if row is None:
        raise NotFound("escape")
    return _row(row)


async def resolve_escape(session: AsyncSession, principal: Principal, escape_id: uuid.UUID,
                         request: EscapeResolveRequest) -> dict[str, Any]:
    row = (await session.execute(text(
        "SELECT * FROM defect_escapes WHERE workspace_id = :ws AND id = :id FOR UPDATE"),
        {"ws": principal.workspace.id, "id": str(escape_id)})).mappings().one_or_none()
    if row is None:
        raise NotFound("escape")
    if row["status"] != "OPEN":
        raise ApiError(409, "escape_closed", "the escape is already closed",
                       details={"status": row["status"]})
    root_gate = request.root_gate or row["root_gate"]
    if request.status == "FIXED" and not (request.fix_ref and root_gate):
        raise ApiError(422, "validation_error",
                       "a fixed escape names its root gate and the fix (fix_ref)",
                       details={"fields": ["fix_ref", "root_gate"]})
    if request.status == "WAIVED" and not (request.waive_reason or "").strip():
        raise ApiError(422, "validation_error", "a waiver says why",
                       details={"field": "waive_reason"})
    await session.execute(text(
        "UPDATE defect_escapes SET status = :s, fix_ref = :f, waive_reason = :w, "
        "root_gate = :g, closed_at = NOW() WHERE id = :id"),
        {"s": request.status, "f": request.fix_ref, "w": request.waive_reason, "g": root_gate,
         "id": str(escape_id)})
    if root_gate == "AUDIT" and row["root_gate"] != "AUDIT" and row["task_id"] is not None:
        await _sample(session, principal.workspace.id, row["task_id"], "RISK", AUDITOR_POOL)
    await session.commit()
    return await get_escape(session, principal, escape_id)


def _row(row: Any) -> dict[str, Any]:
    out = {}
    for key, value in dict(row).items():
        if key in ("tenant_id", "workspace_id"):
            continue
        out[key] = (value.isoformat() if hasattr(value, "isoformat")
                    else str(value) if isinstance(value, uuid.UUID) else value)
    return out


# ---------------------------------------------------------------- samples


async def list_samples(session: AsyncSession, principal: Principal, *, assigned: str | None,
                       verdict: str | None, limit: int = 100) -> dict[str, Any]:
    params: dict[str, Any] = {"ws": principal.workspace.id, "l": max(1, min(limit, 500))}
    where = ["s.workspace_id = :ws"]
    if principal.is_human:
        if assigned:
            where.append("s.assigned_to = :a")
            params["a"] = assigned
    else:
        if principal.role is not RoleScope.AUDITOR:
            raise Forbidden("only AUDITOR tokens work the sample queue")
        where.append("s.assigned_to IN (:pool, :me)")
        params.update(pool=AUDITOR_POOL, me=principal.agent_signature)
    if verdict:
        where.append("s.verdict = :v")
        params["v"] = verdict
    rows = (await session.execute(text(
        "SELECT s.*, t.task_key, t.namespace, (s.verdict = 'PENDING' AND s.created_at < NOW() - "
        f"make_interval(days => {int(settings.quality_sample_overdue_days)})) AS overdue "
        "FROM audit_samples s JOIN collab_tasks t ON t.id = s.task_id "
        f"WHERE {' AND '.join(where)} ORDER BY s.created_at LIMIT :l"), params)).mappings().all()
    return {"items": [_row(row) for row in rows]}


async def claim_sample(session: AsyncSession, principal: Principal,
                       sample_id: uuid.UUID) -> dict[str, Any]:
    if principal.is_human or principal.role is not RoleScope.AUDITOR:
        raise Forbidden("only AUDITOR tokens claim RISK samples")
    claimed = (await session.execute(text(
        "UPDATE audit_samples SET assigned_to = :me WHERE workspace_id = :ws AND id = :id "
        "AND assigned_to = :pool AND verdict = 'PENDING' RETURNING *"),
        {"me": principal.agent_signature, "ws": principal.workspace.id, "id": str(sample_id),
         "pool": AUDITOR_POOL})).mappings().one_or_none()
    if claimed is None:
        raise ApiError(409, "sample_unavailable", "the sample is not open for claiming")
    await session.commit()
    return _row(claimed)


async def sample_verdict(session: AsyncSession, principal: Principal, sample_id: uuid.UUID,
                         request: SampleVerdictRequest) -> dict[str, Any]:
    row = (await session.execute(text(
        "SELECT s.*, t.namespace FROM audit_samples s JOIN collab_tasks t ON t.id = s.task_id "
        "WHERE s.workspace_id = :ws AND s.id = :id FOR UPDATE OF s"),
        {"ws": principal.workspace.id, "id": str(sample_id)})).mappings().one_or_none()
    if row is None:
        raise NotFound("sample")
    mine = HUMAN if principal.is_human else principal.agent_signature
    if row["assigned_to"] != mine:
        raise Forbidden("the sample is assigned to someone else")
    if row["verdict"] != "PENDING":
        raise ApiError(409, "sample_unavailable", "the sample already has a verdict")
    escape_id = None
    if request.verdict == "DEFECT":
        if request.root_gate is None:
            raise ApiError(422, "validation_error", "a defect names its root gate (first guess)",
                           details={"field": "root_gate"})
        escape_id = await _insert_escape(
            session, principal.workspace.id, task_id=row["task_id"], scope_key=row["namespace"],
            source="HUMAN_SAMPLE" if principal.is_human else "EXTERNAL_REPORT",
            severity=request.severity, root_gate=request.root_gate,
            description=request.description or f"found in a {row['reason']} sample",
            created_by=principal.agent_signature)
    await session.execute(text(
        "UPDATE audit_samples SET verdict = :v, escape_id = :e, completed_at = NOW() "
        "WHERE id = :id"), {"v": request.verdict, "e": escape_id, "id": str(sample_id)})
    await session.commit()
    result = dict(row)
    result.update(verdict=request.verdict, escape_id=escape_id)
    return _row(result)


# ----------------------------------------------------------------- claims


def claim_scope(external_ref: str, related_doc_key: str | None) -> str:
    parsed = urlparse(external_ref)
    parts = [part for part in parsed.path.split("/") if part]
    repo = "/".join(parts[:2]) if len(parts) >= 2 else (parsed.netloc or external_ref)[:200]
    if "/" not in repo:
        repo = f"{repo}/-"
    return f"{repo}:{related_doc_key or ''}"


async def _claim_view(session: AsyncSession, workspace_id: Any,
                      claim: dict[str, Any]) -> dict[str, Any]:
    info = await domain(session, workspace_id, claim["scope_key"])
    gates = await gate_status(session, workspace_id, claim_id=claim["id"])
    missing = missing_gates(gates, RELEASE_GATES[min(info["effective_tier"], 1)])
    view = _row(claim)
    view["release"] = {**info, "gates": {kind: entry["status"] for kind, entry in gates.items()},
                       "missing": missing,
                       "released": claim["status"] == "VERIFIED" and not missing}
    return view


async def report_claim(session: AsyncSession, principal: Principal,
                       request: ChangeClaimRequest) -> dict[str, Any]:
    if not (principal.is_human or principal.is_commander or can(principal, "task:transition")):
        require_permission(principal, "task:transition")
    scope_key = claim_scope(request.external_ref, request.related_doc_key)
    inserted = (await session.execute(text(
        "INSERT INTO change_claims (workspace_id, reporter, external_ref, commit_sha, "
        "related_doc_key, related_task_key, scope_key) VALUES (:ws, :r, :ref, :sha, :d, :t, :k) "
        "ON CONFLICT (workspace_id, external_ref, commit_sha) DO NOTHING RETURNING *"),
        {"ws": principal.workspace.id, "r": principal.agent_signature,
         "ref": request.external_ref, "sha": request.commit_sha.lower(),
         "d": request.related_doc_key, "t": request.related_task_key,
         "k": scope_key})).mappings().one_or_none()
    if inserted is None:
        existing = (await session.execute(text(
            "SELECT * FROM change_claims WHERE workspace_id = :ws AND external_ref = :ref "
            "AND commit_sha = :sha"),
            {"ws": principal.workspace.id, "ref": request.external_ref,
             "sha": request.commit_sha.lower()})).mappings().one()
        if existing["status"] == "REFUTED":
            raise ApiError(409, "claim_already_refuted", "this commit was already refuted",
                           details={"claim_id": str(existing["id"])})
        return await _claim_view(session, principal.workspace.id, dict(existing))
    await session.commit()
    return await _claim_view(session, principal.workspace.id, dict(inserted))


async def verify_claim(session: AsyncSession, principal: Principal, claim_id: uuid.UUID,
                       request: ChangeClaimVerifyRequest) -> dict[str, Any]:
    if not (principal.is_human or principal.role is RoleScope.AUDITOR):
        raise Forbidden("only an AUDITOR token or a person verifies change claims")
    claim = await _claim(session, principal, claim_id, for_update=True)
    if claim["status"] == "REFUTED":
        raise ApiError(409, "claim_already_refuted", "the claim was already refuted",
                       details={"claim_id": str(claim_id)})
    if not principal.is_human and claim["reporter"] == principal.agent_signature:
        raise Forbidden("the reporter of a change cannot verify it")
    updated = (await session.execute(text(
        "UPDATE change_claims SET status = :s, verified_by = :by, verified_at = NOW(), "
        "verify_evidence = CAST(:e AS jsonb) WHERE id = :id RETURNING *"),
        {"s": request.verdict, "by": principal.agent_signature, "e": _json(request.evidence),
         "id": str(claim_id)})).mappings().one()
    if request.verdict == "REFUTED":
        await _notify(session, principal.workspace.id, "QUALITY_CLAIM_REFUTED",
                      "quality.claim_refuted", scope_key=claim["scope_key"],
                      targets=[claim["reporter"], HUMAN],
                      details={"claim_id": str(claim_id), "external_ref": claim["external_ref"],
                               "commit_sha": claim["commit_sha"]})
    else:
        info = await domain(session, principal.workspace.id, claim["scope_key"])
        await session.execute(text(
            "INSERT INTO quality_releases (workspace_id, claim_id, scope_key, tier, path) "
            "VALUES (:ws, :c, :k, :t, :p)"),
            {"ws": principal.workspace.id, "c": str(claim_id), "k": claim["scope_key"],
             "t": info["effective_tier"], "p": "HUMAN" if principal.is_human else "AUDITED"})
    await session.commit()
    return await _claim_view(session, principal.workspace.id, dict(updated))


# ------------------------------------------------------------ people's side


async def set_tier(session: AsyncSession, principal: Principal,
                   request: AutonomyTierRequest) -> dict[str, Any]:
    if request.scope_type == "NAMESPACE" and "/" in request.scope_key:
        raise ApiError(422, "validation_error", "a namespace has no '/'",
                       details={"field": "scope_key"})
    if request.scope_type == "REPO_PATH" and not (
            "/" in request.scope_key.partition(":")[0] and ":" in request.scope_key):
        raise ApiError(422, "validation_error", "REPO_PATH keys look like owner/repo:path_glob",
                       details={"field": "scope_key"})
    before = await configured_tier(session, principal.workspace.id, request.scope_key)
    check = await promotion_check(session, principal.workspace.id, request.scope_key)
    if request.tier > before and not check["eligible"]:
        add_warning("promotion_below_threshold",
                    "the domain does not meet the promotion threshold; promoted anyway",
                    **{key: value for key, value in check.items() if key != "eligible"})
    await session.execute(text(
        "INSERT INTO autonomy_tiers (workspace_id, scope_type, scope_key, tier, updated_by) "
        "VALUES (:ws, :st, :k, :t, :by) ON CONFLICT (workspace_id, scope_type, scope_key) "
        "DO UPDATE SET tier = :t, updated_by = :by, updated_at = NOW()"),
        {"ws": principal.workspace.id, "st": request.scope_type, "k": request.scope_key,
         "t": request.tier, "by": principal.agent_signature})
    await session.commit()
    return {**await domain(session, principal.workspace.id, request.scope_key),
            "scope_type": request.scope_type, "previous_tier": before, "promotion": check}


async def promotion_check(session: AsyncSession, workspace_id: Any,
                          scope_key: str) -> dict[str, Any]:
    tier = await configured_tier(session, workspace_id, scope_key)
    completed = int(await session.scalar(text(
        "SELECT COUNT(*) FROM quality_releases WHERE workspace_id = :ws AND scope_key = :k "
        "AND tier = :t"), {"ws": workspace_id, "k": scope_key, "t": tier}) or 0)
    rate = await escape_rate(session, workspace_id, scope_key, 30)
    s1 = int(await session.scalar(text(
        "SELECT COUNT(*) FROM defect_escapes WHERE workspace_id = :ws AND scope_key = :k "
        "AND severity = 'S1' AND created_at > NOW() - INTERVAL '30 days'"),
        {"ws": workspace_id, "k": scope_key}) or 0)
    eligible = (completed >= settings.quality_promotion_min_completed
                and rate["escape_rate"] < settings.quality_promotion_max_escape_rate
                and s1 == 0)
    return {"eligible": eligible, "completed_at_tier": completed,
            "escape_rate_30d": rate["escape_rate"], "s1_30d": s1}


async def close_circuit(session: AsyncSession, principal: Principal, scope_key: str,
                        note: str) -> dict[str, Any]:
    closed = await session.scalar(text(
        "UPDATE quality_circuit SET state = 'CLOSED', consecutive_failures = 0, "
        "degraded_tier = NULL, last_closed_at = NOW(), supervised_next = TRUE, "
        "opened_reason = opened_reason || ' | closed: ' || :n "
        "WHERE workspace_id = :ws AND scope_key = :k AND state = 'OPEN' RETURNING scope_key"),
        {"ws": principal.workspace.id, "k": scope_key, "n": note})
    if closed is None:
        raise ApiError(409, "circuit_not_open", "the circuit is not open",
                       details={"scope_key": scope_key})
    await session.commit()
    return await domain(session, principal.workspace.id, scope_key)


async def create_poison(session: AsyncSession, principal: Principal,
                        request: PoisonTaskRequest) -> dict[str, Any]:
    from app.services import tasks as task_service

    task = await task_service._create_task_core(
        session, principal, request.task, agent=principal.agent_signature,
        batch=request.task.session_batch_id or uuid.uuid4(), commit=False)
    await session.execute(text(
        "INSERT INTO poison_marks (task_id, workspace_id, defect_description, expected_gate, "
        "created_by) VALUES (:t, :ws, :d, :g, :by)"),
        {"t": task.id, "ws": principal.workspace.id, "d": request.defect_description,
         "g": request.expected_gate, "by": principal.agent_signature})
    await session.commit()
    return {"task": task_out(task).model_dump(mode="json"),
            "poison": {"defect_description": request.defect_description,
                       "expected_gate": request.expected_gate}}


async def list_poison(session: AsyncSession, principal: Principal) -> dict[str, Any]:
    rows = (await session.execute(text(
        "SELECT p.*, t.task_key, t.status::text AS task_status, "
        "EXISTS (SELECT 1 FROM defect_escapes e WHERE e.task_id = p.task_id "
        "AND e.source = 'POISON_TEST') AS escaped, "
        "(EXISTS (SELECT 1 FROM audits a WHERE a.task_id = p.task_id AND a.verdict = 'REJECT') "
        "OR EXISTS (SELECT 1 FROM quality_gate_runs g WHERE g.task_id = p.task_id "
        "AND g.status IN ('FAIL', 'ERROR'))) AS caught "
        "FROM poison_marks p JOIN collab_tasks t ON t.id = p.task_id "
        "WHERE p.workspace_id = :ws ORDER BY p.created_at DESC LIMIT 200"),
        {"ws": principal.workspace.id})).mappings().all()
    return {"items": [_row(row) for row in rows]}


# ------------------------------------------------------------------ report


async def report(session: AsyncSession, principal: Principal, window: str) -> dict[str, Any]:
    """§10/§11: every number computed at query time; the only source of truth."""
    if not (principal.is_human or principal.is_commander):
        raise Forbidden("the quality report is for people and the workspace key")
    days = WINDOWS.get(window)
    if days is None:
        raise ApiError(422, "validation_error", "window is 7d or 30d",
                       details={"field": "window"})
    ws = principal.workspace.id
    params = {"ws": ws, "d": days}
    scopes = [row[0] for row in await session.execute(text(
        "SELECT scope_key FROM autonomy_tiers WHERE workspace_id = :ws "
        "UNION SELECT scope_key FROM quality_circuit WHERE workspace_id = :ws "
        "UNION SELECT scope_key FROM quality_releases WHERE workspace_id = :ws "
        "AND created_at > NOW() - make_interval(days => :d) "
        "UNION SELECT scope_key FROM quality_gate_runs WHERE workspace_id = :ws "
        "AND created_at > NOW() - make_interval(days => :d) "
        "UNION SELECT namespace FROM collab_tasks WHERE workspace_id = :ws AND status = 'DONE' "
        "AND updated_at > NOW() - make_interval(days => :d) ORDER BY 1"), params)]
    items = []
    for scope_key in scopes:
        info = await domain(session, ws, scope_key)
        state = await circuit(session, ws, scope_key)
        rate = await escape_rate(session, ws, scope_key, days)
        scoped = {**params, "k": scope_key}
        gates = (await session.execute(text(
            "SELECT COUNT(*) FILTER (WHERE status = 'PASS'), COUNT(*) FROM ("
            "SELECT DISTINCT ON (COALESCE(task_id, claim_id), gate_kind) status "
            "FROM quality_gate_runs WHERE workspace_id = :ws AND scope_key = :k "
            "AND created_at > NOW() - make_interval(days => :d) "
            "ORDER BY COALESCE(task_id, claim_id), gate_kind, created_at DESC, id DESC) latest"),
            scoped)).one()
        audits = (await session.execute(text(
            "SELECT COUNT(*) FILTER (WHERE a.verdict = 'REJECT'), COUNT(*) FROM audits a "
            "JOIN collab_tasks t ON t.id = a.task_id WHERE a.workspace_id = :ws "
            "AND t.namespace = :k AND a.created_at > NOW() - make_interval(days => :d)"),
            scoped)).one()
        poison = (await session.execute(text(
            "SELECT COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM audits a WHERE a.task_id = "
            "p.task_id AND a.verdict = 'REJECT') OR EXISTS (SELECT 1 FROM quality_gate_runs g "
            "WHERE g.task_id = p.task_id AND g.status IN ('FAIL', 'ERROR'))), COUNT(*) "
            "FROM poison_marks p JOIN collab_tasks t ON t.id = p.task_id "
            "WHERE p.workspace_id = :ws AND t.namespace = :k "
            "AND p.created_at > NOW() - make_interval(days => :d)"), scoped)).one()
        items.append({
            **info, **rate,
            "gate_pass_rate": round(gates[0] / gates[1], 4) if gates[1] else None,
            "audit_reject_rate": round(audits[0] / audits[1], 4) if audits[1] else None,
            "poison_catch_rate": round(poison[0] / poison[1], 4) if poison[1] else None,
            "circuit_opened_count": state["opened_count"],
            "circuit_opened_at": state["opened_at"].isoformat() if state["opened_at"] else None,
            "promotion": await promotion_check(session, ws, scope_key),
        })
    samples = (await session.execute(text(
        "SELECT COUNT(*) FILTER (WHERE verdict <> 'PENDING'), COUNT(*), "
        "COUNT(*) FILTER (WHERE verdict = 'PENDING' AND created_at < NOW() - "
        "make_interval(days => :o)) FROM audit_samples WHERE workspace_id = :ws "
        "AND created_at > NOW() - make_interval(days => :d)"),
        {**params, "o": settings.quality_sample_overdue_days})).one()
    ping_pong = {str(row[0]): row[1] for row in await session.execute(text(
        "SELECT ping_pong_count, COUNT(*) FROM collab_tasks WHERE workspace_id = :ws "
        "AND updated_at > NOW() - make_interval(days => :d) GROUP BY 1 ORDER BY 1"), params)}
    now = await db_now(session)
    return {"window": window, "generated_at": now.isoformat(), "enforced": enforced(),
            "scopes": items,
            "samples": {"completed": samples[0], "total": samples[1], "overdue": samples[2],
                        "completion_rate": round(samples[0] / samples[1], 4)
                        if samples[1] else None},
            "ping_pong_distribution": ping_pong,
            "since": (now - timedelta(days=days)).isoformat()}
