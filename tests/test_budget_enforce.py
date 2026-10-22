"""Cost control with enforcement on (PRD AG-ST-15 R1.1).

Runs against a second server started with BUDGET_MODE=enforce on the same
database (``enforce_client``); setup goes through the regular server.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import httpx
import pytest

from app.services import budget, indexer

pytestmark = pytest.mark.integration


def _db() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def _fetch(sql: str, *args):
    conn = await asyncpg.connect(_db())
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def _agent(client, admin_headers, workspace):
    response = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        headers=admin_headers,
        json={"agent_signature": f"enf_{uuid.uuid4().hex[:6]}", "role_scope": "EXECUTOR"},
    )
    assert response.status_code in (200, 201), response.text
    return {"Authorization": f"Bearer {response.json()['api_key']}"}


async def _owner(client, admin_headers, workspace):
    email = f"budget-{uuid.uuid4().hex[:8]}@example.test"
    await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/human-users",
        headers=admin_headers,
        json={"email": email, "display_name": "O", "role": "OWNER",
              "password": "correct-horse-battery"},
    )
    login = await client.post(
        "/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    return {"Authorization": f"Bearer {login.json()['token']}"}


async def _limits(client, admin_headers, kind, scope_id, **values):
    response = await client.put(
        f"/v1/admin/budget-limits/{kind}/{scope_id}", headers=admin_headers, json=values
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _put(http, headers, key, content, version=0):
    return await http.put(
        f"/v1/documents/{key}",
        headers=headers,
        json={"content": content, "expected_version": version},
    )


async def test_rate_limit_then_forced_backoff_then_circuit(
    client, enforce_client, admin_headers, workspace
):
    agent = await _agent(client, admin_headers, workspace)
    prefix = uuid.uuid4().hex[:6]
    successes = 0
    first_refusal = None
    for index in range(40):
        response = await _put(enforce_client, agent, f"rate-{prefix}-{index}", f"c {index}", 0)
        if response.status_code == 200:
            successes += 1
            continue
        first_refusal = response
        break
    assert successes >= 20  # burst, plus whatever refilled meanwhile
    assert first_refusal is not None and first_refusal.status_code == 429
    error = first_refusal.json()["error"]
    assert error["code"] == "rate_limited" and error["details"]["dimension"] == "writes"
    wait = int(first_refusal.headers["Retry-After"])
    assert wait >= 1

    # Retrying before Retry-After is refused and doubles the wait ...
    early = await _put(enforce_client, agent, f"rate-{prefix}-x", "x", 0)
    assert early.status_code == 429
    assert early.json()["error"]["details"]["reason"] == "ignored_backoff"
    assert int(early.headers["Retry-After"]) == wait * 2
    early = await _put(enforce_client, agent, f"rate-{prefix}-x", "x", 0)
    assert int(early.headers["Retry-After"]) == wait * 4
    # ... and the third violation in a row opens a short circuit.
    tripped = await _put(enforce_client, agent, f"rate-{prefix}-x", "x", 0)
    assert tripped.status_code == 429
    assert tripped.json()["error"]["code"] == "circuit_open"
    assert tripped.json()["error"]["details"]["reason"] == "ignored_backoff"
    rows = await _fetch(
        "SELECT count(*) AS n FROM usage_ledger WHERE workspace_id = $1::uuid "
        "AND category = 'ignored_backoff'",
        workspace["workspace"]["id"],
    )
    assert rows[0]["n"] == 3
    # Refused retries took no bucket tokens.
    stored = await _fetch(
        "SELECT count(*) AS n FROM shared_documents WHERE workspace_id = $1::uuid "
        "AND doc_key = $2",
        workspace["workspace"]["id"], f"rate-{prefix}-x",
    )
    assert stored[0]["n"] == 0


async def test_hot_key_loop_trips_circuit_until_a_human_clears_it(
    client, enforce_client, admin_headers, workspace
):
    workspace_id = workspace["workspace"]["id"]
    # Raise the rate limit so only the loop detector can trip.
    await _limits(client, admin_headers, "workspace", workspace_id,
                  write_rate_per_minute=1000, write_burst=1000)
    agent = await _agent(client, admin_headers, workspace)
    key = f"loop-{uuid.uuid4().hex[:6]}"
    statuses = []
    for version in range(21):
        response = await _put(enforce_client, agent, key, f"loop {version}", version)
        statuses.append(response.status_code)
    assert statuses[:20] == [200] * 20
    last = response.json()["error"]
    assert statuses[20] == 429 and last["code"] == "circuit_open"
    assert last["details"]["reason"] == "hot_key_loop"
    assert last["details"]["doc_key"] == key
    assert int(response.headers["Retry-After"]) > 500

    other = await _put(enforce_client, agent, f"other-{uuid.uuid4().hex[:6]}", "other", 0)
    assert other.status_code == 429 and other.json()["error"]["code"] == "circuit_open"
    events = await _fetch(
        "SELECT payload FROM timeline_streams WHERE workspace_id = $1::uuid "
        "AND event_type = 'BUDGET_CIRCUIT_OPEN'",
        workspace_id,
    )
    assert len(events) == 1

    usage = (await enforce_client.get("/v1/usage/embedding", headers=agent)).json()
    assert usage["limits"]["write_burst"] == 1000
    principal_key = usage["circuit_open"][0]["principal_key"]
    owner = await _owner(client, admin_headers, workspace)
    cleared = await client.post(
        f"/v1/human/budget/circuits/{principal_key}/clear", headers=owner
    )
    assert cleared.status_code == 200 and cleared.json()["cleared"] == 1
    again = await _put(enforce_client, agent, f"after-{uuid.uuid4().hex[:6]}", "ok", 0)
    assert again.status_code == 200, again.text


async def test_spent_quota_defers_indexing_until_reset(
    client, enforce_client, admin_headers, workspace, monkeypatch
):
    workspace_id = workspace["workspace"]["id"]
    headers = workspace["headers"]
    await _limits(client, admin_headers, "workspace", workspace_id, embedding_tokens_per_day=1)
    first = await _put(enforce_client, headers, f"q-{uuid.uuid4().hex[:6]}", "spends the quota")
    assert first.status_code == 200, first.text
    key = f"q-{uuid.uuid4().hex[:6]}"
    deferred = await _put(enforce_client, headers, key, "written after the quota ran out")
    assert deferred.status_code == 200, deferred.text
    body = deferred.json()
    assert body["document"]["index_state"] == "deferred"
    assert "embedding_deferred_quota" in {w["code"] for w in body.get("warnings", [])}
    read = await enforce_client.get(f"/v1/documents/{key}", headers=headers)
    assert read.status_code == 200
    assert read.json()["content"] == "written after the quota ran out"
    usage = (await enforce_client.get("/v1/usage/embedding", headers=headers)).json()
    assert usage["embedding_tokens_quota"] == 1

    # Next day: the quota resets and the worker indexes the deferred document.
    await _limits(client, admin_headers, "workspace", workspace_id,
                  embedding_tokens_per_day=10_000_000)
    conn = await asyncpg.connect(_db())
    try:
        await conn.execute(
            "UPDATE document_index SET next_attempt_at = NOW() "
            "WHERE document_id = (SELECT id FROM shared_documents WHERE doc_key = $1 "
            "AND workspace_id = $2::uuid)",
            key, workspace_id,
        )
    finally:
        await conn.close()
    monkeypatch.setattr(budget.settings, "budget_mode", "enforce")
    await indexer.process_jobs(limit=500)
    read = await enforce_client.get(f"/v1/documents/{key}", headers=headers)
    assert read.json()["index_state"] == "indexed"


async def test_tenant_cap_covers_its_workspaces(client, enforce_client, admin_headers, workspace):
    tenant_id = (
        await _fetch("SELECT tenant_id FROM workspaces WHERE id = $1::uuid",
                     workspace["workspace"]["id"])
    )[0]["tenant_id"]
    await _limits(client, admin_headers, "tenant", tenant_id, embedding_tokens_per_day=1)
    refused = await client.put(
        f"/v1/admin/budget-limits/tenant/{tenant_id}",
        headers=admin_headers,
        json={"write_burst": 5},
    )
    assert refused.status_code == 422
    headers = workspace["headers"]
    await _put(enforce_client, headers, f"t-{uuid.uuid4().hex[:6]}", "spends the tenant cap")
    second = await _put(enforce_client, headers, f"t-{uuid.uuid4().hex[:6]}", "deferred")
    assert second.json()["document"]["index_state"] == "deferred"


async def test_one_stream_per_token(client, enforce_client, admin_headers, workspace):
    agent = await _agent(client, admin_headers, workspace)
    async with enforce_client.stream("GET", "/v1/stream", headers=agent) as first:
        assert first.status_code == 200
        second = await enforce_client.get("/v1/stream", headers=agent,
                                          timeout=httpx.Timeout(10))
        assert second.status_code == 429
        error = second.json()["error"]
        assert error["code"] == "rate_limited"
        assert error["details"]["dimension"] == "concurrency"
        assert second.headers["Retry-After"] == "1"
