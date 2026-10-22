"""Agent onboarding over MCP and REST: four steps, role paths, idempotence."""

from __future__ import annotations

import json
import os
import uuid

import asyncpg
import httpx
import pytest

pytestmark = pytest.mark.integration

BRIEF = {"summary": "done", "key_metrics": {}, "unverified_hypotheses": [], "risk_flag": False}


def _db() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def _agent(client, admin_headers, workspace, role, namespaces=None):
    signature = f"{role.lower()}_{uuid.uuid4().hex[:6]}"
    response = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        headers=admin_headers,
        json={"agent_signature": signature, "role_scope": role,
              "allowed_namespaces": namespaces or ["*"]},
    )
    assert response.status_code in (200, 201), response.text
    return {"Authorization": f"Bearer {response.json()['api_key']}"}, signature


async def _audits(workspace_id: str, signature: str) -> list[str]:
    conn = await asyncpg.connect(_db())
    try:
        rows = await conn.fetch(
            "SELECT action FROM access_audit WHERE workspace_id = $1::uuid "
            "AND details->>'principal' = $2 ORDER BY id",
            workspace_id, signature,
        )
        return [row["action"] for row in rows]
    finally:
        await conn.close()


async def _steps_to_first_action(client, agent):
    """Steps 1-3; returns the hint that names the role's first action."""
    first = await client.get("/v1/orientation", headers=agent)
    assert first.status_code == 200
    block = first.json()["onboarding"]
    version = block["next_call"]["args"]["version"]
    acked = (await client.post("/v1/orientation/ack", headers=agent,
                               json={"version": version})).json()
    readme = acked["onboarding"]["next_call"]
    assert readme["tool"] == "dereference"
    read = await client.post("/v1/dereference", headers=agent, json=readme["args"])
    assert read.status_code == 200, read.text
    hint = read.json()["onboarding"]
    assert hint["state"] == "needs_first_action" and hint["completed_steps"] == [1, 2, 3]
    return hint


async def test_executor_path_and_idempotence(client, admin_headers, workspace):
    agent, signature = await _agent(client, admin_headers, workspace, "EXECUTOR")
    first = await client.get("/v1/orientation", headers=agent)
    pack = first.json()
    block = pack["onboarding"]
    assert block["state"] == "new" and block["step"] == 1 and block["total_steps"] == 4
    assert block["next_call"]["tool"] == "ack_constitution"
    assert block["next_call"]["args"]["version"] == pack["constitution"]["version"]
    assert 1 <= len(block["required_reading"]) <= 3
    assert all({"uri", "why"} <= set(item) for item in block["required_reading"])
    assert block["do_not"] and block["first_time"] is True
    assert len(first.content) <= 12_288 + 2_048  # a small first response
    assert {"me", "constitution", "next_calls"} <= set(pack)  # existing fields kept

    again = (await client.get("/v1/orientation", headers=agent)).json()["onboarding"]
    assert again["state"] == "oriented" and again["completed_steps"] == [1]
    constitution = await client.get("/v1/documents/system/constitution", headers=agent)
    assert constitution.json()["onboarding"]["state"] == "needs_ack"

    version = again["next_call"]["args"]["version"]
    acked = (await client.post("/v1/orientation/ack", headers=agent,
                               json={"version": version})).json()
    assert acked["onboarding"]["state"] == "needs_namespace_readme"
    assert acked["onboarding"]["next_call"]["args"]["uri"] == "doc://system/namespaces/default"
    await client.post("/v1/orientation/ack", headers=agent, json={"version": version})

    read = await client.post("/v1/dereference", headers=agent,
                             json=acked["onboarding"]["next_call"]["args"])
    hint = read.json()["onboarding"]
    assert hint["next_call"] == {"tool": "workspace_sync",
                                 "args": {"task_statuses": ["READY", "HANDOFF"]},
                                 "why": "Find tasks you can claim."}
    sync = await client.get("/v1/workspace/map", headers=agent)
    assert sync.json()["onboarding"]["next_call"]["tool"] == "agent_claim_and_read"

    task = await client.post("/v1/tasks", headers=workspace["headers"],
                             json={"title": "claim me"})
    claimed = await client.post(f"/v1/tasks/{task.json()['task_key']}/claim", headers=agent,
                                json={})
    assert claimed.status_code == 200, claimed.text
    done = claimed.json()["onboarding"]
    assert done["state"] == "active" and done["completed_steps"] == [1, 2, 3, 4]
    assert done["next_call"] is None
    after = await client.get("/v1/documents", headers=agent)
    assert "onboarding" not in after.json()  # no more prompting
    final = (await client.get("/v1/orientation", headers=agent)).json()["onboarding"]
    assert final["state"] == "active" and final["do_not"] == []

    actions = await _audits(workspace["workspace"]["id"], signature)
    for action in ("agent.oriented", "agent.acknowledged", "agent.namespace_read",
                   "agent.first_action", "agent.onboarded"):
        assert actions.count(action) == 1, (action, actions)


async def test_scout_analyst_and_auditor_paths(client, admin_headers, workspace):
    scout, _ = await _agent(client, admin_headers, workspace, "SCOUT")
    hint = await _steps_to_first_action(client, scout)
    assert hint["next_call"] == {"tool": "list_documents", "args": {"namespace": "raw_inbox"},
                                 "why": "See what is already collected before adding to it."}
    listed = await client.get("/v1/documents", headers=scout, params={"namespace": "raw_inbox"})
    write = listed.json()["onboarding"]["next_call"]
    assert write["tool"] == "upsert_shared_doc"
    stored = await client.put(
        f"/v1/documents/{write['args']['doc_key']}", headers=scout,
        json={"namespace": "raw_inbox", "content": "# first finding", "expected_version": 0},
    )
    assert stored.status_code == 200, stored.text
    assert stored.json()["onboarding"]["state"] == "active"

    analyst, _ = await _agent(client, admin_headers, workspace, "ANALYST")
    hint = await _steps_to_first_action(client, analyst)
    assert hint["next_call"]["tool"] == "list_documents"
    listed = await client.get("/v1/documents", headers=analyst)
    assert listed.json()["onboarding"]["next_call"]["tool"] == "create_task"
    created = await client.post("/v1/tasks", headers=analyst, json={"title": "analysis"})
    assert created.status_code == 201
    assert created.json()["onboarding"]["state"] == "active"

    auditor, _ = await _agent(client, admin_headers, workspace, "AUDITOR")
    hint = await _steps_to_first_action(client, auditor)
    assert hint["next_call"]["args"] == {"task_statuses": ["REVIEWING"]}
    executor, _ = await _agent(client, admin_headers, workspace, "EXECUTOR")
    task = (await client.post("/v1/tasks", headers=workspace["headers"],
                              json={"title": "to review"})).json()
    claimed = (await client.post(f"/v1/tasks/{task['task_key']}/claim", headers=executor,
                                 json={})).json()
    handed = await client.post(f"/v1/tasks/{task['task_key']}/handoff", headers=executor, json={
        "next_status": "REVIEWING", "brief": BRIEF,
        "lease_epoch": claimed["task"]["lease_epoch"],
    })
    assert handed.status_code == 200, handed.text
    queue = await client.get("/v1/workspace/map", headers=auditor)
    assert queue.json()["onboarding"]["next_call"]["tool"] == "review_task"
    reviewed = await client.post(f"/v1/tasks/{task['task_key']}/review", headers=auditor,
                                 json={"decision": "approve"})
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["onboarding"]["state"] == "active"


async def test_restricted_token_gets_its_own_readme(client, admin_headers, workspace):
    agent, _ = await _agent(client, admin_headers, workspace, "ANALYST", namespaces=["ideas"])
    await client.put("/v1/documents/seed", headers=workspace["headers"],
                     json={"namespace": "ideas", "content": "seed", "expected_version": 0})
    block = (await client.get("/v1/orientation", headers=agent)).json()["onboarding"]
    uris = [item["uri"] for item in block["required_reading"]]
    assert "doc://system/namespaces/ideas" in uris


async def test_mcp_instructions_order_and_hints(client, admin_headers, workspace, api_base_url):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    agent, _ = await _agent(client, admin_headers, workspace, "EXECUTOR")
    async with httpx.AsyncClient(headers=agent, timeout=60) as http:
        async with streamable_http_client(f"{api_base_url}/mcp/", http_client=http) as (
                read, write, _):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert "Call get_orientation first before any other tool" in init.instructions
                tools = (await session.list_tools()).tools
                assert tools[0].name == "get_orientation"
                assert "FIRST CALL" in tools[0].description
                # Calling another tool first still points back to step 1.
                early = await session.call_tool("list_documents", {})
                assert early.structuredContent["onboarding"]["next_call"]["tool"] == \
                    "get_orientation"
                oriented = await session.call_tool("get_orientation", {})
                assert oriented.structuredContent["onboarding"]["state"] == "new"
                text = oriented.content[0].text
                assert json.loads(text)["onboarding"]["next_call"]["tool"] == "ack_constitution"


async def test_console_sees_agent_onboarding_without_secrets(client, admin_headers, workspace):
    agent, signature = await _agent(client, admin_headers, workspace, "EXECUTOR")
    idle, idle_signature = await _agent(client, admin_headers, workspace, "SCOUT")
    del idle
    await client.get("/v1/orientation", headers=agent)
    email = f"console-{uuid.uuid4().hex[:8]}@example.test"
    await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/human-users",
        headers=admin_headers,
        json={"email": email, "display_name": "O", "role": "OWNER",
              "password": "correct-horse-battery"},
    )
    login = await client.post("/v1/auth/login",
                              json={"email": email, "password": "correct-horse-battery"})
    owner = {"Authorization": f"Bearer {login.json()['token']}"}
    overview = await client.get("/v1/human/agent-onboarding", headers=owner)
    assert overview.status_code == 200, overview.text
    body = overview.json()
    states = {item["agent_signature"]: item for item in body["items"]}
    assert states[signature]["state"] == "oriented"
    assert states[signature]["completed_steps"] == [1]
    assert states[idle_signature]["state"] == "new"
    assert body["summary"]["tokens"] == 2 and body["summary"]["orientation_rate"] == 0.5
    assert "agt_" not in overview.text and "api_key" not in overview.text
    admin_view = await client.get(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-onboarding",
        headers=admin_headers,
    )
    assert admin_view.status_code == 200
    assert (await client.get("/v1/human/agent-onboarding", headers=agent)).status_code in (
        401, 403)
