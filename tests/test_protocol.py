"""Failure-semantics protocol acceptance (PRD AG-ST-18)."""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


def _doc_body(content: str, expected_version: int = 0) -> dict:
    return {"content": content, "expected_version": expected_version}


async def test_same_key_same_body_replays_without_second_write(client, workspace):
    doc_key = f"idem/{uuid.uuid4().hex[:8]}"
    headers = {**workspace["headers"], "Idempotency-Key": uuid.uuid4().hex}
    responses = [
        await client.put(f"/v1/documents/{doc_key}", headers=headers, json=_doc_body("one"))
        for _ in range(3)
    ]
    assert [r.status_code for r in responses] == [200, 200, 200], responses[0].text
    first = responses[0].json()
    assert "replayed" not in first
    for replay in responses[1:]:
        body = replay.json()
        assert body.pop("replayed") is True
        assert body == first
        assert replay.headers["Idempotent-Replayed"] == "true"
    versions = await client.get(
        f"/v1/documents/{doc_key}/versions", headers=workspace["headers"]
    )
    assert versions.status_code == 200, versions.text
    assert len(versions.json()["versions"]) == 1


async def test_same_key_different_body_is_rejected(client, workspace):
    doc_key = f"idem/{uuid.uuid4().hex[:8]}"
    headers = {**workspace["headers"], "Idempotency-Key": uuid.uuid4().hex}
    first = await client.put(
        f"/v1/documents/{doc_key}", headers=headers, json=_doc_body("one")
    )
    assert first.status_code == 200, first.text
    reused = await client.put(
        f"/v1/documents/{doc_key}", headers=headers, json=_doc_body("two", 1)
    )
    assert reused.status_code == 409, reused.text
    error = reused.json()["error"]
    assert error["code"] == "idempotency_key_reused"
    assert error["class"] == "fix_request"
    assert "original_request_hash" in error["details"]
    current = await client.get(f"/v1/documents/{doc_key}", headers=workspace["headers"])
    assert current.json()["content"] == "one"


async def test_keys_are_scoped_to_the_credential(client, admin_headers, workspace):
    response = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        headers=admin_headers,
        json={"agent_signature": f"idem_{uuid.uuid4().hex[:6]}", "role_scope": "EXECUTOR"},
    )
    agent = {"Authorization": f"Bearer {response.json()['api_key']}"}
    key = uuid.uuid4().hex
    first_key = f"idem/{uuid.uuid4().hex[:8]}"
    a = await client.put(
        f"/v1/documents/{first_key}",
        headers={**workspace["headers"], "Idempotency-Key": key},
        json=_doc_body("commander"),
    )
    b = await client.put(
        f"/v1/documents/{first_key}",
        headers={**agent, "Idempotency-Key": key},
        json=_doc_body("agent"),
    )
    assert a.status_code == 200
    # A different principal never sees the other's record: it runs for real.
    assert b.status_code == 409
    assert b.json()["error"]["code"] == "version_conflict"


async def test_errors_carry_class_and_action(client, workspace):
    missing = await client.get(
        f"/v1/documents/missing-{uuid.uuid4().hex}", headers=workspace["headers"]
    )
    error = missing.json()["error"]
    assert (error["code"], error["class"], error["retryable"]) == ("not_found", "abandon", False)
    invalid = await client.put(
        "/v1/documents/x", headers=workspace["headers"], json={"content": 5}
    )
    assert invalid.json()["error"]["class"] == "fix_request"
    unauthenticated = await client.get("/v1/whoami")
    assert unauthenticated.json()["error"]["class"] == "reauth"


async def test_version_negotiation(client, workspace):
    plain = await client.get("/v1/whoami", headers=workspace["headers"])
    assert plain.headers["AgentGraph-Version"] == "2026-10-21"
    declared = await client.get(
        "/v1/whoami", headers={**workspace["headers"], "AgentGraph-Version": "2026-11"}
    )
    assert declared.status_code == 200
    assert declared.headers["AgentGraph-Version"] == "2026-11"
    rejected = await client.get(
        "/v1/whoami", headers={**workspace["headers"], "AgentGraph-Version": "1999-01-01"}
    )
    assert rejected.status_code == 400
    error = rejected.json()["error"]
    assert error["code"] == "unsupported_api_version"
    assert "2026-11" in error["details"]["supported"]


async def test_agent_docs_publish_retry_policy(client, workspace):
    docs = await client.get("/v1/agent-docs", headers=workspace["headers"])
    conventions = docs.json()["conventions"]
    assert conventions["protocol"]["retry"]["max_attempts"] == 5
    codes = {row["code"] for row in conventions["error_codes"]}
    assert {"idempotency_key_reused", "unsupported_api_version"} <= codes


async def test_mcp_write_tools_replay_by_idempotency_key(api_base_url, client, workspace):
    import json

    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    doc_key = f"idem-mcp/{uuid.uuid4().hex[:8]}"
    key = uuid.uuid4().hex
    async with httpx.AsyncClient(
        base_url=api_base_url, headers=workspace["headers"], timeout=60
    ) as mcp_http:
        async with streamable_http_client(
            f"{api_base_url}/mcp/", http_client=mcp_http
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                upsert = next(t for t in tools.tools if t.name == "upsert_shared_doc")
                assert "idempotency_key" in upsert.inputSchema["properties"]
                args = {"doc_key": doc_key, "content": "mcp body", "idempotency_key": key}
                first = await session.call_tool("upsert_shared_doc", args)
                second = await session.call_tool("upsert_shared_doc", args)
                assert first.isError is False, first.content[0].text
                assert second.isError is False, second.content[0].text
                assert json.loads(second.content[0].text)["replayed"] is True
                reused = await session.call_tool(
                    "upsert_shared_doc", {**args, "content": "different"}
                )
                assert reused.isError is True
                assert reused.structuredContent["code"] == "idempotency_key_reused"
                assert reused.structuredContent["class"] == "fix_request"
    versions = await client.get(
        f"/v1/documents/{doc_key}/versions", headers=workspace["headers"]
    )
    assert len(versions.json()["versions"]) == 1
