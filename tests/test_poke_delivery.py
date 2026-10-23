"""Poke delivery (2026-10-23 spec "戳一戳", acceptance T-01 to T-05): addressed
events reach their targets, mentions are never dropped silently, subtask
outcomes reach the parent's lead, and failing webhooks retry then go DEAD."""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import asyncpg
import pytest

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}
PASSWORD = "correct-horse-battery"


def _dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
BRIEF = {"summary": "ready", "key_metrics": {}, "unverified_hypotheses": [], "risk_flag": False}


async def _workspace(client) -> dict:
    created = (await client.post("/v1/admin/workspaces", headers=ADMIN, json={
        "name": f"poke-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
    wid = created["workspace"]["id"]
    email = f"owner-{uuid.uuid4().hex[:8]}@example.test"
    await client.post(f"/v1/admin/workspaces/{wid}/human-users", headers=ADMIN, json={
        "email": email, "display_name": "Owner", "role": "OWNER", "password": PASSWORD})
    login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    return {"id": wid, "key": {"Authorization": f"Bearer {created['api_key']}"},
            "human": {"Authorization": f"Bearer {login.json()['token']}"}}


async def _agent(client, ws: dict, role: str = "EXECUTOR", label: str | None = None) -> dict:
    signature = label or f"{role.lower()}-{uuid.uuid4().hex[:6]}"
    minted = await client.post(f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
                               json={"agent_signature": signature, "role_scope": role,
                                     "allowed_namespaces": ["*"]})
    assert minted.status_code in (200, 201), minted.text
    return {"headers": {"Authorization": f"Bearer {minted.json()['api_key']}"},
            "signature": signature}


async def _mine(client, headers, cursor: str) -> list[dict]:
    polled = await client.get("/v1/events", headers=headers,
                              params={"cursor": cursor, "for_me": "true"})
    assert polled.status_code == 200, polled.text
    return polled.json()["events"]


async def _cursor(client, headers) -> str:
    oriented = await client.get("/v1/orientation", headers=headers)
    return oriented.json()["inbox"]["event_cursor"]


async def test_mentions_and_assignees_are_checked(client):
    """T-01, P-02, R-05."""
    ws = await _workspace(client)
    label = f"Zcode{uuid.uuid4().hex[:4]}"
    b = await _agent(client, ws, label=label)
    cursor = await _cursor(client, b["headers"])  # T-05: the orientation cursor polls
    bare = await client.post("/v1/tasks", headers=ws["key"], json={
        "title": "x", "mentions": [label]})
    assert bare.status_code == 422
    assert label in bare.text

    created = await client.post("/v1/tasks", headers=ws["key"], json={
        "title": "please look", "mentions": [f"agent:{label}"]})
    assert created.status_code == 201, created.text
    assert created.json()["mentions"] == [f"agent:{label}"]
    types = [event["type"] for event in await _mine(client, b["headers"], cursor)]
    assert "mention.created" in types

    wrong_case = await client.post("/v1/tasks", headers=ws["key"], json={
        "title": "x", "assignee": f"agent:{label.lower()}"})
    assert wrong_case.status_code == 422
    error = wrong_case.json()["error"]
    assert error["code"] == "unknown_principal"
    assert error["details"]["did_you_mean"] == [f"agent:{label}"]


async def test_verdicts_and_subtasks_poke_the_right_people(client):
    """T-02 and T-04 (P-05)."""
    ws = await _workspace(client)
    author = await _agent(client, ws)
    cursor = await _cursor(client, author["headers"])
    await client.put("/v1/documents/poke/plan", headers=ws["key"],
                     json={"content": "base", "expected_version": 0})
    proposal = await client.post("/v1/documents/poke/plan/proposals", headers=author["headers"],
                                 json={"content": "better", "base_version": 1})
    assert proposal.status_code == 201, proposal.text
    applied = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/proposals/{proposal.json()['id']}/apply",
        headers=ADMIN, json={"rebase": True, "note": "thanks"})
    assert applied.status_code == 200, applied.text
    decided = next(event for event in await _mine(client, author["headers"], cursor)
                   if event["type"] == "proposal.resolved")
    assert decided["refs"]["proposal_id"] == proposal.json()["id"]
    assert decided["details"]["to_status"] == "APPLIED"

    lead, worker = await _agent(client, ws), await _agent(client, ws)
    parent_key = f"PK-{uuid.uuid4().hex[:6].upper()}"
    await client.post("/v1/tasks", headers=ws["key"], json={
        "task_key": parent_key, "title": "the whole", "assigned_role": "EXECUTOR"})
    assert (await client.post(f"/v1/tasks/{parent_key}/claim", headers=lead["headers"],
                              json={})).status_code == 200
    split = await client.post(f"/v1/tasks/{parent_key}/subtasks", headers=ws["key"],
                              json={"tasks": [{"title": "part one",
                                               "assigned_role": "EXECUTOR"}],
                                    "lead": f"agent:{lead['signature']}"})
    assert split.status_code in (200, 201), split.text
    child_key = split.json()["children"][0]["task_key"]
    lead_cursor = await _cursor(client, lead["headers"])
    await client.post(f"/v1/tasks/{child_key}/claim", headers=worker["headers"], json={})
    handed = await client.post(f"/v1/tasks/{child_key}/handoff", headers=worker["headers"],
                               json={"next_status": "REVIEWING", "brief": BRIEF})
    assert handed.status_code == 200, handed.text
    approved = await client.post(f"/v1/tasks/{child_key}/review", headers=ws["key"], json={
        "decision": "approve", "expected_version": handed.json()["task"]["version"]})
    assert approved.status_code == 200, approved.text
    events = await _mine(client, lead["headers"], lead_cursor)
    assert any(event["refs"].get("alias") == child_key
               and (event.get("details") or {}).get("to_status") == "DONE"
               for event in events), events


class _Failing(BaseHTTPRequestHandler):
    hits = 0

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        _Failing.hits += 1
        self.rfile.read(int(self.headers.get("content-length", "0")))
        self.send_response(500)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return


@pytest.fixture
def failing_sink() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Failing)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/hook"
    finally:
        server.shutdown()
        server.server_close()


async def test_failing_webhooks_retry_three_times_then_die(client, failing_sink):
    """T-03 (D-1): three retries, then DEAD and a webhook.dead for people. The
    retry delays (30 s, 120 s, 600 s) are brought forward in the database."""
    ws = await _workspace(client)
    rule = await client.post(f"/v1/admin/workspaces/{ws['id']}/webhooks", headers=ADMIN,
                             json={"name": "b", "url": failing_sink,
                                   "event_types": ["DOC_CREATED"]})
    assert rule.status_code == 201, rule.text
    await client.put(f"/v1/documents/poke/d-{uuid.uuid4().hex[:6]}", headers=ws["key"],
                     json={"content": "x", "expected_version": 0})
    delivery = None
    for _ in range(60):
        listed = await client.get(
            f"/v1/admin/workspaces/{ws['id']}/webhooks/{rule.json()['id']}/deliveries",
            headers=ADMIN)
        items = listed.json()["items"]
        delivery = items[0] if items else None
        if delivery and delivery["status"] == "DEAD":
            break
        conn = await asyncpg.connect(_dsn())
        try:
            await conn.execute("UPDATE webhook_deliveries SET next_attempt_at = NOW() "
                               "WHERE rule_id = $1::uuid AND status = 'PENDING'",
                               rule.json()["id"])
        finally:
            await conn.close()
        await asyncio.sleep(1)
    assert delivery is not None and delivery["status"] == "DEAD", delivery
    assert delivery["attempts"] == 4
    inbox = await client.get("/v1/human/inbox", headers=ws["human"])
    assert "webhook.dead" in [item["kind"] for item in inbox.json()["items"]]
