from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest

pytestmark = pytest.mark.integration


async def make_agent(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    workspace: dict,
    role: str,
    namespaces: list[str] | None = None,
) -> dict:
    signature = f"{role.lower()}_{uuid.uuid4().hex[:6]}"
    response = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        json={
            "agent_signature": signature,
            "role_scope": role,
            "allowed_namespaces": namespaces or ["*"],
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return {
        "signature": signature,
        "token_id": body["token"]["id"],
        "api_key": body["api_key"],
        "headers": {"Authorization": f"Bearer {body['api_key']}"},
    }


def task_key(prefix: str = "TASK") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


async def create_task(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    key: str,
    **overrides,
) -> dict:
    response = await client.post(
        "/v1/tasks",
        headers=headers,
        json={"task_key": key, "title": f"Task {key}", **overrides},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def claim(client: httpx.AsyncClient, headers: dict[str, str], key: str, lease=60):
    response = await client.post(
        f"/v1/tasks/{key}/claim",
        headers=headers,
        json={"lease_seconds": lease},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def handoff(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    key: str,
    **overrides,
):
    payload = {
        "next_status": "REVIEWING",
        "brief": {
            "summary": "work complete",
            "key_metrics": {"score": 0.9},
            "unverified_hypotheses": [],
            "risk_flag": False,
        },
        **overrides,
    }
    response = await client.post(
        f"/v1/tasks/{key}/handoff", headers=headers, json=payload
    )
    assert response.status_code == 200, response.text
    return response.json()


async def read_sse_event(lines, timeout: float = 12.0) -> dict:
    event_id = None
    while True:
        line = await asyncio.wait_for(lines.__anext__(), timeout=timeout)
        if line.startswith("id: "):
            event_id = int(line[4:])
        if line.startswith("data: "):
            payload = json.loads(line[6:])
            if payload.get("table"):
                payload["_sse_id"] = event_id or payload.get("id")
                return payload


async def test_admin_auth_agent_tokens_and_rbac(client, admin_headers, workspace):
    unauthorized = await client.get("/v1/admin/workspaces")
    assert unauthorized.status_code == 401
    created = await client.post(
        "/v1/admin/workspaces",
        headers=admin_headers,
        json={"name": f"rbac-{uuid.uuid4().hex[:6]}", "tier": "developer_5usd"},
    )
    assert created.status_code == 201
    wid = created.json()["workspace"]["id"]
    commander = created.json()["api_key"]
    commander_headers = {"Authorization": f"Bearer {commander}"}
    who = await client.get("/v1/whoami", headers=commander_headers)
    assert who.status_code == 200
    assert who.json()["role"] == "COMMANDER"
    assert who.json()["auth_kind"] == "workspace"

    local_workspace = {"workspace": {"id": wid}}
    executor = await make_agent(client, admin_headers, local_workspace, "EXECUTOR")
    analyst = await make_agent(client, admin_headers, local_workspace, "ANALYST")
    scout = await make_agent(
        client, admin_headers, local_workspace, "SCOUT", ["raw_inbox"]
    )
    auditor = await make_agent(client, admin_headers, local_workspace, "AUDITOR")

    executor_who = await client.get("/v1/whoami", headers=executor["headers"])
    assert executor_who.json()["role"] == "EXECUTOR"
    assert (await client.get("/v1/whoami", headers=scout["headers"])).json()["role"] == "SCOUT"

    scout_slots = await client.get("/v1/slots", headers=scout["headers"])
    assert scout_slots.status_code == 403
    assert scout_slots.json()["error"]["code"] == "forbidden"

    raw_doc = f"raw_inbox/note-{uuid.uuid4().hex[:6]}"
    scout_create = await client.put(
        f"/v1/documents/{raw_doc}",
        headers=scout["headers"],
        json={
            "content": "raw observation",
            "frontmatter": {"source": "web"},
            "expected_version": 0,
            "namespace": "raw_inbox",
        },
    )
    assert scout_create.status_code == 200, scout_create.text
    scout_update = await client.put(
        f"/v1/documents/{raw_doc}",
        headers=scout["headers"],
        json={
            "content": "overwrite",
            "frontmatter": {},
            "expected_version": 1,
            "namespace": "raw_inbox",
        },
    )
    assert scout_update.status_code == 403

    wrong_ns = await client.put(
        f"/v1/documents/default/forbidden-{uuid.uuid4().hex[:6]}",
        headers=scout["headers"],
        json={"content": "x", "frontmatter": {}, "expected_version": 0},
    )
    assert wrong_ns.status_code == 403
    assert wrong_ns.json()["error"]["code"] == "NAMESPACE_FORBIDDEN"

    analyst_slot = await client.put(
        "/v1/slots/analyst:test",
        headers=analyst["headers"],
        json={"state": {"x": 1}, "expected_version": 0},
    )
    assert analyst_slot.status_code == 403

    executor_slot = await client.put(
        "/v1/slots/executor:test",
        headers=executor["headers"],
        json={"state": {"x": 1}, "expected_version": 0},
    )
    assert executor_slot.status_code == 200

    audit_read = await client.get("/v1/audit", headers=auditor["headers"])
    assert audit_read.status_code == 200
    analyst_audit = await client.get("/v1/audit", headers=analyst["headers"])
    assert analyst_audit.status_code == 403

    tokens = await client.get(
        f"/v1/admin/workspaces/{wid}/agent-tokens", headers=admin_headers
    )
    assert tokens.status_code == 200
    assert len(tokens.json()["items"]) == 4

    disabled = await client.patch(
        f"/v1/admin/workspaces/{wid}/agent-tokens/{auditor['token_id']}",
        headers=admin_headers,
        json={"is_active": False},
    )
    assert disabled.status_code == 200
    blocked = await client.get("/v1/whoami", headers=auditor["headers"])
    assert blocked.status_code == 401


async def test_state_grid_human_sovereignty_and_batch_undo(client, admin_headers, workspace):
    wid = workspace["workspace"]["id"]
    executor = await make_agent(client, admin_headers, workspace, "EXECUTOR")
    slot_key = f"trading:regime-{uuid.uuid4().hex[:5]}"
    created = await client.put(
        f"/v1/slots/{slot_key}",
        headers=executor["headers"],
        json={
            "state": {"bias": "LONG", "confidence": 0.6},
            "expected_version": 0,
            "goal": "bias",
        },
    )
    assert created.status_code == 200
    version = created.json()["slot"]["version"]

    patched = await client.post(
        f"/v1/slots/{slot_key}/patch",
        headers=executor["headers"],
        json={
            "ops": [{"op": "replace", "path": "/confidence", "value": 0.8}],
            "expected_version": version,
        },
    )
    assert patched.status_code == 200
    version = patched.json()["slot"]["version"]
    assert patched.json()["slot"]["current_state"]["confidence"] == 0.8

    stale = await client.post(
        f"/v1/slots/{slot_key}/patch",
        headers=executor["headers"],
        json={
            "ops": [{"op": "replace", "path": "/confidence", "value": 0.1}],
            "expected_version": version - 1,
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "version_conflict"
    assert stale.json()["error"]["details"]["latest"]["version"] == version

    human = await client.put(
        f"/v1/admin/workspaces/{wid}/slots/{slot_key}",
        headers=admin_headers,
        json={
            "state": {"bias": "SHORT", "confidence": 0.95},
            "expected_version": version,
            "human_note": "human risk off",
        },
    )
    assert human.status_code == 200, human.text
    version = human.json()["slot"]["version"]
    assert human.json()["slot"]["locked_by_human"] is True

    blocked = await client.post(
        f"/v1/slots/{slot_key}/patch",
        headers=executor["headers"],
        json={
            "ops": [{"op": "replace", "path": "/bias", "value": "LONG"}],
            "expected_version": version,
        },
    )
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "HUMAN_SOVEREIGNTY_LOCKED"

    unlocked = await client.put(
        f"/v1/admin/workspaces/{wid}/slots/{slot_key}",
        headers=admin_headers,
        json={"expected_version": version, "unlock": True},
    )
    assert unlocked.status_code == 200
    version = unlocked.json()["slot"]["version"]
    assert unlocked.json()["slot"]["locked_by_human"] is False

    batch = uuid.uuid4()
    changed = await client.post(
        f"/v1/slots/{slot_key}/patch",
        headers=executor["headers"],
        json={
            "ops": [{"op": "replace", "path": "/bias", "value": "LONG"}],
            "expected_version": version,
            "session_batch_id": str(batch),
        },
    )
    assert changed.status_code == 200
    version = changed.json()["slot"]["version"]

    audit = await client.get(
        f"/v1/admin/workspaces/{wid}/audit",
        headers=admin_headers,
        params={"batch_id": str(batch)},
    )
    assert audit.status_code == 200
    assert audit.json()["batches"][0]["entry_count"] == 1
    undo = await client.post(
        f"/v1/admin/workspaces/{wid}/batches/{batch}/undo",
        headers=admin_headers,
    )
    assert undo.status_code == 200
    assert undo.json()["restored"] == 1
    restored = await client.get(f"/v1/slots/{slot_key}", headers=executor["headers"])
    assert restored.status_code == 200
    assert restored.json()["current_state"]["bias"] == "SHORT"
    assert restored.json()["version"] > version

    timeline = await client.get(
        f"/v1/admin/workspaces/{wid}/timeline", headers=admin_headers
    )
    assert any(
        item["event_type"] == "HUMAN_OVERRIDE"
        for item in timeline.json()["items"]
    )
    assert any(
        item["event_type"] == "BATCH_UNDONE"
        for item in timeline.json()["items"]
    )


async def test_task_lease_handoff_l2_and_dependencies(client, admin_headers, workspace):
    wid = workspace["workspace"]["id"]
    commander = workspace["headers"]
    executor = await make_agent(client, admin_headers, workspace, "EXECUTOR")
    executor2 = await make_agent(client, admin_headers, workspace, "EXECUTOR")
    auditor = await make_agent(client, admin_headers, workspace, "AUDITOR")
    key = task_key()
    await create_task(client, commander, key, assigned_role="EXECUTOR")
    claimed = await claim(client, executor["headers"], key)
    assert claimed["task"]["status"] == "IN_PROGRESS"
    assert claimed["session_batch_id"]

    conflict = await client.post(
        f"/v1/tasks/{key}/claim",
        headers=executor2["headers"],
        json={"lease_seconds": 60},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "lease_conflict"

    renewed = await client.post(
        f"/v1/tasks/{key}/renew",
        headers=executor["headers"],
        json={"lease_seconds": 120},
    )
    assert renewed.status_code == 200

    ho = await handoff(
        client,
        executor["headers"],
        key,
        artifact_text="# Full L2 report\n\nDetailed evidence.",
    )
    assert ho["task"]["status"] == "REVIEWING"
    assert ho["artifact_uri"].startswith("doc://artifact/")
    assert ho["task"]["claimed_by"] is None

    l2 = await client.post(
        "/v1/dereference",
        headers=auditor["headers"],
        json={"uri": ho["artifact_uri"]},
    )
    assert l2.status_code == 200
    assert "Detailed evidence" in l2.json()["document"]["content"]

    approved = await client.post(
        f"/v1/tasks/{key}/review",
        headers=auditor["headers"],
        json={
            "decision": "approve",
            "feedback": "verified",
            "expected_version": ho["task"]["version"],
        },
    )
    assert approved.status_code == 200
    assert approved.json()["task"]["status"] == "DONE"

    parent = task_key("PARENT")
    child = task_key("CHILD")
    await create_task(client, commander, parent, assigned_role="EXECUTOR")
    child_task = await create_task(
        client,
        commander,
        child,
        assigned_role="EXECUTOR",
        depends_on=[parent],
    )
    assert child_task["status"] == "TODO"
    await claim(client, executor["headers"], parent)
    handed = await handoff(
        client,
        executor["headers"],
        parent,
        next_status="REVIEWING",
    )
    reviewed = await client.post(
        f"/v1/tasks/{parent}/review",
        headers=commander,
        json={
            "decision": "approve",
            "next_status": "DONE",
            "expected_version": handed["task"]["version"],
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    child_after = await client.get(f"/v1/tasks/{child}", headers=commander)
    assert child_after.status_code == 200
    assert child_after.json()["status"] == "READY"

    tasks = await client.get(
        f"/v1/admin/workspaces/{wid}/tasks", headers=admin_headers
    )
    assert tasks.status_code == 200
    assert {item["task_key"] for item in tasks.json()["items"]} >= {key, parent, child}


async def test_ping_pong_circuit_breaker(client, admin_headers, workspace):
    commander = workspace["headers"]
    executor = await make_agent(client, admin_headers, workspace, "EXECUTOR")
    auditor = await make_agent(client, admin_headers, workspace, "AUDITOR")
    key = task_key("PP")
    await create_task(
        client,
        commander,
        key,
        assigned_role="EXECUTOR",
        max_retries=2,
    )
    for index in range(2):
        await claim(client, executor["headers"], key)
        ho = await handoff(client, executor["headers"], key)
        rejected = await client.post(
            f"/v1/tasks/{key}/review",
            headers=auditor["headers"],
            json={
                "decision": "reject",
                "feedback": f"attempt {index + 1}",
                "expected_version": ho["task"]["version"],
            },
        )
        assert rejected.status_code == 200
        if index == 0:
            assert rejected.json()["task"]["status"] == "READY"
            assert rejected.json()["task"]["ping_pong_count"] == 1
            assert rejected.json()["escalated"] is False
        else:
            assert rejected.json()["task"]["status"] == "ESCALATED_TO_HUMAN"
            assert rejected.json()["task"]["ping_pong_count"] == 2
            assert rejected.json()["escalated"] is True

    blocked = await client.post(
        f"/v1/tasks/{key}/claim",
        headers=executor["headers"],
        json={"lease_seconds": 60},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "escalated_to_human"

    reviewable = await client.get(f"/v1/tasks/{key}", headers=commander)
    approved = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/tasks/{key}/review",
        headers=admin_headers,
        json={
            "decision": "approve",
            "feedback": "human resolved",
            "expected_version": reviewable.json()["version"],
        },
    )
    assert approved.status_code == 200
    assert approved.json()["task"]["status"] == "DONE"
    assert approved.json()["task"]["ping_pong_count"] == 0


async def test_documents_sections_search_and_human_lock(client, admin_headers, workspace):
    wid = workspace["workspace"]["id"]
    analyst = await make_agent(client, admin_headers, workspace, "ANALYST")
    key = f"strategy/plan-{uuid.uuid4().hex[:6]}"
    created = await client.put(
        f"/v1/documents/{key}",
        headers=analyst["headers"],
        json={
            "title": "Weekly plan",
            "content": "# Weekly plan\n\n## Risk\nkeep 2%\n\n## Levels\nbuy 64200",
            "frontmatter": {"status": "DRAFT", "risk_limit_pct": 2.5},
            "expected_version": 0,
        },
    )
    assert created.status_code == 200
    version = created.json()["document"]["version"]
    assert created.json()["document"]["agent_view"].startswith("---\n")

    patched = await client.post(
        f"/v1/documents/{key}/patch-section",
        headers=analyst["headers"],
        json={
            "section_heading": "Risk",
            "new_markdown": "keep 3%",
            "expected_version": version,
        },
    )
    assert patched.status_code == 200
    version = patched.json()["document"]["version"]
    assert "keep 3%" in patched.json()["document"]["content"]

    stale = await client.post(
        f"/v1/documents/{key}/patch-section",
        headers=analyst["headers"],
        json={
            "section_heading": "Risk",
            "new_markdown": "keep 1%",
            "expected_version": version - 1,
        },
    )
    assert stale.status_code == 409

    query = await client.post(
        "/v1/query",
        headers=analyst["headers"],
        json={
            "query": "risk plan",
            "filters": {"status": "DRAFT"},
            "limit": 5,
            "include_unverified": True,
        },
    )
    assert query.status_code == 200
    assert any(item["doc_key"] == key for item in query.json()["items"])

    locked = await client.put(
        f"/v1/admin/workspaces/{wid}/documents/{key}",
        headers=admin_headers,
        json={
            "content": "# Weekly plan\n\n## Risk\nhuman says 1%\n\n## Levels\nbuy 64200",
            "expected_version": version,
            "human_note": "human edit",
        },
    )
    assert locked.status_code == 200
    version = locked.json()["document"]["version"]
    assert locked.json()["document"]["locked_by_human"] is True

    blocked = await client.post(
        f"/v1/documents/{key}/patch-section",
        headers=analyst["headers"],
        json={
            "section_heading": "Risk",
            "new_markdown": "agent overwrite",
            "expected_version": version,
        },
    )
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "HUMAN_SOVEREIGNTY_LOCKED"

    unlocked = await client.put(
        f"/v1/admin/workspaces/{wid}/documents/{key}",
        headers=admin_headers,
        json={"expected_version": version, "unlock": True},
    )
    assert unlocked.status_code == 200
    version = unlocked.json()["document"]["version"]
    allowed = await client.post(
        f"/v1/documents/{key}/patch-section",
        headers=analyst["headers"],
        json={
            "section_heading": "Risk",
            "new_markdown": "agent says 2.5%",
            "expected_version": version,
        },
    )
    assert allowed.status_code == 200

    admin_created = await client.post(
        f"/v1/admin/workspaces/{wid}/documents",
        headers=admin_headers,
        json={
            "doc_key": f"admin/doc-{uuid.uuid4().hex[:6]}",
            "title": "Admin doc",
            "content": "# Created by human",
            "frontmatter": {},
            "expected_version": 0,
        },
    )
    assert admin_created.status_code == 201


async def test_sse_delivers_state_changes_real(client, api_base_url, admin_headers, workspace):
    executor = await make_agent(client, admin_headers, workspace, "EXECUTOR")
    # This test uses its own client because it must stream concurrently.
    async with httpx.AsyncClient(base_url=api_base_url, timeout=30) as http:
        slot_key = f"sse:slot-{uuid.uuid4().hex[:5]}"
        created = await http.put(
            f"/v1/slots/{slot_key}",
            headers=executor["headers"],
            json={"state": {"value": 1}, "expected_version": 0},
        )
        assert created.status_code == 200
        version = created.json()["slot"]["version"]
        async with http.stream(
            "GET",
            "/v1/stream?tables=context_slots",
            headers=executor["headers"],
        ) as response:
            assert response.status_code == 200
            lines = response.aiter_lines()
            async for line in lines:
                if line.startswith("event: connected"):
                    break
            patch = await http.post(
                f"/v1/slots/{slot_key}/patch",
                headers=executor["headers"],
                json={
                    "ops": [{"op": "replace", "path": "/value", "value": 2}],
                    "expected_version": version,
                },
            )
            assert patch.status_code == 200
            event = await read_sse_event(lines)
            assert event["table"] == "context_slots"
            assert event["slot_key"] == slot_key


async def test_mcp_v3_tools(client, api_base_url, admin_headers, workspace):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    executor = await make_agent(
        client, admin_headers, workspace, "EXECUTOR"
    )
    commander = workspace["headers"]
    key = task_key("MCP")
    async with httpx.AsyncClient(base_url=api_base_url, timeout=30) as http:
        created = await http.post(
            "/v1/tasks",
            headers=commander,
            json={"task_key": key, "title": "MCP task", "assigned_role": "EXECUTOR"},
        )
        assert created.status_code == 201
        slot = await http.put(
            "/v1/slots/mcp:slot",
            headers=commander,
            json={"state": {"bias": "LONG"}, "expected_version": 0},
        )
        assert slot.status_code == 200
        version = slot.json()["slot"]["version"]
        raw = await http.post(
            "/mcp/",
            headers={
                **executor["headers"],
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "integration", "version": "1"},
                },
            },
        )
        assert raw.status_code == 200, raw.text
        assert raw.headers["content-type"].startswith("application/json")
        async with httpx.AsyncClient(
            base_url=api_base_url,
            headers=executor["headers"],
            timeout=60,
        ) as mcp_http:
            async with streamable_http_client(
                f"{api_base_url}/mcp/", http_client=mcp_http
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert {tool.name for tool in tools.tools} == {
                        "workspace_sync",
                        "agent_claim_and_read",
                        "agent_patch_shared_state",
                        "agent_handoff_task",
                        "agent_escalate_to_human",
                        "upsert_shared_doc",
                        "patch_doc_section",
                        "query_workspace",
                        "dereference",
                        "propose_document_change",
                        "vote_on_proposal",
                        "list_document_proposals",
                        "draft_external_action",
                        "create_task",
                        "review_task",
                        "request_human_review",
                        "list_documents",
                        "upsert_many",
                        "dereference_many",
                        "archive_document",
                        "restore_document",
                        "rename_document",
                        "register_namespace",
                        "list_namespaces",
                        "get_token",
                        "get_lineage",
                        "list_token_tree",
                        "issue_child_token",
                        "revoke_token",
                        "renew_token",
                        "register_agent",
                        "get_agent",
                        "list_agents",
                        "update_agent",
                        "set_status",
                        "list_tasks",
                        "write_continuation",
                        "create_subtasks",
                        "get_task_tree",
                        "clear_taint",
                        "recall_document",
                        "get_recall",
                        "get_budget",
                        "set_budget",
                        "list_budgets",
                        "explain_write",
                        "register_parts",
                        "get_orientation",
                        "ack_constitution",
                        "get_usage",
                        "get_index_status",
                        "heartbeat",
                        "release_lease",
                        "list_leases",
                        "export_workspace",
                        "get_export",
                        "list_exports",
                        "poll_events",
                        "subscribe",
                        "list_subscriptions",
                        "unsubscribe",
                        "get_api_docs",
                    }
                    sync = await session.call_tool("workspace_sync", {})
                    assert sync.isError is False
                    sync_payload = json.loads(sync.content[0].text)
                    assert sync_payload["permissions"]
                    assert sync.structuredContent is not None
                    assert "result" not in sync.structuredContent
                    assert isinstance(sync.structuredContent["tasks"], list)
                    handoff_tool = next(
                        tool for tool in tools.tools if tool.name == "agent_handoff_task"
                    )
                    assert "summary" in json.dumps(handoff_tool.inputSchema)
                    claim_tool = next(
                        tool for tool in tools.tools if tool.name == "agent_claim_and_read"
                    )
                    # The server picks the TTL when none is sent (900 s under 2026-11).
                    properties = claim_tool.inputSchema["properties"]
                    for name, low in (("lease_seconds", 5), ("lease_ttl_seconds", 60)):
                        lease_schema = properties[name]
                        assert lease_schema.get("default") is None
                        bounds = next(
                            item for item in lease_schema.get("anyOf", [lease_schema])
                            if item.get("type") == "integer"
                        )
                        assert bounds["minimum"] == low and bounds["maximum"] == 3600
                    create_tool = next(
                        tool for tool in tools.tools if tool.name == "create_task"
                    )
                    batch_schema = create_tool.inputSchema["properties"]["session_batch_id"]
                    assert any(
                        item.get("format") == "uuid"
                        for item in batch_schema.get("anyOf", [batch_schema])
                    )
                    assert any(
                        item["task_key"] == key
                        for item in sync_payload["tasks"]
                    )
                    mcp_created = await session.call_tool(
                        "create_task",
                        {"title": "MCP created task", "assigned_role": "EXECUTOR"},
                    )
                    assert mcp_created.isError is False
                    assert json.loads(mcp_created.content[0].text)["title"] == "MCP created task"
                    claimed = await session.call_tool(
                        "agent_claim_and_read",
                        {"task_key": key, "lease_seconds": 3600},
                    )
                    assert claimed.isError is False
                    assert json.loads(claimed.content[0].text)["task"]["status"] == "IN_PROGRESS"
                    patched = await session.call_tool(
                        "agent_patch_shared_state",
                        {
                            "slot_key": "mcp:slot",
                            "ops": [{"op": "replace", "path": "/bias", "value": "SHORT"}],
                            "expected_version": version,
                        },
                    )
                    assert patched.isError is False
                    doc = await session.call_tool(
                        "upsert_shared_doc",
                        {
                            "doc_key": "mcp/doc",
                            "content": "# MCP doc\n\nrisk note",
                            "frontmatter": {"status": "DRAFT"},
                            "expected_version": 0,
                        },
                    )
                    assert doc.isError is False
                    ns_doc = await session.call_tool(
                        "upsert_shared_doc",
                        {
                            "doc_key": "mcp/ns-doc",
                            "content": "# Namespace doc\n\nv1",
                            "namespace": "mcpns",
                            "expected_version": 0,
                        },
                    )
                    assert ns_doc.isError is False
                    proposal = await session.call_tool(
                        "propose_document_change",
                        {
                            "doc_key": "mcp/ns-doc",
                            "content": "# Namespace doc\n\nv2",
                            "base_version": 1,
                        },
                    )
                    assert proposal.isError is False
                    proposal_id = json.loads(proposal.content[0].text)["id"]
                    review_request = await session.call_tool(
                        "request_human_review",
                        {"kind": "proposal", "key": proposal_id},
                    )
                    assert review_request.isError is False
                    missing = await session.call_tool(
                        "agent_claim_and_read", {"task_key": "missing-task"}
                    )
                    assert missing.isError is True
                    assert '"http_status": 404' in missing.content[0].text
                    assert '"code": "not_found"' in missing.content[0].text
                    assert missing.structuredContent["code"] == "not_found"
                    unknown_tool = await session.call_tool("whoami", {})
                    assert unknown_tool.isError is True
                    unknown_payload = json.loads(unknown_tool.content[0].text)
                    assert unknown_payload["code"] == "unknown_tool"
                    assert unknown_payload["http_status"] == 404
                    assert "unknown tool" in unknown_payload["message"]

                    invalid_args = await session.call_tool(
                        "create_task",
                        {"title": "invalid role", "assigned_role": "NOT_A_ROLE"},
                    )
                    assert invalid_args.isError is True
                    invalid_envelope = json.loads(invalid_args.content[0].text)
                    assert invalid_envelope["code"] == "validation_error"
                    assert invalid_envelope["http_status"] == 422
                    assert invalid_args.structuredContent["code"] == "validation_error"
                    assert invalid_args.structuredContent["details"]
                    found = await session.call_tool(
                        "query_workspace",
                        {
                            "filters": {"status": "DRAFT"},
                            "limit": 5,
                            "include_unverified": True,
                        },
                    )
                    assert found.isError is False
                    assert any(
                        item["doc_key"] == "mcp/doc"
                        for item in json.loads(found.content[0].text)["items"]
                    )
                    hand = await session.call_tool(
                        "agent_handoff_task",
                        {
                            "task_key": key,
                            "next_status": "REVIEWING",
                            "brief": {
                                "summary": "MCP handoff",
                                "key_metrics": {"x": 1},
                                "unverified_hypotheses": [],
                                "risk_flag": False,
                            },
                            "artifact_text": "# MCP artifact\n\nL2 body",
                        },
                    )
                    assert hand.isError is False
                    hand_task = json.loads(hand.content[0].text)
                    reviewed = await client.post(
                        f"/v1/tasks/{key}/review",
                        headers=commander,
                        json={
                            "decision": "approve",
                            "next_status": "DONE",
                            "expected_version": hand_task["task"]["version"],
                        },
                    )
                    assert reviewed.status_code == 200, reviewed.text
                    artifact_uri = hand_task["artifact_uri"]
                    deref = await session.call_tool(
                        "dereference", {"uri": artifact_uri}
                    )
                    assert deref.isError is False
                    assert "L2 body" in json.loads(deref.content[0].text)["document"]["content"]
                    after = await session.call_tool("workspace_sync", {})
                    after_payload = json.loads(after.content[0].text)
                    output = next(
                        item for item in after_payload["outputs"] if item["task_key"] == key
                    )
                    assert output["artifact_uri"] == artifact_uri
                    assert output["handoff_summary"] == "MCP handoff"
                    compact_sync = await session.call_tool(
                        "workspace_sync", {"compact": True}
                    )
                    compact_payload = json.loads(compact_sync.content[0].text)
                    compact_task = next(
                        item for item in compact_payload["tasks"] if item["task_key"] == key
                    )
                    assert compact_task["artifact_uri"] == artifact_uri
                    assert compact_task["handoff_summary"] == "MCP handoff"
                    listed = await session.call_tool(
                        "list_documents",
                        {"namespace": "mcpns", "limit": 100},
                    )
                    assert listed.isError is False
                    listed_payload = json.loads(listed.content[0].text)
                    assert listed_payload["next_cursor"] is None
                    assert "mcp/ns-doc" in {
                        item["doc_key"] for item in listed_payload["items"]
                    }
                    many = await session.call_tool(
                        "upsert_many",
                        {
                            "items": [
                                {
                                    "doc_key": "mcpns/many-a",
                                    "content": "# many a",
                                    "namespace": "mcpns",
                                    "expected_version": 0,
                                },
                                {
                                    "doc_key": "mcpns/many-b",
                                    "content": "# many b",
                                    "namespace": "mcpns",
                                    "expected_version": 0,
                                },
                            ]
                        },
                    )
                    assert many.isError is False, many.content[0].text
                    many_payload = json.loads(many.content[0].text)
                    assert len(many_payload["items"]) == 2
                    deref_many = await session.call_tool(
                        "dereference_many",
                        {
                            "uris": [
                                "doc://mcpns/many-a",
                                "doc://mcpns/missing",
                            ]
                        },
                    )
                    assert deref_many.isError is False
                    deref_payload = json.loads(deref_many.content[0].text)
                    assert deref_payload["items"][0]["ok"] is True
                    assert deref_payload["items"][1]["ok"] is False
                    assert deref_payload["items"][1]["error"]["code"] == "not_found"
                    archived = await session.call_tool(
                        "archive_document",
                        {"doc_key": "mcpns/many-a", "reason": "MCP cleanup"},
                    )
                    assert archived.isError is False, archived.content[0].text
                    assert json.loads(archived.content[0].text)["document"][
                        "archived_at"
                    ]
                    active = await session.call_tool(
                        "list_documents",
                        {"namespace": "mcpns", "limit": 100},
                    )
                    assert "mcpns/many-a" not in {
                        item["doc_key"]
                        for item in json.loads(active.content[0].text)["items"]
                    }
                    with_archived = await session.call_tool(
                        "list_documents",
                        {
                            "namespace": "mcpns",
                            "limit": 100,
                            "include_archived": True,
                        },
                    )
                    assert "mcpns/many-a" in {
                        item["doc_key"]
                        for item in json.loads(with_archived.content[0].text)["items"]
                    }
                    restored = await session.call_tool(
                        "restore_document", {"doc_key": "mcpns/many-a"}
                    )
                    assert restored.isError is False
                    assert (
                        json.loads(restored.content[0].text)["document"]["archived_at"]
                        is None
                    )
                    incremental = await session.call_tool(
                        "workspace_sync",
                        {"since": sync_payload["next_cursor"]},
                    )
                    assert incremental.isError is False
                    incremental_payload = json.loads(incremental.content[0].text)
                    assert incremental_payload["incremental"] is True
                    assert "mcpns/many-a" in {
                        item["doc_key"]
                        for item in incremental_payload["documents"]
                    }
                    assert incremental_payload["next_cursor"]
                    docs_tool = await session.call_tool("get_api_docs", {})
                    assert docs_tool.isError is False
                    docs_payload = json.loads(docs_tool.content[0].text)
                    assert docs_payload["batch_limits"]["upsert_many"] == 25
                    assert "DocumentUpsertRequest" in docs_payload["schemas"]
    timeline = await client.get(
        "/v1/timeline",
        headers=commander,
        params={"event_type": "HUMAN_REVIEW_REQUESTED", "namespace": "mcpns"},
    )
    assert timeline.status_code == 200
    review_events = timeline.json()["items"]
    assert review_events
    assert review_events[0]["platform"] == "mcp"
    assert review_events[0]["namespace"] == "mcpns"
    assert review_events[0]["payload"]["proposal_id"] == proposal_id


async def test_quotas_are_enforced(client, admin_headers):
    created = await client.post(
        "/v1/admin/workspaces",
        headers=admin_headers,
        json={"name": f"free-{uuid.uuid4().hex[:6]}", "tier": "free"},
    )
    assert created.status_code == 201
    headers = {"Authorization": f"Bearer {created.json()['api_key']}"}
    first_doc = await client.put(
        "/v1/documents/free:one",
        headers=headers,
        json={"content": "one", "frontmatter": {}, "expected_version": 0},
    )
    assert first_doc.status_code == 200
    second_doc = await client.put(
        "/v1/documents/free:two",
        headers=headers,
        json={"content": "two", "frontmatter": {}, "expected_version": 0},
    )
    assert second_doc.status_code == 402
    assert second_doc.json()["error"]["code"] == "quota_exceeded"
    first_slot = await client.put(
        "/v1/slots/free:one",
        headers=headers,
        json={"state": {}, "expected_version": 0},
    )
    assert first_slot.status_code == 200
    second_slot = await client.put(
        "/v1/slots/free:two",
        headers=headers,
        json={"state": {}, "expected_version": 0},
    )
    assert second_slot.status_code == 402
    assert second_slot.json()["error"]["details"]["resource"] == "context_slots"


async def test_lease_reaper(client, admin_headers, workspace):
    commander = workspace["headers"]
    executor = await make_agent(client, admin_headers, workspace, "EXECUTOR")
    key = task_key("REAP")
    await create_task(client, commander, key, assigned_role="EXECUTOR")
    await claim(client, executor["headers"], key, lease=5)
    deadline = asyncio.get_running_loop().time() + 25
    while asyncio.get_running_loop().time() < deadline:
        current = await client.get(f"/v1/tasks/{key}", headers=commander)
        if current.json()["status"] == "READY":
            break
        await asyncio.sleep(1)
    else:
        raise AssertionError("task lease was not reaped")
    assert current.json()["claimed_by"] is None
    assert current.json()["lease_expires_at"] is None


async def test_dashboard_root_and_openapi(client):
    health = await client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["version"] == "3.2.1"
    assert health.headers["x-request-id"]

    root = await client.get("/")
    assert root.status_code == 200
    assert root.json()["version"] == "3.2.1"
    assert set(root.json()["primitives"]) == {
        "workspace_sync",
        "create_task",
        "review_task",
        "request_human_review",
        "agent_claim_and_read",
        "agent_patch_shared_state",
        "agent_handoff_task",
        "agent_escalate_to_human",
        "upsert_shared_doc",
        "upsert_many",
        "patch_doc_section",
        "query_workspace",
        "list_documents",
        "dereference",
        "dereference_many",
        "archive_document",
        "restore_document",
        "rename_document",
        "register_namespace",
        "list_namespaces",
        "get_token",
        "get_lineage",
        "list_token_tree",
        "issue_child_token",
        "revoke_token",
        "renew_token",
        "register_agent",
        "get_agent",
        "list_agents",
        "update_agent",
        "set_status",
        "list_tasks",
        "write_continuation",
        "create_subtasks",
        "get_task_tree",
        "clear_taint",
        "recall_document",
        "get_recall",
        "get_budget",
        "set_budget",
        "list_budgets",
        "explain_write",
        "register_parts",
        "get_orientation",
        "ack_constitution",
        "get_usage",
        "get_index_status",
        "heartbeat",
        "release_lease",
        "list_leases",
        "export_workspace",
        "get_export",
        "list_exports",
        "poll_events",
        "subscribe",
        "list_subscriptions",
        "unsubscribe",
        "propose_document_change",
        "vote_on_proposal",
        "list_document_proposals",
        "draft_external_action",
        "get_api_docs",
    }
    openapi = await client.get("/openapi.json")
    assert openapi.status_code == 200
    paths = openapi.json()["paths"]
    for path in (
        "/v1/tasks",
        "/v1/tasks/{task_key}/claim",
        "/v1/tasks/{task_key}/handoff",
        "/v1/slots/{slot_key}/patch",
        "/v1/documents/{doc_key}",
        "/v1/query",
        "/v1/stream",
    ):
        assert path in paths

    from pathlib import Path

    out = Path(__file__).resolve().parent.parent / "dashboard" / "out" / "index.html"
    if out.exists():
        dashboard = await client.get("/dashboard/")
        assert dashboard.status_code == 200
        assert "AgentGraph Console" in dashboard.text
