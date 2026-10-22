"""Cost control in shadow mode (PRD AG-ST-15 acceptance 5 and 6)."""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.integration


def _db() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def _agent(client, admin_headers, workspace):
    response = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        headers=admin_headers,
        json={"agent_signature": f"budget_{uuid.uuid4().hex[:6]}", "role_scope": "EXECUTOR"},
    )
    return {"Authorization": f"Bearer {response.json()['api_key']}"}, response.json()["token"]


async def _ledger(workspace_id: str, category: str) -> int:
    conn = await asyncpg.connect(_db())
    try:
        return int(
            await conn.fetchval(
                "SELECT COALESCE(SUM(amount), 0) FROM usage_ledger "
                "WHERE workspace_id = $1::uuid AND category = $2",
                workspace_id,
                category,
            )
        )
    finally:
        await conn.close()


async def test_shadow_mode_records_but_never_blocks_bursts(client, admin_headers, workspace):
    agent, _ = await _agent(client, admin_headers, workspace)
    responses = await asyncio.gather(
        *(
            client.put(
                f"/v1/documents/burst/{uuid.uuid4().hex[:6]}-{index}",
                headers=agent,
                json={"content": f"burst {index}", "expected_version": 0},
            )
            for index in range(30)
        )
    )
    assert [response.status_code for response in responses] == [200] * 30
    would = await _ledger(workspace["workspace"]["id"], "would_rate_limit")
    assert 8 <= would <= 10
    usage = responses[0].json()["usage"]
    assert usage["embedding_tokens"] > 0 and usage["quota_remaining"] > 0


async def test_hot_key_loop_is_recorded_in_shadow_mode(client, admin_headers, workspace):
    agent, _ = await _agent(client, admin_headers, workspace)
    key = f"loop/{uuid.uuid4().hex[:6]}"
    statuses = []
    for version in range(22):
        response = await client.put(
            f"/v1/documents/{key}",
            headers=agent,
            json={"content": f"loop {version}", "expected_version": version},
        )
        statuses.append(response.status_code)
    assert statuses == [200] * 22
    assert await _ledger(workspace["workspace"]["id"], "would_circuit_open") >= 1


async def test_get_usage_matches_the_ledger(client, admin_headers, workspace):
    agent, token = await _agent(client, admin_headers, workspace)
    await client.put(
        f"/v1/documents/usage/{uuid.uuid4().hex[:6]}",
        headers=agent,
        json={"content": "metered words " * 50, "expected_version": 0},
    )
    await client.post("/v1/query", headers=agent, json={"query": f"q-{uuid.uuid4().hex}"})
    usage = await client.get("/v1/usage/embedding?window=day", headers=agent)
    assert usage.status_code == 200, usage.text
    body = usage.json()
    assert body["mode"] == "shadow"
    assert body["embedding_tokens_used"] == await _ledger(
        workspace["workspace"]["id"], "embedding_tokens"
    )
    assert body["writes"] >= 1
    assert [row["principal_key"] for row in body["by_agent"]] == [token["canonical_id"]]
