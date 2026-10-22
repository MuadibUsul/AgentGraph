"""Lineage stamps and grant snapshots acceptance (PRD AG-ID-03)."""

from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.integration


def _db() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def _agent(client, admin_headers, workspace, role="EXECUTOR", purpose=None):
    response = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        headers=admin_headers,
        json={
            "agent_signature": f"{role.lower()}_{uuid.uuid4().hex[:6]}",
            "role_scope": role,
            "purpose": purpose,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return {"Authorization": f"Bearer {body['api_key']}"}, body["token"]


async def _root_token(client, workspace) -> str:
    token = await client.get("/v1/tokens/self", headers=workspace["headers"])
    assert token.status_code == 200, token.text
    assert token.json()["kind"] == "root"
    return token.json()["token_id"]


async def test_agent_writes_carry_a_fixed_stamp(client, admin_headers, workspace):
    agent, token = await _agent(client, admin_headers, workspace, purpose="research")
    root = await _root_token(client, workspace)
    key = f"lineage/{uuid.uuid4().hex[:6]}"
    written = await client.put(
        f"/v1/documents/{key}",
        headers=agent,
        json={"content": "stamped", "expected_version": 0},
    )
    assert written.status_code == 200, written.text
    stamp = written.json()["document"]["updated_stamp"]
    assert stamp["chain"] == [root, token["canonical_id"]]
    assert stamp["token_id"] == token["canonical_id"]
    assert stamp["grant_snapshot_id"].startswith("gs_")
    assert written.json()["document"]["created_stamp"] == stamp

    # Revoking the token afterwards never rewrites history (acceptance 2).
    revoked = await client.patch(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens/{token['id']}",
        headers=admin_headers,
        json={"is_active": False},
    )
    assert revoked.status_code == 200
    reread = await client.get(f"/v1/documents/{key}", headers=workspace["headers"])
    assert reread.json()["updated_stamp"] == stamp
    versions = await client.get(f"/v1/documents/{key}/versions", headers=workspace["headers"])
    assert versions.json()["versions"][0]["stamp"]["chain"] == stamp["chain"]

    explained = await client.get(f"/v1/documents/{key}/explain", headers=workspace["headers"])
    assert explained.status_code == 200, explained.text
    body = explained.json()
    assert body["writer"]["principal_id"] == token["agent_signature"]
    assert body["token"]["token_id"] == token["canonical_id"]
    assert body["issued_by"]["token_id"] == root
    assert body["grant_at_write"]["role_scope"] == "EXECUTOR"
    assert body["grant_at_write"]["purpose"] == "research"
    assert "doc:write" in body["effective_permissions_at_write"]


async def test_grant_changes_create_new_snapshots(client, admin_headers, workspace):
    agent, token = await _agent(client, admin_headers, workspace)
    first = await client.put(
        f"/v1/documents/g/{uuid.uuid4().hex[:6]}",
        headers=agent,
        json={"content": "a", "expected_version": 0},
    )
    first_snapshot = first.json()["document"]["updated_stamp"]["grant_snapshot_id"]
    changed = await client.patch(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens/{token['id']}",
        headers=admin_headers,
        json={"role_scope": "ANALYST"},
    )
    assert changed.status_code == 200, changed.text
    second = await client.put(
        f"/v1/documents/g/{uuid.uuid4().hex[:6]}",
        headers=agent,
        json={"content": "b", "expected_version": 0},
    )
    second_snapshot = second.json()["document"]["updated_stamp"]["grant_snapshot_id"]
    assert second_snapshot != first_snapshot
    conn = await asyncpg.connect(_db())
    try:
        roles = dict(
            await conn.fetch(
                "SELECT id, role_scope FROM grant_snapshots WHERE id = ANY($1::text[])",
                [first_snapshot, second_snapshot],
            )
        )
        assert roles == {first_snapshot: "EXECUTOR", second_snapshot: "ANALYST"}
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "UPDATE grant_snapshots SET role_scope = 'COMMANDER' WHERE id = $1",
                first_snapshot,
            )
    finally:
        await conn.close()


async def test_written_by_subtree_matches_the_database(client, admin_headers, workspace):
    agent, token = await _agent(client, admin_headers, workspace)
    other, _ = await _agent(client, admin_headers, workspace)
    for index in range(3):
        await client.put(
            f"/v1/documents/sub/{uuid.uuid4().hex[:6]}-{index}",
            headers=agent,
            json={"content": "mine", "expected_version": 0},
        )
    await client.put(
        f"/v1/documents/sub/{uuid.uuid4().hex[:6]}",
        headers=other,
        json={"content": "theirs", "expected_version": 0},
    )
    listed = await client.get(
        f"/v1/documents?written_by_subtree={token['canonical_id']}&limit=500",
        headers=workspace["headers"],
    )
    assert listed.status_code == 200, listed.text
    conn = await asyncpg.connect(_db())
    try:
        expected = await conn.fetchval(
            "SELECT count(DISTINCT d.id) FROM shared_documents d JOIN document_versions v "
            "ON v.document_id = d.id WHERE d.workspace_id = $1::uuid "
            "AND d.archived_at IS NULL AND v.stamp->'chain' ? $2",
            workspace["workspace"]["id"],
            token["canonical_id"],
        )
    finally:
        await conn.close()
    assert listed.json()["total"] == expected == 3


async def test_token_visibility_rules(client, admin_headers, workspace):
    agent, token = await _agent(client, admin_headers, workspace)
    _, sibling = await _agent(client, admin_headers, workspace)
    root = await _root_token(client, workspace)
    me = await client.get("/v1/tokens/self", headers=agent)
    assert me.status_code == 200 and me.json()["token_id"] == token["canonical_id"]
    assert "api_key" not in me.text and "api_key_hash" not in me.text
    lineage = await client.get("/v1/tokens/self/lineage", headers=agent)
    assert [item["token_id"] for item in lineage.json()["chain"]] == [
        root,
        token["canonical_id"],
    ]
    # An agent may read its ancestors but not list an ancestor's subtree.
    assert (await client.get(f"/v1/tokens/{root}", headers=agent)).status_code == 200
    assert (await client.get(f"/v1/tokens/{root}/tree", headers=agent)).status_code == 403
    assert (
        await client.get(f"/v1/tokens/{sibling['canonical_id']}", headers=agent)
    ).status_code == 403
    tree = await client.get(f"/v1/tokens/{root}/tree", headers=workspace["headers"])
    ids = {item["token_id"] for item in tree.json()["items"]}
    assert {token["canonical_id"], sibling["canonical_id"]} <= ids


async def test_events_and_audit_carry_actor_stamps(client, admin_headers, workspace):
    agent, token = await _agent(client, admin_headers, workspace)
    key = f"events/{uuid.uuid4().hex[:6]}"
    await client.put(
        f"/v1/documents/{key}", headers=agent, json={"content": "x", "expected_version": 0}
    )
    conn = await asyncpg.connect(_db())
    try:
        payload = await conn.fetchval(
            "SELECT payload FROM workspace_events WHERE workspace_id = $1::uuid "
            "AND doc_key = $2 ORDER BY id DESC LIMIT 1",
            workspace["workspace"]["id"],
            key,
        )
        audit = await conn.fetchval(
            "SELECT stamp FROM state_audit_diffs WHERE workspace_id = $1::uuid "
            "ORDER BY created_at DESC LIMIT 1",
            workspace["workspace"]["id"],
        )
        timeline = await conn.fetchval(
            "SELECT stamp FROM timeline_streams WHERE workspace_id = $1::uuid "
            "ORDER BY created_at DESC LIMIT 1",
            workspace["workspace"]["id"],
        )
    finally:
        await conn.close()
    actor = json.loads(payload)["actor_stamp"]
    assert actor["chain"] and actor["token_id"] == token["canonical_id"]
    assert json.loads(audit)["token_id"] == token["canonical_id"]
    assert json.loads(timeline)["chain"]


async def test_human_and_admin_writes_are_stamped(client, admin_headers, workspace):
    email = f"stamp-{uuid.uuid4().hex[:8]}@example.test"
    await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/human-users",
        headers=admin_headers,
        json={"email": email, "display_name": "H", "role": "OWNER",
              "password": "correct-horse-battery"},
    )
    login = await client.post(
        "/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    human = {"Authorization": f"Bearer {login.json()['token']}"}
    written = await client.put(
        f"/v1/documents/h/{uuid.uuid4().hex[:6]}",
        headers=human,
        json={"content": "human", "expected_version": 0},
    )
    stamp = written.json()["document"]["updated_stamp"]
    assert stamp["kind"] == "human" and stamp["principal_id"].startswith("prn_")
    assert stamp["chain"] == [stamp["principal_id"]]
