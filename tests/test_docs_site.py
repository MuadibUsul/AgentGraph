"""Public docs site, operator API reference and the console checklist."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from app import docs_site

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parent.parent
GENERATED = ROOT / "dashboard" / "app" / "docs" / "site.generated.json"
ALLOWED_CONSTANTS = {"AGENTGRAPH_AUTH", "AGENTGRAPH_TOKEN", "AGENTGRAPH_URL",
                     "HUMAN_SOVEREIGNTY_LOCKED", "IN_PROGRESS", "NAMESPACE_FORBIDDEN",
                     "AGENT_CLAIM"}


def test_generated_site_is_current_and_clean():
    content = docs_site.dumps(docs_site.build())
    assert GENERATED.read_text(encoding="utf-8") == content, (
        "run scripts/gen_docs_site.py"
    )
    assert "/v1/admin" not in content
    assert "api_key_hash" not in content and "password_hash" not in content
    assert not re.search(r"\bsk-[A-Za-z0-9]{8,}|\bjina_[A-Za-z0-9]{8,}|postgres(ql)?(\+\w+)?://",
                         content)
    # Only example placeholders and protocol constants look like settings.
    constants = set(re.findall(r"\b[A-Z][A-Z0-9]+_[A-Z0-9_]{2,}\b", content))
    assert constants <= ALLOWED_CONSTANTS, constants - ALLOWED_CONSTANTS
    site = json.loads(content)
    for page in site["pages"].values():
        for block in page["blocks"]:
            for value in _texts(block):
                assert set(value) >= {"en", "zh"} and value["en"] and value["zh"], block


def _texts(block):
    for key in ("text", "label"):
        if isinstance(block.get(key), dict):
            yield block[key]
    yield from block.get("items", [])
    yield from block.get("columns", [])
    for row in block.get("rows", []):
        yield from row


def test_api_reference_comes_from_openapi_without_admin():
    from app.main import app

    openapi = app.openapi()
    groups = docs_site.api_reference(openapi)
    paths = {endpoint["path"] for group in groups for endpoint in group["endpoints"]}
    assert "/v1/orientation" in paths and "/v1/events" in paths
    assert not any(path.startswith("/v1/admin") for path in paths)
    expected = {path for path in openapi["paths"]
                if path.startswith("/v1") and not path.startswith("/v1/admin")}
    assert paths == expected


async def test_docs_are_public_and_swagger_moved(client):
    root = await client.get("/docs", follow_redirects=False)
    assert root.status_code in (307, 308)
    # Served by the Next.js export when the console is built, otherwise by
    # the plain-HTML fallback; both carry the page title in English.
    index = await client.get("/docs/")
    assert index.status_code == 200 and "text/html" in index.headers["content-type"]
    assert "AgentGraph Docs" in index.text and "/v1/admin" not in index.text
    quick = await client.get("/docs/quickstart/")
    assert quick.status_code == 200 and "Quickstart" in quick.text
    for header in ("content-security-policy", "x-frame-options", "x-content-type-options",
                   "referrer-policy"):
        assert header in quick.headers
    assert "no-store" in quick.headers["cache-control"]
    assert (await client.get("/docs/nope/")).status_code == 404
    # Development settings: Swagger UI is open at its new path.
    assert (await client.get("/api-reference")).status_code == 200


def test_fallback_pages_render_both_languages():
    from app.api.public_docs import _render

    for slug in docs_site.build()["pages"]:
        english = _render(slug, "en")
        chinese = _render(slug, "zh")
        assert 'lang="en"' in english and 'lang="zh-CN"' in chinese
        assert "/v1/admin" not in english + chinese
    assert "快速开始" in _render("quickstart", "zh")
    assert "Agent 快速开始" in _render("agent-quickstart", "zh")


async def test_operator_reference_needs_basic_auth(enforce_client):
    assert (await enforce_client.get("/docs/")).status_code == 200
    denied = await enforce_client.get("/api-reference")
    assert denied.status_code == 401 and "Basic" in denied.headers["www-authenticate"]
    assert (await enforce_client.get("/openapi.json")).status_code == 401
    allowed = await enforce_client.get("/api-reference", auth=("operator", "test-admin"))
    assert allowed.status_code == 200 and "swagger" in allowed.text.lower()
    spec = await enforce_client.get("/openapi.json", auth=("operator", "test-admin"))
    assert spec.status_code == 200


async def _agent(client, admin_headers, workspace):
    signature = f"docs_{uuid.uuid4().hex[:6]}"
    response = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        headers=admin_headers,
        json={"agent_signature": signature, "role_scope": "EXECUTOR"},
    )
    return response.json()["api_key"], signature


async def test_published_curl_examples_run(client, admin_headers, workspace):
    key, _ = await _agent(client, admin_headers, workspace)
    headers = {"Authorization": f"Bearer {key}"}
    for example in docs_site.EXAMPLES:
        response = await client.request(
            example["method"], example["path"], headers=headers,
            params=example.get("query"), json=example.get("body"),
        )
        assert response.status_code == example["expect"], (example["id"], response.text)
        assert example["path"] in docs_site.curl(example)


async def test_published_mcp_example_calls_get_orientation(
    client, admin_headers, workspace, api_base_url, tmp_path
):
    key, signature = await _agent(client, admin_headers, workspace)
    configs = docs_site.mcp_configs()
    script = tmp_path / "orientation.py"
    script.write_text(configs["python"], encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script)],
        env={**os.environ, "AGENTGRAPH_URL": api_base_url, "AGENTGRAPH_TOKEN": key},
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert f"connected as {signature}" in result.stdout
    cursor = json.loads(configs["cursor"])["mcpServers"]["agentgraph"]
    claude = json.loads(configs["claude_desktop"])["mcpServers"]["agentgraph"]
    assert cursor["url"] == configs["endpoint"] and cursor["url"].endswith("/mcp/")
    assert cursor["headers"]["Authorization"].startswith("Bearer ")
    assert configs["endpoint"] in claude["args"]
    assert claude["env"]["AGENTGRAPH_AUTH"].startswith("Bearer ")


async def test_new_owner_completes_the_checklist(client):
    email = f"onboard-{uuid.uuid4().hex[:8]}@example.test"
    registered = await client.post("/v1/auth/register", json={
        "email": email, "password": "correct-horse-battery-staple",
        "display_name": "New Owner", "workspace_name": "Onboarding Workspace",
    })
    assert registered.status_code == 201, registered.text
    owner = {"Authorization": f"Bearer {registered.json()['token']}"}
    status = (await client.get("/v1/human/onboarding", headers=owner)).json()
    assert status["visible"] is True and status["completed"] == 0
    assert [step["id"] for step in status["steps"]] == [
        "learn_workspace", "create_token", "connect_mcp", "first_content",
        "human_review", "view_audit",
    ]
    assert all(step["doc"].startswith("/docs/") for step in status["steps"])

    minted = await client.post("/v1/human/agent-tokens", headers=owner, json={
        "agent_signature": "first_agent", "role_scope": "EXECUTOR", "allowed_namespaces": ["*"],
    })
    assert minted.status_code in (200, 201), minted.text
    agent = {"Authorization": f"Bearer {minted.json()['api_key']}"}
    assert (await client.get("/v1/orientation", headers=agent)).status_code == 200
    written = await client.put("/v1/documents/notes/first", headers=agent,
                               json={"content": "# First", "expected_version": 0})
    assert written.status_code == 200
    verified = await client.post("/v1/human/documents/notes/first/verify", headers=owner,
                                 json={"expected_version": 1})
    assert verified.status_code == 200, verified.text
    status = (await client.get("/v1/human/onboarding", headers=owner)).json()
    done = {step["id"] for step in status["steps"] if step["done"]}
    assert done == {"create_token", "connect_mcp", "first_content", "human_review"}

    await client.post("/v1/human/onboarding", headers=owner,
                      json={"action": "complete", "step": "learn_workspace"})
    skipped = (await client.post("/v1/human/onboarding", headers=owner,
                                 json={"action": "skip", "step": "view_audit"})).json()
    assert skipped["all_done"] is True and skipped["visible"] is False
    again = (await client.post("/v1/human/onboarding", headers=owner,
                               json={"action": "complete", "step": "learn_workspace"})).json()
    assert again["completed"] == 5  # idempotent
    closed = (await client.post("/v1/human/onboarding", headers=owner,
                                json={"action": "reopen"})).json()
    assert closed["dismissed"] is False
    bad = await client.post("/v1/human/onboarding", headers=owner,
                            json={"action": "complete", "step": "nope"})
    assert bad.status_code == 422
    # The checklist belongs to people: agent tokens cannot read it.
    assert (await client.get("/v1/human/onboarding", headers=agent)).status_code in (401, 403)


async def test_checklist_can_be_closed_without_finishing(client):
    registered = await client.post("/v1/auth/register", json={
        "email": f"close-{uuid.uuid4().hex[:8]}@example.test",
        "password": "correct-horse-battery-staple",
        "display_name": "Closer", "workspace_name": "Close Workspace",
    })
    owner = {"Authorization": f"Bearer {registered.json()['token']}"}
    closed = (await client.post("/v1/human/onboarding", headers=owner,
                                json={"action": "dismiss"})).json()
    assert closed["visible"] is False and closed["dismissed"] is True
    assert (await client.get("/v1/human/onboarding", headers=owner)).json()["visible"] is False
