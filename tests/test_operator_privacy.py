"""Operator privacy (v3.35): the operator sees counts and changes, never content."""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.integration


async def _tenant_of(workspace_id: str) -> str:
    conn = await asyncpg.connect(
        os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1))
    try:
        return await conn.fetchval("SELECT tenant_id FROM workspaces WHERE id = $1::uuid",
                                   workspace_id)
    finally:
        await conn.close()


async def _signup(client) -> tuple[dict[str, str], str]:
    registered = await client.post("/v1/auth/register", json={
        "email": f"private-{uuid.uuid4().hex[:8]}@example.test",
        "password": "correct-horse-battery-staple",
        "display_name": "Private Owner", "workspace_name": "Private Workspace",
    })
    assert registered.status_code == 201, registered.text
    owner = {"Authorization": f"Bearer {registered.json()['token']}"}
    workspace_id = (await client.get("/v1/auth/me", headers=owner)).json()["workspace"]["id"]
    return owner, workspace_id


async def test_operator_sees_counts_not_content_of_a_signup(client, admin_headers):
    owner, wid = await _signup(client)
    minted = await client.post("/v1/human/agent-tokens", headers=owner, json={
        "agent_signature": "writer", "role_scope": "EXECUTOR", "allowed_namespaces": ["*"]})
    agent = {"Authorization": f"Bearer {minted.json()['api_key']}"}
    written = await client.put("/v1/documents/notes/secret", headers=agent,
                               json={"content": "# Secret plan", "expected_version": 0})
    assert written.status_code == 200, written.text

    base = f"/v1/admin/workspaces/{wid}"
    for path in ("/stats", "/usage", "/budget", "/index-status", "/metrics"):
        allowed = await client.get(base + path, headers=admin_headers)
        assert allowed.status_code == 200, (path, allowed.text)
    stats = (await client.get(base + "/stats", headers=admin_headers)).json()
    assert "Secret plan" not in str(stats)

    for method, path in (
        ("GET", "/documents"), ("GET", "/documents/notes/secret"),
        ("GET", "/documents/notes/secret/versions"), ("GET", "/tasks"), ("GET", "/audit"),
        ("GET", "/timeline"), ("GET", "/slots"), ("GET", "/proposals"),
        ("GET", "/agent-tokens"), ("GET", "/human-users"), ("GET", "/alerts"),
        ("POST", "/agent-tokens"), ("POST", "/rotate-key"), ("POST", "/root-token:rotate"),
        ("POST", "/human-users"), ("GET", "/stream"),
    ):
        denied = await client.request(method, base + path, headers=admin_headers, json={})
        assert denied.status_code == 403, (method, path, denied.status_code)
        assert denied.json()["error"]["code"] == "operator_content_denied", (method, path)

    tenant = await _tenant_of(wid)
    export = await client.post(f"/v1/admin/tenants/{tenant}/exports", headers=admin_headers,
                               json={"scope": "tenant"})
    assert export.status_code == 403
    listed = (await client.get("/v1/admin/workspaces", headers=admin_headers)).json()
    mine = next(item for item in listed if item["id"] == wid)
    assert mine["private"] is True and mine["documents"] >= 1
    assert mine["support_access_until"] is None


async def test_owner_opens_and_closes_support_access(client, admin_headers):
    owner, wid = await _signup(client)
    base = f"/v1/admin/workspaces/{wid}"
    state = (await client.get("/v1/human/support-access", headers=owner)).json()
    assert state["private"] is True and state["active"] is None

    opened = await client.post("/v1/human/support-access", headers=owner,
                               json={"hours": 1, "note": "ticket 12"})
    assert opened.status_code == 200, opened.text
    assert opened.json()["active"]["allow_write"] is False
    read = await client.get(base + "/documents", headers=admin_headers)
    assert read.status_code == 200, read.text
    write = await client.post(base + "/tasks", headers=admin_headers,
                              json={"task_key": "x", "title": "x"})
    assert write.status_code == 403
    assert write.json()["error"]["code"] == "operator_write_denied"

    state = (await client.get("/v1/human/support-access", headers=owner)).json()
    assert state["active"]["requests"] >= 1
    assert any(item["path"] == base + "/documents" for item in state["operator_requests"])
    listed = (await client.get("/v1/admin/workspaces", headers=admin_headers)).json()
    assert next(item for item in listed if item["id"] == wid)["support_access_until"]

    closed = await client.delete("/v1/human/support-access", headers=owner)
    assert closed.json()["active"] is None
    again = await client.get(base + "/documents", headers=admin_headers)
    assert again.status_code == 403


async def test_operator_workspaces_stay_reachable_until_made_private(
    client, admin_headers, workspace,
):
    wid = workspace["workspace"]["id"]
    base = f"/v1/admin/workspaces/{wid}"
    assert (await client.get(base + "/documents", headers=admin_headers)).status_code == 200
    email = f"owner-{uuid.uuid4().hex[:8]}@example.test"
    await client.post(base + "/human-users", headers=admin_headers, json={
        "email": email, "display_name": "O", "role": "OWNER",
        "password": "correct-horse-battery"})
    login = await client.post("/v1/auth/login",
                              json={"email": email, "password": "correct-horse-battery"})
    owner = {"Authorization": f"Bearer {login.json()['token']}"}
    state = (await client.get("/v1/human/support-access", headers=owner)).json()
    assert state["operator_managed"] is True
    made = await client.post("/v1/human/support-access/make-private", headers=owner)
    assert made.json()["private"] is True
    assert (await client.get(base + "/documents", headers=admin_headers)).status_code == 403

    reviewer_email = f"rev-{uuid.uuid4().hex[:8]}@example.test"
    conn = await asyncpg.connect(
        os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1))
    try:
        await conn.execute("UPDATE workspaces SET operator_managed = TRUE WHERE id = $1::uuid",
                           wid)
    finally:
        await conn.close()
    await client.post(base + "/human-users", headers=admin_headers, json={
        "email": reviewer_email, "display_name": "R", "role": "REVIEWER",
        "password": "correct-horse-battery"})
    reviewer_login = await client.post("/v1/auth/login", json={
        "email": reviewer_email, "password": "correct-horse-battery"})
    reviewer = {"Authorization": f"Bearer {reviewer_login.json()['token']}"}
    refused = await client.post("/v1/human/support-access", headers=reviewer, json={"hours": 1})
    assert refused.status_code == 403
