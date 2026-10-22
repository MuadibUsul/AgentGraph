"""Unified leases: heartbeat, fencing, orphans, revocation, succession (PRD AG-CO-03).

Expiry is simulated by moving ``lease_expires_at`` into the past; the test
server's reaper runs every 5 seconds.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.integration

V2 = {"AgentGraph-Version": "2026-11"}


def _brief(summary: str) -> dict:
    return {"summary": summary, "key_metrics": {}, "unverified_hypotheses": [],
            "risk_flag": False}


def _db() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def _sql(query: str, *args):
    conn = await asyncpg.connect(_db())
    try:
        return await conn.fetch(query, *args)
    finally:
        await conn.close()


async def _agent(client, admin_headers, workspace, name=None):
    signature = name or f"lease_{uuid.uuid4().hex[:6]}"
    response = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        headers=admin_headers,
        json={"agent_signature": signature, "role_scope": "EXECUTOR"},
    )
    assert response.status_code in (200, 201), response.text
    body = response.json()
    return {"Authorization": f"Bearer {body['api_key']}", **V2}, signature, body["token"]["id"]


async def _task(client, workspace, **extra):
    response = await client.post(
        "/v1/tasks",
        headers=workspace["headers"],
        json={"title": f"lease task {uuid.uuid4().hex[:6]}", **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _claim(client, headers, task, ttl=60):
    return await client.post(
        f"/v1/tasks/{task['task_key']}/claim", headers=headers, json={"lease_ttl_seconds": ttl}
    )


async def _expire(task_id: str) -> None:
    await _sql(
        "UPDATE collab_tasks SET lease_expires_at = NOW() - INTERVAL '1 second' "
        "WHERE id = $1::uuid",
        task_id,
    )


async def _wait_status(client, workspace, task, status, timeout=20.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        current = (
            await client.get(f"/v1/tasks/{task['task_key']}", headers=workspace["headers"])
        ).json()
        if current["status"] == status:
            return current
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"task stayed {current['status']}, wanted {status}")
        await asyncio.sleep(0.5)


async def _events(workspace_id: str, event_type: str, task_key: str):
    return await _sql(
        "SELECT payload FROM timeline_streams WHERE workspace_id = $1::uuid "
        "AND event_type = $2 AND payload->>'task_key' = $3",
        workspace_id, event_type, task_key,
    )


async def test_claim_returns_a_lease_with_fencing_token(client, admin_headers, workspace):
    agent, signature, _ = await _agent(client, admin_headers, workspace)
    task = await _task(client, workspace)
    claimed = await _claim(client, agent, task, ttl=90)
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["lease"]
    assert lease["lease_id"].startswith("lease_")
    assert lease["ttl_seconds"] == 90 and lease["heartbeat_interval_s"] == 30
    assert lease["fencing_token"] == claimed.json()["task"]["fencing_token"]
    assert lease["holder"] == signature and lease["state"] == "active"
    listed = (await client.get("/v1/leases", headers=agent)).json()["items"]
    assert [item["lease_id"] for item in listed] == [lease["lease_id"]]

    default = await client.post(
        f"/v1/tasks/{(await _task(client, workspace))['task_key']}/claim",
        headers=agent, json={},
    )
    assert default.json()["lease"]["ttl_seconds"] == 900  # 2026-11 default


async def test_heartbeat_extends_and_records_notes(client, admin_headers, workspace):
    agent, _, _ = await _agent(client, admin_headers, workspace)
    other, _, _ = await _agent(client, admin_headers, workspace)
    task = await _task(client, workspace)
    lease = (await _claim(client, agent, task)).json()["lease"]
    beat = await client.post(
        f"/v1/leases/{lease['lease_id']}/heartbeat",
        headers=agent,
        json={"note": "parsed the inputs", "progress": 0.4},
    )
    assert beat.status_code == 200, beat.text
    assert beat.json()["expires_at"] > lease["expires_at"]
    foreign = await client.post(
        f"/v1/leases/{lease['lease_id']}/heartbeat", headers=other, json={}
    )
    assert foreign.status_code == 403
    assert foreign.json()["error"]["code"] == "not_lease_holder"
    missing = await client.post("/v1/leases/lease_NOPE/heartbeat", headers=agent, json={})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "lease_not_found"

    # A silent interval produces exactly one heartbeat_missed event.
    await _sql(
        "UPDATE leases SET last_heartbeat_at = NOW() - INTERVAL '25 seconds' "
        "WHERE lease_id = $1",
        lease["lease_id"],
    )
    deadline = asyncio.get_running_loop().time() + 20
    while not await _events(workspace["workspace"]["id"], "TASK_HEARTBEAT_MISSED",
                            task["task_key"]):
        assert asyncio.get_running_loop().time() < deadline, "no heartbeat_missed event"
        await asyncio.sleep(0.5)
    await asyncio.sleep(6)
    missed = await _events(workspace["workspace"]["id"], "TASK_HEARTBEAT_MISSED",
                           task["task_key"])
    assert len(missed) == 1


async def test_expired_lease_orphans_task_with_continuation(client, admin_headers, workspace):
    agent, signature, _ = await _agent(client, admin_headers, workspace)
    task = await _task(client, workspace)
    claimed = await _claim(client, agent, task)
    lease_id = claimed.json()["lease"]["lease_id"]
    await client.post(f"/v1/leases/{lease_id}/heartbeat", headers=agent,
                      json={"note": "halfway through"})
    await _expire(task["id"])
    current = await _wait_status(client, workspace, task, "READY")
    assert current["orphaned_from"] == signature and current["orphan_count"] == 1
    events = await _events(workspace["workspace"]["id"], "LEASE_EXPIRED", task["task_key"])
    assert len(events) == 1
    # The successor is an ordinary agent, not the workspace key.
    successor, _, _ = await _agent(client, admin_headers, workspace)
    continuation = await client.get(
        f"/v1/documents/handoff/{task['canonical_id'].lower()}", headers=successor
    )
    assert continuation.status_code == 200, continuation.text
    assert continuation.json()["frontmatter"]["auto_generated"] is True
    assert "halfway through" in continuation.json()["content"]
    lease = (await _sql("SELECT state, end_reason FROM leases WHERE lease_id = $1", lease_id))[0]
    assert lease["state"] == "expired" and lease["end_reason"] == "timeout"

    # The former holder's late write is refused with lease_lost.
    late = await client.post(
        f"/v1/tasks/{task['task_key']}/handoff",
        headers=agent,
        json={"next_status": "DONE", "brief": _brief("late result")},
    )
    assert late.status_code == 409, late.text
    assert late.json()["error"]["code"] == "lease_lost"
    assert late.json()["error"]["details"]["task_status"] == "READY"


async def test_stale_fencing_token_is_refused(client, admin_headers, workspace):
    first, _, _ = await _agent(client, admin_headers, workspace)
    second, second_name, _ = await _agent(client, admin_headers, workspace)
    task = await _task(client, workspace)
    old = (await _claim(client, first, task)).json()["lease"]["fencing_token"]
    await _expire(task["id"])
    await _wait_status(client, workspace, task, "READY")
    new = (await _claim(client, second, task)).json()["lease"]["fencing_token"]
    assert new > old
    stale = await client.post(
        f"/v1/tasks/{task['task_key']}/handoff",
        headers=first,
        json={"next_status": "DONE", "brief": _brief("stale"), "fencing_token": old},
    )
    assert stale.status_code == 409
    error = stale.json()["error"]
    assert error["code"] == "stale_fencing_token"
    assert error["details"] == {"presented": old, "current": new,
                                "task_key": task["task_key"]}
    unchanged = (
        await client.get(f"/v1/tasks/{task['task_key']}", headers=workspace["headers"])
    ).json()
    assert unchanged["status"] == "IN_PROGRESS" and unchanged["claimed_by"] == second_name


async def test_repeated_orphaning_escalates(client, admin_headers, workspace):
    agent, _, _ = await _agent(client, admin_headers, workspace)
    task = await _task(client, workspace)
    for round_number in range(3):
        claimed = await _claim(client, agent, task)
        assert claimed.status_code == 200, claimed.text
        await _expire(task["id"])
        expected = "ESCALATED_TO_HUMAN" if round_number == 2 else "READY"
        await _wait_status(client, workspace, task, expected)
    escalations = await _events(workspace["workspace"]["id"], "TASK_ESCALATED", task["task_key"])
    assert [row["payload"] for row in escalations][0].find("repeated_orphan") >= 0


async def test_revoking_a_token_releases_its_leases(client, admin_headers, workspace):
    agent, _, token_id = await _agent(client, admin_headers, workspace)
    tasks = [await _task(client, workspace) for _ in range(2)]
    for task in tasks:
        assert (await _claim(client, agent, task)).status_code == 200
    revoked = await client.patch(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens/{token_id}",
        headers=admin_headers,
        json={"is_active": False},
    )
    assert revoked.status_code == 200, revoked.text
    for task in tasks:
        current = (
            await client.get(f"/v1/tasks/{task['task_key']}", headers=workspace["headers"])
        ).json()
        assert current["status"] == "READY"
    states = await _sql(
        "SELECT state, end_reason FROM leases WHERE holder_token_id = $1::uuid", token_id
    )
    assert {(row["state"], row["end_reason"]) for row in states} == {
        ("revoked", "holder_revoked")
    }


async def test_assignee_only_and_transfer(client, admin_headers, workspace):
    first, first_name, _ = await _agent(client, admin_headers, workspace)
    other, other_name, _ = await _agent(client, admin_headers, workspace)
    assigned = [
        await _task(client, workspace, assignee=f"agent:{first_name}") for _ in range(4)
    ]
    refused = await _claim(client, other, assigned[0])
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "not_assignee"
    assert (await _claim(client, first, assigned[0])).status_code == 200
    for index in range(2):
        response = await client.put(
            f"/v1/documents/maintained-{uuid.uuid4().hex[:6]}-{index}",
            headers=workspace["headers"],
            json={"content": "maintained", "expected_version": 0,
                  "frontmatter": {"maintainer": f"agent:{first_name}"}},
        )
        assert response.status_code == 200, response.text

    email = f"lease-{uuid.uuid4().hex[:8]}@example.test"
    await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/human-users",
        headers=admin_headers,
        json={"email": email, "display_name": "O", "role": "OWNER",
              "password": "correct-horse-battery"},
    )
    login = await client.post(
        "/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    owner = {"Authorization": f"Bearer {login.json()['token']}"}
    audits_before = (await _sql(
        "SELECT count(*) AS n FROM state_audit_diffs WHERE workspace_id = $1::uuid "
        "AND target_table = 'responsibilities'", workspace["workspace"]["id"],
    ))[0]["n"]
    moved = await client.post(
        "/v1/human/transfer-responsibilities",
        headers=owner,
        json={"from": f"agent:{first_name}", "to": f"agent:{other_name}"},
    )
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert sorted(body["assigned_tasks"]) == sorted(task["task_key"] for task in assigned)
    assert len(body["maintained_docs"]) == 2
    assert body["leases_revoked"] == [assigned[0]["task_key"]]
    for task in assigned:
        current = (
            await client.get(f"/v1/tasks/{task['task_key']}", headers=workspace["headers"])
        ).json()
        assert current["assignee"] == f"agent:{other_name}"
    maintainers = await _sql(
        "SELECT frontmatter->>'maintainer' AS m FROM shared_documents "
        "WHERE workspace_id = $1::uuid AND doc_key = ANY($2::text[])",
        workspace["workspace"]["id"], body["maintained_docs"],
    )
    assert {row["m"] for row in maintainers} == {f"agent:{other_name}"}
    audits_after = (await _sql(
        "SELECT count(*) AS n FROM state_audit_diffs WHERE workspace_id = $1::uuid "
        "AND target_table = 'responsibilities'", workspace["workspace"]["id"],
    ))[0]["n"]
    assert audits_after == audits_before + 1
    agent_refused = await client.post(
        "/v1/human/transfer-responsibilities",
        headers=first,
        json={"from": f"agent:{first_name}", "to": f"agent:{other_name}"},
    )
    assert agent_refused.status_code == 403


async def test_concurrent_claims_have_exactly_one_winner(client, admin_headers, workspace):
    agents = [await _agent(client, admin_headers, workspace) for _ in range(30)]
    task = await _task(client, workspace)
    responses = await asyncio.gather(*(_claim(client, headers, task) for headers, _, _ in agents))
    codes = [response.status_code for response in responses]
    assert codes.count(200) == 1, codes
    assert all(code == 409 for code in codes if code != 200), codes
    active = await _sql(
        "SELECT count(*) AS n FROM leases WHERE resource_id = $1::uuid AND state = 'active'",
        task["id"],
    )
    assert active[0]["n"] == 1
