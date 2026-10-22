"""Automated quality control 1.0: gates, audits, tiers, circuit, samples, escapes,
change claims and planted defects (acceptance G-01 to G-12)."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest

from tests.conftest import run_server

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}
PASSWORD = "correct-horse-battery"
BRIEF = {"summary": "ready", "key_metrics": {}, "unverified_hypotheses": [], "risk_flag": False}
ACCEPTANCE = [{"id": "A1", "text": "the report cites its sources", "kind": "MANUAL"},
              {"id": "A2", "text": "tests pass", "kind": "AUTO"}]
ALL_PASS = [{"item_id": "A1", "result": "PASS"}, {"item_id": "A2", "result": "PASS"}]


def _dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.fixture(scope="module")
async def qc(api_base_url) -> AsyncIterator[httpx.AsyncClient]:
    del api_base_url
    async with run_server({"QUALITY_CONTROL": "true"}, ".server-quality.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as http:
            yield http


async def _workspace(client) -> dict:
    created = (await client.post("/v1/admin/workspaces", headers=ADMIN, json={
        "name": f"qc-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
    wid = created["workspace"]["id"]
    email = f"owner-{uuid.uuid4().hex[:8]}@example.test"
    await client.post(f"/v1/admin/workspaces/{wid}/human-users", headers=ADMIN, json={
        "email": email, "display_name": "Owner", "role": "OWNER", "password": PASSWORD})
    login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    ws = {"id": wid, "key": {"Authorization": f"Bearer {created['api_key']}"},
          "human": {"Authorization": f"Bearer {login.json()['token']}"}}
    ws["executor"] = await _agent(client, ws, "EXECUTOR")
    ws["auditor"] = await _agent(client, ws, "AUDITOR")
    return ws


async def _agent(client, ws: dict, role: str) -> dict:
    signature = f"{role.lower()}-{uuid.uuid4().hex[:6]}"
    minted = await client.post(f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
                               json={"agent_signature": signature, "role_scope": role,
                                     "allowed_namespaces": ["*"]})
    assert minted.status_code in (200, 201), minted.text
    return {"headers": {"Authorization": f"Bearer {minted.json()['api_key']}"},
            "signature": signature}


async def _tier(client, ws: dict, scope_key: str, tier: int, scope_type: str = "NAMESPACE"):
    response = await client.put("/v1/human/quality/tiers", headers=ws["human"], json={
        "scope_type": scope_type, "scope_key": scope_key, "tier": tier})
    assert response.status_code == 200, response.text
    return response.json()


async def _task(client, ws: dict, namespace: str, *, review: bool = True,
                creator: dict | None = None) -> dict:
    key = f"QC-{uuid.uuid4().hex[:6].upper()}"
    created = await client.post("/v1/tasks", headers=creator or ws["key"], json={
        "task_key": key, "title": "write the report", "assigned_role": "EXECUTOR",
        "namespace": namespace, "acceptance": ACCEPTANCE,
        "input_context": {"secret_plan": "author notes"}})
    assert created.status_code == 201, created.text
    claimed = await client.post(f"/v1/tasks/{key}/claim", headers=ws["executor"]["headers"],
                                json={})
    assert claimed.status_code == 200, claimed.text
    if review:
        handed = await client.post(f"/v1/tasks/{key}/handoff",
                                   headers=ws["executor"]["headers"], json={
                                       "next_status": "REVIEWING", "brief": BRIEF,
                                       "artifact_text": "the result"})
        assert handed.status_code == 200, handed.text
    return {"key": key}


async def _gate(client, headers, key: str, kind: str, status: str = "PASS", **extra):
    body = {"task_key": key, "gate_kind": kind, "status": status, **extra}
    if kind == "ACCEPTANCE" and "evidence" not in body:
        body["evidence"] = {"items": ALL_PASS}
    return await client.post("/v1/quality/gate-results", headers=headers, json=body)


async def _pass_gates(client, ws: dict, key: str) -> None:
    assert (await _gate(client, ws["executor"]["headers"], key, "CI")).status_code == 201
    accepted = await _gate(client, ws["auditor"]["headers"], key, "ACCEPTANCE")
    assert accepted.status_code == 201, accepted.text


async def _approve(client, ws: dict, key: str, **extra):
    return await client.post(f"/v1/tasks/{key}/review", headers=ws["auditor"]["headers"],
                             json={"decision": "approve", "items": ALL_PASS, **extra})


async def _view(client, ws: dict, key: str) -> dict:
    view = await client.get(f"/v1/quality/tasks/{key}/audit-view",
                            headers=ws["auditor"]["headers"])
    assert view.status_code == 200, view.text
    return view.json()


async def _human_kinds(client, ws: dict) -> list[str]:
    inbox = await client.get("/v1/human/inbox", headers=ws["human"])
    assert inbox.status_code == 200, inbox.text
    return [item["kind"] for item in inbox.json()["items"]]


async def _report(client, ws: dict, scope_key: str) -> dict:
    report = await client.get("/v1/quality/report", headers=ws["key"])
    assert report.status_code == 200, report.text
    return next(item for item in report.json()["scopes"] if item["scope_key"] == scope_key)


async def test_acceptance_is_never_self_reported(qc):
    """G-01, plus the acceptance lock after the first claim."""
    ws = await _workspace(qc)
    task = await _task(qc, ws, "qc1")
    own = await _gate(qc, ws["executor"]["headers"], task["key"], "ACCEPTANCE")
    assert own.status_code == 403
    assert own.json()["error"]["code"] == "acceptance_self_report"
    other = await _gate(qc, ws["auditor"]["headers"], task["key"], "ACCEPTANCE")
    assert other.status_code == 201, other.text
    assert other.json()["effective_status"] == {"ACCEPTANCE": "PASS"}
    partial = await _gate(qc, ws["auditor"]["headers"], task["key"], "ACCEPTANCE",
                          evidence={"items": ALL_PASS[:1]})
    assert partial.json()["effective_status"] == {"ACCEPTANCE": "FAIL"}

    locked = await qc.put(f"/v1/tasks/{task['key']}/acceptance",
                          headers=ws["auditor"]["headers"], json={"acceptance": ACCEPTANCE[:1]})
    assert locked.status_code == 403
    assert locked.json()["error"]["code"] == "acceptance_locked"
    by_creator = await qc.put(f"/v1/tasks/{task['key']}/acceptance", headers=ws["key"],
                              json={"acceptance": ACCEPTANCE[:1]})
    assert by_creator.status_code == 200, by_creator.text
    assert by_creator.json()["acceptance_locked"] is True
    assert by_creator.json()["review_history"][-1]["type"] == "acceptance_change"


async def test_tier1_release_needs_gates_and_the_audit_view(qc):
    """G-02, G-03, G-04: gates first, then an audit from the audit view."""
    ws = await _workspace(qc)
    await _tier(qc, ws, "qc2", 1)
    task = await _task(qc, ws, "qc2")
    await _view(qc, ws, task["key"])
    accepted = await _gate(qc, ws["auditor"]["headers"], task["key"], "ACCEPTANCE")
    assert accepted.status_code == 201
    unmet = await _approve(qc, ws, task["key"])
    assert unmet.status_code == 409
    assert unmet.json()["error"]["code"] == "quality_gates_unmet"
    assert unmet.json()["error"]["details"]["missing"] == ["CI"]
    incomplete = await _approve(qc, ws, task["key"], items=ALL_PASS[:1])
    assert incomplete.status_code == 422
    assert incomplete.json()["error"]["code"] == "acceptance_items_incomplete"

    second = await _task(qc, ws, "qc2")
    await _pass_gates(qc, ws, second["key"])
    blind = await _approve(qc, ws, second["key"])
    assert blind.status_code == 422
    assert blind.json()["error"]["code"] == "audit_view_required"
    view = await _view(qc, ws, second["key"])
    assert "handoff_brief" not in view and "input_context" not in view
    assert "author notes" not in json.dumps(view)
    assert view["acceptance"] == ACCEPTANCE
    assert view["effective_gate_status"] == {"ACCEPTANCE": "PASS", "CI": "PASS"}
    approved = await _approve(qc, ws, second["key"])
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "DONE"

    conn = await asyncpg.connect(_dsn())
    try:
        audit = await conn.fetchrow(
            "SELECT a.verdict, a.items, a.view_used FROM audits a JOIN collab_tasks t "
            "ON t.id = a.task_id WHERE t.task_key = $1 AND t.workspace_id = $2::uuid",
            second["key"], ws["id"])
    finally:
        await conn.close()
    assert audit["verdict"] == "APPROVE" and audit["view_used"] is True
    assert json.loads(audit["items"]) == ALL_PASS


async def test_authors_do_not_audit_and_rejections_cite(qc):
    """G-05 and the cited-clauses rule."""
    ws = await _workspace(qc)
    task = await _task(qc, ws, "qc5")
    own = await qc.post(f"/v1/tasks/{task['key']}/review", headers=ws["executor"]["headers"],
                        json={"decision": "reject", "feedback": "mine", "cited_clauses": ["§1"]})
    assert own.status_code == 403
    uncited = await qc.post(f"/v1/tasks/{task['key']}/review", headers=ws["auditor"]["headers"],
                            json={"decision": "reject", "feedback": "missing sources"})
    assert uncited.status_code == 422
    assert uncited.json()["error"]["details"]["field"] == "cited_clauses"
    state = await qc.get(f"/v1/tasks/{task['key']}", headers=ws["key"])
    assert state.json()["status"] == "REVIEWING"
    rejected = await qc.post(f"/v1/tasks/{task['key']}/review",
                             headers=ws["auditor"]["headers"], json={
                                 "decision": "reject", "feedback": "missing sources",
                                 "cited_clauses": ["A1"]})
    assert rejected.status_code == 200, rejected.text


async def test_tier2_auto_release_and_the_circuit(qc):
    """G-06: tier 2 releases on runner gates; three failures open the circuit."""
    ws = await _workspace(qc)
    await _tier(qc, ws, "qc6", 2)
    task = await _task(qc, ws, "qc6", review=False)
    for kind in ("CI", "TESTS"):
        assert (await _gate(qc, ws["executor"]["headers"], task["key"], kind)).status_code == 201
    claimed = await _gate(qc, ws["auditor"]["headers"], task["key"], "ACCEPTANCE")
    assert claimed.status_code == 201
    not_runner = await qc.post(f"/v1/tasks/{task['key']}/handoff",
                               headers=ws["executor"]["headers"],
                               json={"next_status": "DONE", "brief": BRIEF})
    assert not_runner.status_code == 409
    assert not_runner.json()["error"]["details"]["missing"] == ["ACCEPTANCE:RUNNER"]
    downgraded = await _gate(qc, ws["auditor"]["headers"], task["key"], "ACCEPTANCE",
                             source="RUNNER")
    assert downgraded.json()["source"] == "AGENT_CLAIM"
    assert any(w["code"] == "gate_source_downgraded" for w in downgraded.json()["warnings"])
    runner = await _gate(qc, ws["key"], task["key"], "ACCEPTANCE", source="RUNNER")
    assert runner.json()["source"] == "RUNNER"
    done = await qc.post(f"/v1/tasks/{task['key']}/handoff", headers=ws["executor"]["headers"],
                         json={"next_status": "DONE", "brief": BRIEF})
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "DONE"

    failing = await _task(qc, ws, "qc6", review=False)
    for _ in range(3):
        await _gate(qc, ws["executor"]["headers"], failing["key"], "CI", "FAIL")
    scope = await _report(qc, ws, "qc6")
    assert (scope["circuit_state"], scope["tier"], scope["effective_tier"]) == ("OPEN", 2, 1)
    for kind in ("CI", "TESTS"):
        await _gate(qc, ws["executor"]["headers"], failing["key"], kind)
    await _gate(qc, ws["key"], failing["key"], "ACCEPTANCE", source="RUNNER")
    blocked = await qc.post(f"/v1/tasks/{failing['key']}/handoff",
                            headers=ws["executor"]["headers"],
                            json={"next_status": "DONE", "brief": BRIEF})
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "quality_circuit_open"
    assert "quality.circuit_opened" in await _human_kinds(qc, ws)

    closed = await qc.post("/v1/human/quality/circuit/qc6/close", headers=ws["human"],
                           json={"note": "fixed the flaky runner"})
    assert closed.status_code == 200, closed.text
    assert closed.json()["effective_tier"] == 0  # the next change is supervised


async def test_s1_escape_opens_the_circuit_at_once(qc):
    """G-07."""
    ws = await _workspace(qc)
    await _tier(qc, ws, "qc7", 1)
    task = await _task(qc, ws, "qc7")
    escape = await qc.post("/v1/human/quality/escapes", headers=ws["human"], json={
        "task_key": task["key"], "severity": "S1", "description": "data loss in prod"})
    assert escape.status_code == 201, escape.text
    assert (await _report(qc, ws, "qc7"))["circuit_state"] == "OPEN"
    assert "quality.circuit_opened" in await _human_kinds(qc, ws)

    open_escape = escape.json()["id"]
    no_ref = await qc.post(f"/v1/human/quality/escapes/{open_escape}/resolve",
                           headers=ws["human"], json={"status": "FIXED"})
    assert no_ref.status_code == 422
    fixed = await qc.post(f"/v1/human/quality/escapes/{open_escape}/resolve",
                          headers=ws["human"], json={
                              "status": "FIXED", "root_gate": "TESTS",
                              "fix_ref": "added a data-loss regression gate"})
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["status"] == "FIXED"


async def test_a_defective_sample_records_an_escape(qc):
    """G-08."""
    ws = await _workspace(qc)
    await _tier(qc, ws, "qc8", 1)
    task = await _task(qc, ws, "qc8")
    await _pass_gates(qc, ws, task["key"])
    await _view(qc, ws, task["key"])
    assert (await _approve(qc, ws, task["key"])).status_code == 200
    before = (await _report(qc, ws, "qc8"))["escape_rate"]
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            "INSERT INTO audit_samples (workspace_id, task_id, reason, assigned_to) "
            "SELECT workspace_id, id, 'RANDOM', 'human' FROM collab_tasks WHERE task_key = $1 "
            "AND workspace_id = $2::uuid", task["key"], ws["id"])
    finally:
        await conn.close()
    queue = await qc.get("/v1/human/quality/samples", headers=ws["human"])
    sample = next(item for item in queue.json()["items"] if item["task_key"] == task["key"])
    judged = await qc.post(f"/v1/human/quality/samples/{sample['id']}/verdict",
                           headers=ws["human"], json={"verdict": "DEFECT", "root_gate": "AUDIT",
                                                      "description": "sources were invented"})
    assert judged.status_code == 200, judged.text
    assert judged.json()["escape_id"]
    after = await _report(qc, ws, "qc8")
    assert after["escapes"] == 1 and after["escape_rate"] > before


async def test_a_planted_defect_that_passes_is_an_escape(qc):
    """G-09: and agents never see the mark."""
    ws = await _workspace(qc)
    await _tier(qc, ws, "qc9", 1)
    key = f"QC-{uuid.uuid4().hex[:6].upper()}"
    planted = await qc.post("/v1/human/quality/poison", headers=ws["human"], json={
        "task": {"task_key": key, "title": "write the report", "assigned_role": "EXECUTOR",
                 "namespace": "qc9", "acceptance": ACCEPTANCE},
        "defect_description": "PLANTED-OFF-BY-ONE", "expected_gate": "TESTS"})
    assert planted.status_code == 201, planted.text
    await qc.post(f"/v1/tasks/{key}/claim", headers=ws["executor"]["headers"], json={})
    await qc.post(f"/v1/tasks/{key}/handoff", headers=ws["executor"]["headers"],
                  json={"next_status": "REVIEWING", "brief": BRIEF, "artifact_text": "x"})
    await _pass_gates(qc, ws, key)
    views = [await _view(qc, ws, key),
             (await qc.get(f"/v1/tasks/{key}", headers=ws["auditor"]["headers"])).json(),
             (await qc.get("/v1/tasks", headers=ws["executor"]["headers"])).json(),
             (await qc.get("/v1/orientation", headers=ws["executor"]["headers"])).json()]
    for view in views:
        text = json.dumps(view).lower()
        assert "poison" not in text and "planted-off-by-one" not in text
    assert (await _approve(qc, ws, key)).status_code == 200

    conn = await asyncpg.connect(_dsn())
    try:
        escape = await conn.fetchrow(
            "SELECT source, severity, root_gate FROM defect_escapes WHERE workspace_id = $1::uuid",
            ws["id"])
        failures = await conn.fetchval(
            "SELECT consecutive_failures FROM quality_circuit WHERE workspace_id = $1::uuid "
            "AND scope_key = 'qc9'", ws["id"])
    finally:
        await conn.close()
    assert tuple(escape) == ("POISON_TEST", "S2", "TESTS") and failures == 1
    listed = await qc.get("/v1/human/quality/poison", headers=ws["human"])
    assert listed.json()["items"][0]["escaped"] is True
    assert (await _report(qc, ws, "qc9"))["escapes"] == 0  # planted defects are not escapes


async def test_change_claims(qc):
    """G-10: idempotent, refutation notifies, outside changes cap at tier 1."""
    ws = await _workspace(qc)
    await _tier(qc, ws, "acme/app:docs/*", 2, scope_type="REPO_PATH")
    body = {"external_ref": "https://github.com/acme/app/pull/7", "commit_sha": "abc1234def",
            "related_doc_key": "docs/guide.md"}
    first = await qc.post("/v1/quality/claims", headers=ws["executor"]["headers"], json=body)
    again = await qc.post("/v1/quality/claims", headers=ws["executor"]["headers"], json=body)
    assert first.status_code == again.status_code == 201
    assert first.json()["id"] == again.json()["id"]
    assert first.json()["scope_key"] == "acme/app:docs/guide.md"
    own = await qc.post(f"/v1/quality/claims/{first.json()['id']}/verify",
                        headers=ws["executor"]["headers"], json={"verdict": "VERIFIED"})
    assert own.status_code == 403
    await qc.post("/v1/quality/gate-results", headers=ws["executor"]["headers"], json={
        "claim_id": first.json()["id"], "gate_kind": "CI", "status": "PASS",
        "evidence": {"run_url": "https://github.com/acme/app/actions/runs/1"}})
    await qc.post("/v1/quality/gate-results", headers=ws["auditor"]["headers"], json={
        "claim_id": first.json()["id"], "gate_kind": "ACCEPTANCE", "status": "PASS"})
    verified = await qc.post(f"/v1/quality/claims/{first.json()['id']}/verify",
                             headers=ws["auditor"]["headers"],
                             json={"verdict": "VERIFIED", "evidence": {"pr": "open"}})
    assert verified.status_code == 200, verified.text
    release = verified.json()["release"]
    assert (release["tier"], release["effective_tier"], release["released"]) == (2, 1, True)

    bad = {**body, "commit_sha": "0000000bad"}
    claim = (await qc.post("/v1/quality/claims", headers=ws["executor"]["headers"],
                           json=bad)).json()
    refuted = await qc.post(f"/v1/quality/claims/{claim['id']}/verify",
                            headers=ws["auditor"]["headers"],
                            json={"verdict": "REFUTED", "evidence": {"commit": "missing"}})
    assert refuted.status_code == 200, refuted.text
    assert "quality.claim_refuted" in await _human_kinds(qc, ws)
    agent_inbox = await qc.get("/v1/inbox", headers=ws["executor"]["headers"])
    assert "quality.claim_refuted" in [item["kind"] for item in agent_inbox.json()["items"]]
    repeat = await qc.post("/v1/quality/claims", headers=ws["executor"]["headers"], json=bad)
    assert repeat.status_code == 409
    assert repeat.json()["error"]["code"] == "claim_already_refuted"


async def test_escapes_demote_a_domain(qc):
    """G-11: a 30-day escape rate at or above 5% lowers the tier and tells people."""
    ws = await _workspace(qc)
    await _tier(qc, ws, "qc11", 2)
    task = await _task(qc, ws, "qc11")
    escape = await qc.post("/v1/human/quality/escapes", headers=ws["human"], json={
        "task_key": task["key"], "severity": "S3", "description": "wrong total"})
    assert escape.status_code == 201, escape.text
    scope = await _report(qc, ws, "qc11")
    assert scope["tier"] == 1 and scope["escape_rate"] >= 0.05
    assert "quality.tier_demoted" in await _human_kinds(qc, ws)


async def test_a_new_domain_is_supervised(qc):
    """G-12: no tier row means tier 0; a person releases the change."""
    ws = await _workspace(qc)
    task = await _task(qc, ws, "qc12")
    await _pass_gates(qc, ws, task["key"])
    self_done = await qc.post(f"/v1/tasks/{task['key']}/handoff",
                              headers=ws["executor"]["headers"],
                              json={"next_status": "DONE", "brief": BRIEF})
    assert self_done.status_code in (403, 409)  # not claimable after the hand-off either way
    approved = await _approve(qc, ws, task["key"])
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "AWAITING_HUMAN"
    released = await qc.post(f"/v1/human/tasks/{task['key']}/review", headers=ws["human"],
                             json={"decision": "approve"})
    assert released.status_code == 200, released.text
    assert released.json()["status"] == "DONE"

    auto = await _task(qc, ws, "qc12b", review=False)
    denied = await qc.post(f"/v1/tasks/{auto['key']}/handoff", headers=ws["executor"]["headers"],
                           json={"next_status": "DONE", "brief": BRIEF})
    assert denied.status_code == 403
    assert (await _report(qc, ws, "qc12"))["tier"] == 0
