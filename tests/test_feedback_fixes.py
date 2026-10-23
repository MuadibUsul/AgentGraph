"""Fixes from the 2026-10-22 field retest: slots created by patch (MCP-only
agents), rename across a namespace prefix, and MCP parity for both."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}


async def _workspace(client: httpx.AsyncClient) -> dict:
    created = await client.post("/v1/admin/workspaces", headers=ADMIN,
                                json={"name": f"fix-{uuid.uuid4().hex[:6]}", "tier": "pro"})
    body = created.json()
    return {"id": body["workspace"]["id"], "key": {"Authorization": f"Bearer {body['api_key']}"}}


async def _executor(client: httpx.AsyncClient, ws: dict) -> dict:
    minted = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
        json={"agent_signature": f"exec-{uuid.uuid4().hex[:6]}", "role_scope": "EXECUTOR"})
    assert minted.status_code in (200, 201), minted.text
    return {"Authorization": f"Bearer {minted.json()['api_key']}"}


async def test_patch_with_version_zero_creates_a_slot(client):
    ws = await _workspace(client)
    agent = await _executor(client, ws)
    key = f"grid/{uuid.uuid4().hex[:6]}"
    created = await client.post(f"/v1/slots/{key}/patch", headers=agent, json={
        "ops": [{"op": "add", "path": "/status", "value": "open"}], "expected_version": 0})
    assert created.status_code == 200, created.text
    assert created.json()["slot"]["version"] == 1
    assert created.json()["slot"]["current_state"] == {"status": "open"}
    again = await client.post(f"/v1/slots/{key}/patch", headers=agent, json={
        "ops": [{"op": "add", "path": "/x", "value": 1}], "expected_version": 0})
    assert again.status_code == 409
    missing = await client.post(f"/v1/slots/grid/nope-{uuid.uuid4().hex[:4]}/patch",
                                headers=agent, json={
                                    "ops": [{"op": "add", "path": "/x", "value": 1}],
                                    "expected_version": 1})
    assert missing.status_code == 404


async def test_rename_across_a_namespace_prefix_moves_the_document(client):
    ws = await _workspace(client)
    for namespace in ("docs", "ancs"):
        registered = await client.post("/v1/namespaces", headers=ws["key"], json={
            "namespace": namespace, "purpose": "test"})
        assert registered.status_code in (200, 201), registered.text
    wrote = await client.put("/v1/documents/docs/plan", headers=ws["key"], json={
        "content": "plan", "expected_version": 0, "namespace": "docs"})
    assert wrote.status_code in (200, 201), wrote.text
    assert wrote.json()["document"]["namespace"] == "docs"

    moved = await client.post("/v1/documents/docs/plan/rename", headers=ws["key"],
                              json={"new_alias": "ancs/plan"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["document"]["namespace"] == "ancs"
    listed = await client.get("/v1/documents", headers=ws["key"],
                              params={"namespace": "ancs", "include_unverified": True})
    assert "ancs/plan" in [item["doc_key"] for item in listed.json()["items"]]

    kept = await client.post("/v1/documents/ancs/plan/rename", headers=ws["key"],
                             json={"new_alias": "unregistered/plan"})
    assert kept.status_code == 200, kept.text
    assert kept.json()["document"]["namespace"] == "ancs"
    codes = {item["code"] for item in kept.json().get("warnings") or []}
    assert "namespace_unchanged" in codes


async def test_mcp_creates_a_slot_with_version_zero(client, api_base_url):
    ws = await _workspace(client)
    agent = await _executor(client, ws)
    async with httpx.AsyncClient(base_url=api_base_url, headers=agent, timeout=60) as http, \
            streamable_http_client(f"{api_base_url}/mcp/", http_client=http) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("agent_patch_shared_state", {
                "slot_key": f"grid/mcp-{uuid.uuid4().hex[:6]}",
                "ops": [{"op": "add", "path": "/owner", "value": "mcp"}],
                "expected_version": 0})
            assert result.isError is False, result.content
            payload = result.structuredContent or json.loads(result.content[0].text)
            assert payload["slot"]["current_state"] == {"owner": "mcp"}
