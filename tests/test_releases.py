"""Release discipline, capability discovery and incremental orientation
(2026-10-23 retest, findings 1-3)."""

from __future__ import annotations

import json
import os
import uuid

import asyncpg
import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.config import get_settings
from tests.conftest import run_server

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}
settings = get_settings()


def _dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def _workspace(client: httpx.AsyncClient) -> dict:
    created = await client.post("/v1/admin/workspaces", headers=ADMIN,
                                json={"name": f"rel-{uuid.uuid4().hex[:6]}", "tier": "pro"})
    body = created.json()
    return {"id": body["workspace"]["id"], "key": {"Authorization": f"Bearer {body['api_key']}"}}


async def test_root_lists_release_capabilities_and_build_header(client):
    root = await client.get("/", headers={"Accept": "application/json"})
    body = root.json()
    assert body["version"] == settings.app_version
    assert body["release"]["version"] == settings.app_version
    assert body["changelog"]["doc_key"] == "system/changelog"
    flags = body["capabilities"]["feature_flags"]
    assert flags["child_token_issuance"] == {**flags["child_token_issuance"], "enabled": False}
    assert body["capabilities"]["rest_without_same_named_tool"] > 0
    assert body["api_versions"]["latest"] == "2026-11"
    assert root.headers["AgentGraph-Build"] == settings.app_version


async def test_switched_off_feature_says_so(client):
    created = await client.post("/v1/admin/workspaces", headers=ADMIN,
                                json={"name": "flag", "tier": "pro"})
    root_key = {"Authorization": f"Bearer {created.json()['root_key']}"}
    denied = await client.post("/v1/tokens", headers=root_key, json={
        "scopes": ["docs:read"], "purpose": "test",
        "agent": {"agent_id": f"a-{uuid.uuid4().hex[:8]}"}})
    assert denied.status_code == 403
    error = denied.json()["error"]
    assert error["code"] == "feature_disabled"
    assert error["details"]["setting"] == "CHILD_TOKEN_ISSUANCE"


async def test_get_api_docs_lists_capabilities_and_releases(client, api_base_url):
    ws = await _workspace(client)
    async with httpx.AsyncClient(base_url=api_base_url, headers=ws["key"], timeout=60) as http, \
            streamable_http_client(f"{api_base_url}/mcp/", http_client=http) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get_api_docs", {"section": "capabilities"})
            payload = result.structuredContent or json.loads(result.content[0].text)
            listed = payload["rest_without_same_named_tool"]
            assert listed and {"method", "path", "summary"} <= set(listed[0])
            assert "child_token_issuance" in payload["feature_flags"]
            history = await session.call_tool("get_api_docs", {"section": "releases"})
            releases = history.structuredContent or json.loads(history.content[0].text)
            assert releases["current"]["version"] == settings.app_version


async def test_a_new_release_is_announced_and_orientation_is_incremental(client):
    ws = await _workspace(client)
    await client.put("/v1/documents/rel/a", headers=ws["key"],
                     json={"content": "first", "expected_version": 0})
    first = await client.get("/v1/orientation", headers=ws["key"])
    assert first.status_code == 200, first.text
    cursor = first.json()["inbox"]["event_cursor"]
    full_size = len(first.content)

    # Pretend the deployment ran an older build with fewer primitives.
    conn = await asyncpg.connect(_dsn())
    try:
        current = await conn.fetchrow("SELECT * FROM platform_releases WHERE version = $1",
                                      settings.app_version)
        primitives = json.loads(current["primitives"])
        await conn.execute("DELETE FROM platform_releases")
        await conn.execute(
            "INSERT INTO platform_releases (version, schema_version, primitives, released_at) "
            "VALUES ('3.4.9', '3.0.0', $1::jsonb, NOW() - INTERVAL '1 day')",
            json.dumps([name for name in primitives if name != "get_digest"]))
    finally:
        await conn.close()

    async with run_server({}, ".server-release.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as fresh:
            polled = await fresh.get("/v1/events", headers=ws["key"], params={
                "cursor": cursor, "types": "platform.released"})
            assert polled.status_code == 200, polled.text
            events = polled.json()["events"]
            assert len(events) == 1
            details = events[0]["details"]
            assert details["from"] == "3.4.9" and details["to"] == settings.app_version
            assert details["added_primitives"] == ["get_digest"]
            changelog = await fresh.get("/v1/documents/system/changelog", headers=ws["key"],
                                        params={"include_unverified": True})
            assert changelog.status_code == 200, changelog.text
            assert settings.app_version in changelog.json()["content"]

            incremental = await fresh.get("/v1/orientation", headers=ws["key"],
                                          params={"since": cursor})
            assert incremental.status_code == 200, incremental.text
            body = incremental.json()
            assert body["incremental"]["since"] == cursor
            assert body["constitution"]["unchanged"] is True
            assert [item["version"] for item in body["platform_changes"]] == [
                settings.app_version]
            # A second session with nothing new in between: a fraction of the full pack.
            quiet = await fresh.get("/v1/orientation", headers=ws["key"], params={
                "since": body["inbox"]["event_cursor"]})
            assert quiet.json()["platform_changes"] == []
            assert len(quiet.content) <= full_size * 0.25, (len(quiet.content), full_size)

            by_version = await fresh.get("/v1/orientation", headers=ws["key"],
                                         params={"since": "3.4.9"})
            assert by_version.status_code == 200, by_version.text
            assert [item["version"] for item in by_version.json()["platform_changes"]] == [
                settings.app_version]
            bad = await fresh.get("/v1/orientation", headers=ws["key"],
                                  params={"since": "yesterday"})
            assert bad.status_code == 400
