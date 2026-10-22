"""Task collaboration (PRD R2.1): AG-CO-02 pointers, AG-EV-03 mentions,
AG-CO-04 continuation packs, AG-CO-05 escalation, AG-CO-08 task trees."""

from __future__ import annotations

import asyncio
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


def _dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(scope="module")
async def collab(api_base_url) -> AsyncIterator[httpx.AsyncClient]:
    del api_base_url
    async with run_server({"LEASE_REAPER_INTERVAL_SECONDS": "5"},
                          ".server-collab.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as http:
            yield http


async def _workspace(client) -> dict:
    created = await client.post("/v1/admin/workspaces", headers=ADMIN,
                                json={"name": f"Collab {uuid.uuid4().hex[:6]}", "tier": "pro"})
    body = created.json()
    email = f"owner-{uuid.uuid4().hex[:8]}@example.test"
    owner = await client.post(
        f"/v1/admin/workspaces/{body['workspace']['id']}/human-users", headers=ADMIN,
        json={"email": email, "display_name": "Owner", "role": "OWNER", "password": PASSWORD})
    login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    return {"id": body["workspace"]["id"], "key": _bearer(body["api_key"]),
            "owner_id": owner.json()["user"]["id"], "human": _bearer(login.json()["token"])}


async def _agent(client, ws: dict, agent_id: str, *, role: str | None = None,
                 capabilities: list[str] | None = None) -> dict:
    minted = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
        json={"agent_signature": f"{agent_id}_sig", "role_scope": "EXECUTOR"})
    assert minted.status_code in (200, 201), minted.text
    headers = _bearer(minted.json()["api_key"])
    registered = await client.post("/v1/agents/register", headers=headers, json={
        "agent_id": agent_id, "role": role, "capabilities": capabilities or []})
    assert registered.status_code == 201, registered.text
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("UPDATE agent_directory SET owner_human = $1::uuid WHERE agent_id = $2 "
                           "AND workspace_id = $3::uuid", ws["owner_id"], agent_id, ws["id"])
    finally:
        await conn.close()
    return {"headers": headers, "signature": f"{agent_id}_sig", "agent_id": agent_id}


async def _doc(client, ws: dict, key: str, content: str, version: int = 0) -> dict:
    wrote = await client.put(f"/v1/documents/{key}", headers=ws["key"],
                             json={"content": content, "expected_version": version})
    assert wrote.status_code in (200, 201), wrote.text
    return wrote.json()


async def _events(ws: dict, event_type: str) -> list[dict]:
    conn = await asyncpg.connect(_dsn())
    try:
        rows = await conn.fetch(
            "SELECT targets, refs, payload FROM workspace_events WHERE workspace_id = $1::uuid "
            "AND type = $2 ORDER BY seq", ws["id"], event_type)
    finally:
        await conn.close()
    return [dict(row) for row in rows]


def _brief(summary: str) -> dict:
    return {"summary": summary, "key_metrics": {}, "unverified_hypotheses": [],
            "risk_flag": False}


def _codes(response: httpx.Response) -> set[str]:
    return {item["code"] for item in response.json().get("warnings") or []}


async def test_pointers_pin_versions_and_check_roles(collab):
    """AG-CO-02: context pointers, pinning, role/assignee checks, output warning."""
    client = collab
    ws = await _workspace(client)
    reviewer = await _agent(client, ws, "reviewer-a", role="reviewer", capabilities=["legal"])
    writer = await _agent(client, ws, "writer-a", role="writer")
    await _doc(client, ws, "spec-a", "version one")

    missing = await client.post("/v1/tasks", headers=ws["key"], json={
        "title": "Review", "context_docs": [{"doc_key": "spec-a"}, {"doc_key": "nope"}]})
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "context_doc_not_found"
    assert missing.json()["error"]["details"]["missing"] == ["default/nope"]
    unknown = await client.post("/v1/tasks", headers=ws["key"], json={
        "title": "Review", "assignee": "agent:ghost"})
    assert unknown.json()["error"]["code"] == "unknown_principal"

    created = await client.post("/v1/tasks", headers=ws["key"], json={
        "title": "Review the spec", "context_docs": [{"doc_key": "spec-a"}],
        "done_definition": "Findings written", "required_role": "reviewer",
        "required_capabilities": ["legal"],
        "output_to": {"namespace": "default", "doc_key": "review-a"}})
    assert created.status_code == 201, created.text
    task_key = created.json()["task_key"]

    claimable = await client.get("/v1/tasks?claimable_by_me=true", headers=writer["headers"])
    assert task_key not in [item["task_key"] for item in claimable.json()["items"]]
    wrong = await client.post(f"/v1/tasks/{task_key}/claim", headers=writer["headers"], json={})
    assert wrong.status_code == 403 and wrong.json()["error"]["code"] == "role_mismatch"
    mine = await client.get("/v1/tasks?claimable_by_me=true", headers=reviewer["headers"])
    assert task_key in [item["task_key"] for item in mine.json()["items"]]

    claimed = await client.post(f"/v1/tasks/{task_key}/claim", headers=reviewer["headers"],
                                json={})
    assert claimed.status_code == 200, claimed.text
    context = claimed.json()["context"]
    assert context[0]["version"] == 1 and context[0]["content"] == "version one"
    assert claimed.json()["task"]["context_versions_pinned"] == {"default:spec-a": 1}

    await _doc(client, ws, "spec-a", "version two", version=1)
    handed = await client.post(f"/v1/tasks/{task_key}/handoff", headers=reviewer["headers"],
                               json={"next_status": "HANDOFF",
                                     "brief": _brief("half done")})
    assert handed.status_code == 200, handed.text
    assert {"output_missing", "continuation_missing"} <= _codes(handed)

    again = await client.post(f"/v1/tasks/{task_key}/claim", headers=reviewer["headers"],
                              json={})
    item = next(entry for entry in again.json()["context"] if not entry["is_continuation"])
    assert item["version"] == 1 and item["content"] == "version one"
    assert item["newer_version_available"] is True


async def test_mentions_reach_the_mentioned(collab):
    """AG-EV-03: explicit and inline mentions, unknown and offline targets."""
    client = collab
    ws = await _workspace(client)
    await _agent(client, ws, "auditor-m")
    sleeper = await _agent(client, ws, "sleeper-m")
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("UPDATE agent_directory SET status = 'offline' WHERE agent_id = $1 "
                           "AND workspace_id = $2::uuid", sleeper["agent_id"], ws["id"])
    finally:
        await conn.close()
    wrote = await client.put("/v1/documents/notes-m", headers=ws["key"], json={
        "content": "Please check, @agent:auditor-m.", "expected_version": 0,
        "mentions": ["agent:sleeper-m", "agent:nobody-m"]})
    assert wrote.status_code in (200, 201), wrote.text
    assert {"unknown_mention", "target_offline"} <= _codes(wrote)
    events = await _events(ws, "mention.created")
    assert len(events) == 1
    targets = set(json.loads(events[0]["targets"]))
    assert {"agent:auditor-m", "agent:sleeper-m", f"human:{ws['owner_id']}"} <= targets

    # The same inline mention in the next version does not notify again.
    await client.put("/v1/documents/notes-m", headers=ws["key"], json={
        "content": "Please check, @agent:auditor-m. Updated.", "expected_version": 1})
    assert len(await _events(ws, "mention.created")) == 1

    task = await client.post("/v1/tasks", headers=ws["key"], json={
        "title": "Ping", "mentions": ["agent:auditor-m"]})
    assert task.json()["mentions"] == ["agent:auditor-m"]
    assert len(await _events(ws, "mention.created")) == 2


async def test_continuation_packs_relay(collab):
    """AG-CO-04: a pack is required when asked for and comes first at the next claim."""
    client = collab
    ws = await _workspace(client)
    first = await _agent(client, ws, "first-c")
    second = await _agent(client, ws, "second-c")
    created = await client.post("/v1/tasks", headers=ws["key"], json={
        "title": "Relay", "require_continuation": True})
    task_key = created.json()["task_key"]
    await client.post(f"/v1/tasks/{task_key}/claim", headers=first["headers"], json={})
    refused = await client.post(f"/v1/tasks/{task_key}/handoff", headers=first["headers"],
                                json={"next_status": "HANDOFF", "brief": _brief("x")})
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "continuation_required"
    stranger = await client.post(f"/v1/tasks/{task_key}/continuation",
                                 headers=second["headers"], json={"done": ["nothing"]})
    assert stranger.status_code == 403

    written = await client.post(f"/v1/tasks/{task_key}/continuation", headers=first["headers"],
                                json={"done": ["Parsed the input"],
                                      "tried": [{"approach": "regex", "result": "failed",
                                                 "note": "nested quotes"}],
                                      "avoid": ["regex parsing"],
                                      "next_steps": ["use the tokenizer"],
                                      "state": {"cursor": 42},
                                      "handoff": True,
                                      "brief": _brief("parsing half done")})
    assert written.status_code == 200, written.text
    assert written.json()["task"]["status"] == "HANDOFF"
    pointer = written.json()["continuation"]
    assert pointer["namespace"] == "handoff"

    claimed = await client.post(f"/v1/tasks/{task_key}/claim", headers=second["headers"],
                                json={})
    first_item = claimed.json()["context"][0]
    assert first_item["is_continuation"] is True
    assert "regex: **failed** (nested quotes)" in first_item["content"]
    assert "## Avoid / 不要重复" in first_item["content"]
    assert '"cursor": 42' in first_item["content"]


async def test_escalation_blocks_and_resumes(collab):
    """AG-CO-05: WAITING_HUMAN with a paused lease, human-only decision, timeout default."""
    client = collab
    ws = await _workspace(client)
    worker = await _agent(client, ws, "worker-e")
    created = await client.post("/v1/tasks", headers=ws["key"], json={"title": "Decide"})
    task_key = created.json()["task_key"]
    await client.post(f"/v1/tasks/{task_key}/claim", headers=worker["headers"], json={})
    asked = await client.post(f"/v1/tasks/{task_key}/escalate", headers=worker["headers"], json={
        "reason": "need a decision", "question": "Ship with the known bug?",
        "options": [{"id": "ship", "label": "Ship"}, {"id": "wait", "label": "Wait"}],
        "recommended_option": "wait"})
    assert asked.status_code == 200, asked.text
    assert asked.json()["task"]["status"] == "WAITING_HUMAN"
    escalation_id = asked.json()["escalation"]["escalation_id"]
    assert asked.json()["escalation"]["route_to"] == f"human:{ws['owner_id']}"

    blocked = await client.post(f"/v1/tasks/{task_key}/handoff", headers=worker["headers"],
                                json={"next_status": "HANDOFF", "brief": _brief("x")})
    assert blocked.status_code == 409
    by_agent = await client.post(f"/v1/escalations/{escalation_id}/resolve",
                                 headers=worker["headers"], json={"option_id": "ship"})
    assert by_agent.status_code == 403
    bad = await client.post(f"/v1/escalations/{escalation_id}/resolve", headers=ws["human"],
                            json={"option_id": "maybe"})
    assert bad.status_code == 422
    resolved = await client.post(f"/v1/escalations/{escalation_id}/resolve", headers=ws["human"],
                                 json={"option_id": "wait", "note": "after the fix"})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["task_status"] == "IN_PROGRESS"
    task = (await client.get(f"/v1/tasks/{task_key}", headers=ws["key"])).json()
    assert task["status"] == "IN_PROGRESS"
    events = await _events(ws, "escalation.resolved")
    assert json.loads(events[0]["targets"]) == [f"agent:{worker['signature']}"]
    pack = await client.get(f"/v1/documents/{task['continuation']['doc_key']}",
                            headers=ws["human"], params={"namespace": "handoff"})
    assert "Ship with the known bug? → wait (after the fix)" in pack.json()["content"]
    handed = await client.post(f"/v1/tasks/{task_key}/handoff", headers=worker["headers"],
                               json={"next_status": "HANDOFF", "brief": _brief("waited")})
    assert handed.status_code == 200, handed.text

    timed = await client.post("/v1/escalations", headers=worker["headers"], json={
        "question": "Which colour?", "options": [{"id": "red"}, {"id": "blue"}],
        "due_at": "2020-01-01T00:00:00Z", "default_on_timeout": "blue"})
    assert timed.status_code == 201, timed.text
    for _ in range(60):
        listed = await client.get("/v1/escalations?status=resolved", headers=ws["key"])
        found = [item for item in listed.json()["items"]
                 if item["id"] == timed.json()["escalation_id"]]
        if found:
            break
        await asyncio.sleep(0.5)
    assert found and found[0]["resolved_by"] == "timeout_default"
    assert found[0]["option_id"] == "blue"


async def test_task_trees_roll_up_and_cancel(collab):
    """AG-CO-08: subtasks, rollup summaries, children_done, tree view, cascading cancel."""
    client = collab
    ws = await _workspace(client)
    lead = await _agent(client, ws, "lead-t")
    helper = await _agent(client, ws, "helper-t")
    parent = await client.post("/v1/tasks", headers=ws["key"], json={"title": "Big job"})
    parent_key = parent.json()["task_key"]
    not_holder = await client.post(f"/v1/tasks/{parent_key}/subtasks", headers=lead["headers"],
                                   json={"tasks": [{"title": "a"}]})
    assert not_holder.status_code == 403
    await client.post(f"/v1/tasks/{parent_key}/claim", headers=lead["headers"], json={})
    split = await client.post(f"/v1/tasks/{parent_key}/subtasks", headers=lead["headers"], json={
        "tasks": [{"title": "Part one", "task_key": f"P1-{uuid.uuid4().hex[:6]}"},
                  {"title": "Part two", "task_key": f"P2-{uuid.uuid4().hex[:6]}"}]})
    assert split.status_code == 201, split.text
    children = [item["task_key"] for item in split.json()["children"]]
    assert split.json()["parent"]["lead"] == f"agent:{lead['signature']}"

    for child in children:
        await client.post(f"/v1/tasks/{child}/claim", headers=helper["headers"], json={})
        await client.post(f"/v1/tasks/{child}/continuation", headers=helper["headers"],
                          json={"done": ["did it"]})
        no_summary = await client.post(f"/v1/tasks/{child}/handoff", headers=helper["headers"],
                                       json={"next_status": "REVIEWING"})
        assert no_summary.status_code == 422  # the rollup needs brief.summary
        submitted = await client.post(f"/v1/tasks/{child}/handoff", headers=helper["headers"],
                                      json={"next_status": "REVIEWING",
                                            "brief": _brief(f"{child} finished")})
        assert submitted.status_code == 200, submitted.text
        approved = await client.post(f"/v1/tasks/{child}/review", headers=ws["human"],
                                     json={"decision": "approve"})
        assert approved.status_code == 200, approved.text

    parent_now = (await client.get(f"/v1/tasks/{parent_key}", headers=ws["key"])).json()
    assert parent_now["children_done"] is True
    assert parent_now["children_summary"]["total"] == 2
    assert parent_now["children_summary"]["by_status"] == {"DONE": 2}
    assert sorted(item["summary"] for item in parent_now["rollup"]) == sorted(
        f"{child} finished" for child in children)
    done_events = await _events(ws, "task.children_done")
    assert json.loads(done_events[0]["targets"]) == [
        f"agent:{lead['signature']}"]
    tree = await client.get(f"/v1/tasks/{parent_key}/tree?include=summary,rollup",
                            headers=ws["key"])
    assert tree.status_code == 200 and tree.json()["nodes"] == 3
    listed = await client.get(f"/v1/tasks?parent={parent_key}", headers=ws["key"])
    assert sorted(item["task_key"] for item in listed.json()["items"]) == sorted(children)

    second = await client.post("/v1/tasks", headers=ws["key"], json={"title": "Doomed"})
    second_key = second.json()["task_key"]
    await client.post(f"/v1/tasks/{second_key}/claim", headers=lead["headers"], json={})
    more = await client.post(f"/v1/tasks/{second_key}/subtasks", headers=lead["headers"],
                             json={"tasks": [{"title": "x"}, {"title": "y"}]})
    doomed = [item["task_key"] for item in more.json()["children"]]
    await client.post(f"/v1/tasks/{doomed[0]}/claim", headers=helper["headers"], json={})
    cancelled = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/tasks/{second_key}/cancel", headers=ADMIN,
        json={"reason": "scope cut"})
    assert cancelled.status_code == 200, cancelled.text
    for child in doomed:
        state = (await client.get(f"/v1/tasks/{child}", headers=ws["key"])).json()
        assert state["status"] == "CANCELLED" and state["lease_active"] is False
