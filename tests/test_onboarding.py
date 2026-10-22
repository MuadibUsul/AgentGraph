"""Agent onboarding acceptance (PRD AG-OB-01, AG-OB-02, AG-OB-03)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parent.parent
TOP_LEVEL = {
    "schema_version", "generated_at", "server", "me", "constitution", "namespace_map",
    "collaborators", "my_tasks", "claimable_tasks", "required_reading", "inbox",
    "conventions", "next_calls", "warnings",
}


def test_generated_agent_guide_is_current():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_agent_guide.py"), "--check"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _db() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def _agent(client, admin_headers, workspace, role="EXECUTOR", namespaces=None):
    response = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        headers=admin_headers,
        json={"agent_signature": f"{role.lower()}_{uuid.uuid4().hex[:6]}", "role_scope": role,
              "allowed_namespaces": namespaces or ["*"]},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['api_key']}"}, response.json()["token"]


async def _owner(client, admin_headers, workspace):
    email = f"ob-{uuid.uuid4().hex[:8]}@example.test"
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


async def _mcp(api_base_url, headers, calls):
    results = []
    async with httpx.AsyncClient(base_url=api_base_url, headers=headers, timeout=60) as http:
        async with streamable_http_client(f"{api_base_url}/mcp/", http_client=http) as (
            read, write, _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for name, args in calls:
                    if name == "__resources__":
                        results.append(await session.list_resources())
                    else:
                        results.append(await session.call_tool(name, args))
    return results


# ------------------------------------------------------------- AG-OB-02


@pytest.mark.integration
async def test_new_workspace_has_a_draft_constitution(client, workspace):
    constitution = await client.get(
        "/v1/documents/system/constitution", headers=workspace["headers"]
    )
    assert constitution.status_code == 200, constitution.text
    body = constitution.json()
    assert body["namespace"] == "system" and body["verification_status"] == "DRAFT"
    assert body["content"].count("\n## ") == 6


@pytest.mark.integration
async def test_agents_cannot_write_system_but_can_propose(client, admin_headers, workspace):
    agent, _ = await _agent(client, admin_headers, workspace)
    direct = await client.put(
        "/v1/documents/system/constitution",
        headers=agent,
        json={"content": "takeover", "expected_version": 1},
    )
    assert direct.status_code == 403
    assert direct.json()["error"]["code"] == "system_namespace_protected"
    assert direct.json()["error"]["class"] == "fix_request"
    proposal = await client.post(
        "/v1/documents/system/constitution/proposals",
        headers=agent,
        json={"content": "## 空间用途\n\nResearch hub", "base_version": 1,
              "note": "fill purpose"},
    )
    assert proposal.status_code in (200, 201), proposal.text


@pytest.mark.integration
async def test_first_write_to_new_namespace_creates_readme_stub(client, workspace):
    namespace = f"research-{uuid.uuid4().hex[:6]}"
    written = await client.put(
        f"/v1/documents/r/{uuid.uuid4().hex[:6]}",
        headers=workspace["headers"],
        json={"content": "x", "expected_version": 0, "namespace": namespace},
    )
    assert written.status_code == 200, written.text
    codes = [w["code"] for w in written.json()["warnings"]]
    assert "namespace_undocumented" in codes
    readme = await client.get(
        f"/v1/documents/system/namespaces/{namespace}", headers=workspace["headers"]
    )
    assert readme.status_code == 200
    assert readme.json()["verification_status"] == "DRAFT"
    assert readme.json()["frontmatter"]["kind"] == "namespace_readme"


@pytest.mark.integration
async def test_required_frontmatter_warns_then_strict_rejects(client, admin_headers, workspace):
    owner = await _owner(client, admin_headers, workspace)
    namespace = f"rf-{uuid.uuid4().hex[:6]}"
    await client.post("/v1/namespaces", headers=workspace["headers"],
                      json={"namespace": namespace, "purpose": "test"})
    readme = await client.get(
        f"/v1/documents/system/namespaces/{namespace}", headers=workspace["headers"]
    )
    frontmatter = {**readme.json()["frontmatter"], "required_frontmatter": ["owner"],
                   "stub": False}
    updated = await client.put(
        f"/v1/documents/system/namespaces/{namespace}",
        headers=owner,
        json={"content": "documented", "frontmatter": frontmatter,
              "expected_version": readme.json()["version"]},
    )
    assert updated.status_code == 200, updated.text
    soft = await client.put(
        f"/v1/documents/{namespace}/a", headers=workspace["headers"],
        json={"content": "x", "expected_version": 0, "namespace": namespace},
    )
    assert soft.status_code == 200
    assert [w["code"] for w in soft.json()["warnings"]] == ["frontmatter_missing"]
    strict = await client.put(
        f"/v1/documents/system/namespaces/{namespace}",
        headers=owner,
        json={"content": "documented", "frontmatter": {**frontmatter, "strict": True},
              "expected_version": updated.json()["document"]["version"]},
    )
    assert strict.status_code == 200
    rejected = await client.put(
        f"/v1/documents/{namespace}/b", headers=workspace["headers"],
        json={"content": "x", "expected_version": 0, "namespace": namespace},
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["details"]["missing"] == ["owner"]


@pytest.mark.integration
async def test_backfill_stub_counts_match_the_census(client, workspace):
    namespace = f"legacy-{uuid.uuid4().hex[:6]}"
    for index in range(3):
        await client.put(
            f"/v1/documents/{namespace}/doc-{index}", headers=workspace["headers"],
            json={"content": "x", "expected_version": 0, "namespace": namespace},
        )
    conn = await asyncpg.connect(_db())
    try:
        await conn.execute(
            "DELETE FROM shared_documents WHERE workspace_id = $1::uuid "
            "AND namespace = 'system'",
            workspace["workspace"]["id"],
        )
    finally:
        await conn.close()
    from app.database import async_session_factory
    from app.services.onboarding import backfill_all_workspaces

    async with async_session_factory() as session:
        assert await backfill_all_workspaces(session) >= 1
    readme = await client.get(
        f"/v1/documents/system/namespaces/{namespace}", headers=workspace["headers"]
    )
    census = await client.get(
        f"/v1/documents?namespace={namespace}&limit=500", headers=workspace["headers"]
    )
    assert readme.json()["frontmatter"]["generated"]["documents"] == census.json()["total"] == 3
    assert len(readme.json()["frontmatter"]["generated"]["sample_doc_keys"]) == 3


# ------------------------------------------------------------- AG-OB-03


@pytest.mark.integration
async def test_agent_docs_and_openapi_are_token_readable(client, workspace, api_base_url):
    markdown = await client.get(
        "/v1/agent-docs?format=markdown", headers=workspace["headers"]
    )
    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    for needle in ("list_documents", "include_own_unverified", "Retry-After"):
        assert needle in markdown.text
    assert (await client.get("/v1/agent-docs")).status_code == 401
    openapi = await client.get("/v1/openapi.json", headers=workspace["headers"])
    assert openapi.status_code == 200
    assert not [path for path in openapi.json()["paths"] if "/admin" in path]
    (resources,) = await _mcp(api_base_url, workspace["headers"], [("__resources__", {})])
    uris = {str(resource.uri) for resource in resources.resources}
    assert {"agentgraph://docs/agent-guide", "agentgraph://docs/openapi",
            "agentgraph://docs/errors"} <= uris


# ------------------------------------------------------------- AG-OB-01


@pytest.mark.integration
async def test_orientation_first_call_and_next_calls(
    client, admin_headers, workspace, api_base_url
):
    agent, _ = await _agent(client, admin_headers, workspace)
    first = await client.get("/v1/orientation", headers=agent)
    assert first.status_code == 200, first.text
    pack = first.json()
    assert TOP_LEVEL <= set(pack)
    assert pack["required_reading"][0]["doc_key"] == "system/constitution"
    assert pack["next_calls"]
    assert {"code": "not_registered"}.items() <= pack["warnings"][0].items()

    calls = [(call["tool"], call["args"]) for call in pack["next_calls"]]
    calls.append(("upsert_shared_doc", {"doc_key": f"notes/{uuid.uuid4().hex[:6]}",
                                        "content": "first note", "expected_version": 0}))
    results = await _mcp(api_base_url, agent, calls)
    for (name, _), result in zip(calls, results, strict=True):
        assert result.isError is False, (name, result.content[0].text)
    again = await client.get("/v1/orientation", headers=agent)
    assert again.json()["constitution"]["ack_required"] is False

    root = await client.get("/")
    assert root.json()["first_call"] == "get_orientation"


@pytest.mark.integration
async def test_orientation_truncates_to_max_bytes(client, admin_headers, workspace):
    owner = await _owner(client, admin_headers, workspace)
    current = await client.get("/v1/documents/system/constitution", headers=owner)
    big = "## 空间用途\n\n" + ("规则 " * 14_000)
    assert len(big.encode("utf-8")) > 40_000
    updated = await client.put(
        "/v1/documents/system/constitution", headers=owner,
        json={"content": big, "expected_version": current.json()["version"]},
    )
    assert updated.status_code == 200, updated.text
    agent, _ = await _agent(client, admin_headers, workspace)
    response = await client.get("/v1/orientation?max_bytes=32768", headers=agent)
    assert len(response.content) <= 32_768
    constitution = response.json()["constitution"]
    assert constitution["truncated"] is True
    assert constitution["doc_key"] == "system/constitution" and constitution["version"] >= 2


@pytest.mark.integration
async def test_orientation_lists_my_tasks_and_namespace_access(client, admin_headers, workspace):
    agent, token = await _agent(client, admin_headers, workspace)
    for index in range(2):
        key = f"MINE-{index}-{uuid.uuid4().hex[:4]}"
        await client.post("/v1/tasks", headers=workspace["headers"], json={
            "task_key": key, "title": "mine", "assigned_role": "EXECUTOR",
            "input_context": {"doc_key": "system/constitution", "done_definition": "done",
                              "output_to": "notes/out"},
        })
        claimed = await client.post(f"/v1/tasks/{key}/claim", headers=agent, json={})
        assert claimed.status_code == 200, claimed.text
    pack = (await client.get("/v1/orientation", headers=agent)).json()
    assert len(pack["my_tasks"]) == 2
    assert pack["my_tasks"][0]["done_definition"] == "done"
    assert pack["my_tasks"][0]["context_docs"] == ["system/constitution"]

    await client.put("/v1/documents/goals/q", headers=workspace["headers"],
                     json={"content": "x", "expected_version": 0, "namespace": "goals"})
    scoped, _ = await _agent(client, admin_headers, workspace, namespaces=["ideas"])
    narrow = (await client.get("/v1/orientation", headers=scoped)).json()
    goals = next(entry for entry in narrow["namespace_map"] if entry["namespace"] == "goals")
    assert goals["my_access"] == "none" and "documents" not in goals
    assert json.dumps(narrow).count("goals/q") == 0
