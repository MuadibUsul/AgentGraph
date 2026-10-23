"""Lower agent call cost (2026-10-23 proposal): one request for several tool
calls, and slim default detail levels on MCP list / sync / orientation."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}


def _payload(result) -> dict:
    return result.structuredContent or json.loads(result.content[0].text)


async def _seeded(client: httpx.AsyncClient) -> dict:
    created = (await client.post("/v1/admin/workspaces", headers=ADMIN, json={
        "name": f"cost-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
    key = {"Authorization": f"Bearer {created['api_key']}"}
    for index in range(6):
        response = await client.post("/v1/tasks", headers=key, json={
            "task_key": f"COST-{index}-{uuid.uuid4().hex[:4]}", "title": f"task {index}",
            "assigned_role": "EXECUTOR", "description": "a fairly long description " * 20,
            "input_context": {"notes": "x" * 300}})
        assert response.status_code == 201, response.text
    for index in range(60):
        await client.post("/v1/timeline", headers=key, json={
            "event_type": "progress", "payload": {"step": index, "note": "y" * 250}})
    return {"key": key}


async def test_batch_and_slim_defaults(client, api_base_url):
    ws = await _seeded(client)
    async with httpx.AsyncClient(base_url=api_base_url, headers=ws["key"], timeout=60) as http, \
            streamable_http_client(f"{api_base_url}/mcp/", http_client=http) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            batch = _payload(await session.call_tool("batch", {"calls": [
                {"tool": "list_tasks", "arguments": {}, "id": "tasks"},
                {"tool": "list_subscriptions", "arguments": {}},
                {"tool": "get_index_status", "arguments": {}},
                {"tool": "list_tasks", "arguments": {"limit": 0}, "id": "bad"},
                {"tool": "no_such_tool", "arguments": {}},
            ]}))
            assert batch["count"] == 5 and batch["failed"] == 2
            results = {item["id"]: item for item in batch["results"]}
            assert results["tasks"]["ok"] and len(results["tasks"]["result"]["items"]) == 6
            assert results[1]["ok"] and results[2]["ok"]
            assert results["bad"]["error"]["code"] == "validation_error"
            assert results[4]["error"]["code"] == "unknown_tool"

            slim = await session.call_tool("workspace_sync", {})
            full = await session.call_tool("workspace_sync", {"detail": "full"})
            slim_body, full_body = _payload(slim), _payload(full)
            assert set(full_body) <= set(slim_body)  # same structure, only slimmer
            assert len(slim.content[0].text) <= 0.3 * len(full.content[0].text), (
                len(slim.content[0].text), len(full.content[0].text))
            assert len(slim_body["timeline"]) == 20 and slim_body["timeline_truncated"]

            tasks = _payload(await session.call_tool("list_tasks", {}))
            assert "lease_epoch" not in tasks["items"][0]
            assert {"task_key", "title", "status", "version"} <= set(tasks["items"][0])
            every = _payload(await session.call_tool("list_tasks", {"detail": "full"}))
            assert "lease_epoch" in every["items"][0]

    rest = await client.get("/v1/sync", headers=ws["key"], params={"detail": "summary"})
    assert rest.status_code == 200 and rest.json()["detail"] == "summary"
    unchanged = await client.get("/v1/tasks", headers=ws["key"])
    assert "lease_epoch" in unchanged.json()["items"][0]  # REST keeps the full shape
