"""Every change gets a response: the inbox (2026-10-22 delivery protocol 0.2,
acceptance T-01 to T-09)."""

from __future__ import annotations

import os
import uuid

import asyncpg
import httpx
import pytest

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}
PASSWORD = "correct-horse-battery"
BRIEF = {"summary": "ready", "key_metrics": {}, "unverified_hypotheses": [], "risk_flag": False}
ENVELOPE = {"event_seq", "event_id", "kind", "type", "priority", "source_table", "task_key",
            "doc_key", "from_status", "to_status", "actor", "note", "version",
            "applied_version", "created_at"}


def _dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def _workspace(client: httpx.AsyncClient) -> dict:
    created = (await client.post("/v1/admin/workspaces", headers=ADMIN, json={
        "name": f"inbox-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
    wid = created["workspace"]["id"]
    email = f"owner-{uuid.uuid4().hex[:8]}@example.test"
    await client.post(f"/v1/admin/workspaces/{wid}/human-users", headers=ADMIN, json={
        "email": email, "display_name": "Owner", "role": "OWNER", "password": PASSWORD})
    login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    return {"id": wid, "key": {"Authorization": f"Bearer {created['api_key']}"},
            "human": {"Authorization": f"Bearer {login.json()['token']}"}}


async def _agent(client: httpx.AsyncClient, ws: dict, role: str = "EXECUTOR") -> dict:
    signature = f"{role.lower()}-{uuid.uuid4().hex[:6]}"
    minted = await client.post(f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
                               json={"agent_signature": signature, "role_scope": role})
    assert minted.status_code in (200, 201), minted.text
    return {"headers": {"Authorization": f"Bearer {minted.json()['api_key']}"},
            "signature": signature}


async def _inbox(client, headers, **params) -> dict:
    response = await client.get("/v1/inbox", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _kinds(inbox: dict) -> list[str]:
    return [item["kind"] for item in inbox["items"]]


async def test_task_lifecycle_reaches_creator_and_executor(client):
    """T-01, T-02, T-06, T-08: claimed, review requested, done; ack only forward."""
    ws = await _workspace(client)
    a = await _agent(client, ws)
    b = await _agent(client, ws)
    key = f"IB-{uuid.uuid4().hex[:6].upper()}"
    created = await client.post("/v1/tasks", headers=a["headers"], json={
        "task_key": key, "title": "deliver", "assigned_role": "EXECUTOR"})
    assert created.status_code == 201, created.text
    claimed = await client.post(f"/v1/tasks/{key}/claim", headers=b["headers"],
                                json={"lease_seconds": 600})
    assert claimed.status_code == 200, claimed.text

    a_inbox = await _inbox(client, a["headers"])
    assert _kinds(a_inbox) == ["task.claimed"] and a_inbox["unacked_count"] == 1
    assert "task.claimed" not in _kinds(await _inbox(client, b["headers"]))  # the actor
    item = a_inbox["items"][0]
    assert set(item) == ENVELOPE and item["actor"] == b["signature"]

    handed = await client.post(f"/v1/tasks/{key}/handoff", headers=b["headers"], json={
        "next_status": "REVIEWING", "brief": BRIEF, "artifact_text": "the result"})
    assert handed.status_code == 200, handed.text
    assert "task.review_requested" in _kinds(await _inbox(client, a["headers"]))
    people = await client.get("/v1/human/inbox", headers=ws["human"])
    assert people.status_code == 200, people.text
    assert "task.review_requested" in _kinds(people.json())

    approved = await client.post(f"/v1/tasks/{key}/review", headers=ws["key"], json={
        "decision": "approve", "expected_version": handed.json()["task"]["version"]})
    assert approved.status_code == 200, approved.text
    a_inbox = await _inbox(client, a["headers"])
    b_inbox = await _inbox(client, b["headers"])
    assert "task.done" in _kinds(a_inbox) and "task.done" in _kinds(b_inbox)
    done = next(item for item in b_inbox["items"] if item["kind"] == "task.done")
    assert done["priority"] == 0 and done["note"] is None
    assert b_inbox["items"][0]["priority"] == 0  # terminal items come first
    assert "content" not in str(done) and "the result" not in str(b_inbox)

    seqs = sorted(item["event_seq"] for item in a_inbox["items"])
    assert len(seqs) == 3
    acked = await client.post("/v1/inbox/ack", headers=a["headers"],
                              json={"up_to_event_seq": seqs[1]})
    assert (acked.json()["acked_event_seq"], acked.json()["unacked_count"]) == (seqs[1], 1)
    back = await client.post("/v1/inbox/ack", headers=a["headers"], json={"up_to_event_seq": 1})
    assert back.status_code == 200 and back.json()["acked_event_seq"] == seqs[1]
    oriented = await client.get("/v1/orientation", headers=a["headers"])
    assert oriented.json()["inbox"]["unacked_count"] == 1
    assert oriented.json()["inbox"]["unacked_terminal_count"] == 1


async def test_verdicts_need_reasons(client):
    """T-04, T-05: rejections without a reason are refused and change nothing."""
    ws = await _workspace(client)
    a = await _agent(client, ws)
    key = f"IB-{uuid.uuid4().hex[:6].upper()}"
    await client.post("/v1/tasks", headers=ws["key"], json={
        "task_key": key, "title": "x", "assigned_role": "EXECUTOR"})
    await client.post(f"/v1/tasks/{key}/claim", headers=a["headers"], json={})
    handed = await client.post(f"/v1/tasks/{key}/handoff", headers=a["headers"], json={
        "next_status": "REVIEWING", "brief": BRIEF})
    bare = await client.post(f"/v1/tasks/{key}/review", headers=ws["key"], json={
        "decision": "reject", "expected_version": handed.json()["task"]["version"]})
    assert bare.status_code == 422
    assert bare.json()["error"]["code"] == "review_feedback_required"
    task = await client.get(f"/v1/tasks/{key}", headers=ws["key"])
    assert task.json()["status"] == "REVIEWING"

    rejected = await client.post(f"/v1/tasks/{key}/review", headers=ws["key"], json={
        "decision": "reject", "feedback": "cite the source",
        "expected_version": handed.json()["task"]["version"]})
    assert rejected.status_code == 200, rejected.text
    item = next(item for item in (await _inbox(client, a["headers"]))["items"]
                if item["kind"] == "task.review_rejected")
    assert item["note"] == "cite the source" and item["priority"] == 0

    await client.put("/v1/documents/ib/doc", headers=ws["key"],
                     json={"content": "base", "expected_version": 0})
    proposal = await client.post("/v1/documents/ib/doc/proposals", headers=a["headers"],
                                 json={"content": "change", "base_version": 1})
    assert proposal.status_code == 201, proposal.text
    pid = proposal.json()["id"]
    no_note = await client.post(f"/v1/admin/workspaces/{ws['id']}/proposals/{pid}/reject",
                                headers=ADMIN, json={})
    assert no_note.status_code == 422
    assert no_note.json()["error"]["code"] == "review_note_required"
    kinds = _kinds(await _inbox(client, a["headers"]))
    assert "proposal.rejected" not in kinds


async def test_proposal_outcomes_reach_their_authors(client):
    """T-03: applied (with version and note) and superseded."""
    ws = await _workspace(client)
    a = await _agent(client, ws)
    c = await _agent(client, ws)
    await client.put("/v1/documents/ib/plan", headers=ws["key"],
                     json={"content": "base", "expected_version": 0})
    first = await client.post("/v1/documents/ib/plan/proposals", headers=a["headers"],
                              json={"content": "from a", "base_version": 1})
    second = await client.post("/v1/documents/ib/plan/proposals", headers=c["headers"],
                               json={"content": "from c", "base_version": 1})
    assert first.status_code == second.status_code == 201
    people = (await client.get("/v1/human/inbox", headers=ws["human"])).json()
    assert _kinds(people).count("proposal.created") == 2
    applied = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/proposals/{first.json()['id']}/apply",
        headers=ADMIN, json={"rebase": True, "note": "good"})
    assert applied.status_code == 200, applied.text
    item = next(item for item in (await _inbox(client, a["headers"]))["items"]
                if item["kind"] == "proposal.applied")
    assert item["applied_version"] == 2 and item["note"] == "good"
    assert "proposal.superseded" in _kinds(await _inbox(client, c["headers"]))


async def test_waiting_on_people_is_surfaced_after_a_day(client):
    """T-09: computed, not stored; only past 24 hours."""
    ws = await _workspace(client)
    keys = []
    for _ in range(2):
        key = f"IB-{uuid.uuid4().hex[:6].upper()}"
        await client.post("/v1/tasks", headers=ws["key"], json={"task_key": key, "title": "w"})
        keys.append(key)
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            "UPDATE collab_tasks SET status = 'AWAITING_HUMAN', updated_at = NOW() - "
            "INTERVAL '25 hours' WHERE task_key = $1 AND workspace_id = $2::uuid",
            keys[0], ws["id"])
        await conn.execute(
            "UPDATE collab_tasks SET status = 'AWAITING_HUMAN' WHERE task_key = $1 "
            "AND workspace_id = $2::uuid", keys[1], ws["id"])
    finally:
        await conn.close()
    stale = await client.get(f"/v1/tasks/{keys[0]}", headers=ws["key"])
    fresh = await client.get(f"/v1/tasks/{keys[1]}", headers=ws["key"])
    assert stale.json()["surfaced"] is True and fresh.json()["surfaced"] is False
