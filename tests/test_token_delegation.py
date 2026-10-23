"""Token delegation 1.0: role package plus a permission delta (acceptance D-01, D-02,
inheritance down the chain and the shorter child lifetime)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from tests.test_token_tree import ADMIN, _issue, _issued, _workspace, tree  # noqa: F401

pytestmark = pytest.mark.integration


async def _mint(client, ws: dict, role: str) -> dict:
    minted = await client.post(f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
                               json={"agent_signature": f"{role.lower()}-{uuid.uuid4().hex[:6]}",
                                     "role_scope": role})
    assert minted.status_code in (200, 201), minted.text
    body = minted.json()
    return {"id": body["token"]["id"], "cid": body["token"]["canonical_id"],
            "headers": {"Authorization": f"Bearer {body['api_key']}"}}


async def _patch(client, ws: dict, token_id: str, **body):
    return await client.patch(f"/v1/admin/workspaces/{ws['id']}/agent-tokens/{token_id}",
                              headers=ADMIN, json=body)


async def _row_id(client, ws: dict, canonical_id: str) -> str:
    listed = await client.get(f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN)
    assert listed.status_code == 200, listed.text
    return next(item["id"] for item in listed.json()["items"]
                if item["canonical_id"] == canonical_id)


async def test_delta_adds_and_removes_permissions(tree):  # noqa: F811
    """D-01: an AUDITOR gains doc:create and loses proposal:vote, nothing else changes."""
    client = tree
    ws = await _workspace(client)
    auditor = await _mint(client, ws, "AUDITOR")
    before = await client.put("/v1/documents/dl/new", headers=auditor["headers"],
                              json={"content": "x", "expected_version": 0})
    assert before.status_code == 403

    patched = await _patch(client, ws, auditor["id"], scope_add=["doc:create"],
                           scope_remove=["proposal:vote"])
    assert patched.status_code == 200, patched.text
    assert patched.json()["scope_add"] == ["doc:create"]
    assert patched.json()["scope_remove"] == ["proposal:vote"]

    created = await client.put("/v1/documents/dl/new", headers=auditor["headers"],
                               json={"content": "x", "expected_version": 0})
    assert created.status_code in (200, 201), created.text
    proposal = await client.post("/v1/documents/dl/new/proposals", headers=ws["key"],
                                 json={"content": "y", "base_version": 1})
    assert proposal.status_code == 201, proposal.text
    vote = await client.post(f"/v1/proposals/{proposal.json()['id']}/vote",
                             headers=auditor["headers"], json={"score": 4})
    assert vote.status_code == 403
    assert "proposal:vote" in vote.json()["error"]["message"]

    view = (await client.get("/v1/tokens/self", headers=auditor["headers"])).json()
    assert view["permission_delta"] == {"add": ["doc:create"], "remove": ["proposal:vote"]}
    assert "doc:create" in view["effective_permissions"]
    assert "proposal:vote" not in view["effective_permissions"]
    assert "doc:verify" in view["effective_permissions"]  # the rest of the package stays

    cleared = await _patch(client, ws, auditor["id"], scope_add=[], scope_remove=[])
    assert cleared.json()["scope_add"] == [] and cleared.json()["scope_remove"] == []
    again = await client.put("/v1/documents/dl/other", headers=auditor["headers"],
                             json={"content": "x", "expected_version": 0})
    assert again.status_code == 403


async def test_bad_deltas_are_refused(tree):  # noqa: F811
    """D-02: unknown permissions are 422 and change nothing (agent tokens are never
    COMMANDER, so a commander delta cannot be expressed)."""
    client = tree
    ws = await _workspace(client)
    scout = await _mint(client, ws, "SCOUT")
    unknown = await _patch(client, ws, scout["id"], scope_add=["doc:teleport"])
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "scope_unknown"
    assert unknown.json()["error"]["details"]["unknown"] == ["doc:teleport"]
    commander = await client.post(f"/v1/admin/workspaces/{ws['id']}/agent-tokens",
                                  headers=ADMIN, json={"agent_signature": "boss",
                                                       "role_scope": "COMMANDER"})
    assert commander.status_code == 422
    view = (await client.get("/v1/tokens/self", headers=scout["headers"])).json()
    assert view["permission_delta"] == {"add": [], "remove": []}


async def test_children_inherit_removals_and_live_shorter(tree):  # noqa: F811
    """A removal on a branch reaches every child it issues; children default to 12 hours."""
    client = tree
    ws = await _workspace(client)
    branch = await _issued(client, ws["root"], role_scope="AUDITOR",
                           scopes=["docs:read", "docs:write", "proposals:write", "token:issue"],
                           max_descendants=5)
    row = await _row_id(client, ws, branch["token_id"])
    patched = await _patch(client, ws, row, scope_remove=["proposal:vote"])
    assert patched.status_code == 200, patched.text

    child = await _issued(client, branch["headers"], scopes=["docs:read", "proposals:write"])
    view = (await client.get("/v1/tokens/self", headers=child["headers"])).json()
    assert view["permission_delta"]["remove"] == ["proposal:vote"]
    assert "proposal:vote" not in view["effective_permissions"]
    assert "doc:propose" in view["effective_permissions"]
    lives = (datetime.fromisoformat(child["expires_at"]) - datetime.now(timezone.utc))
    assert 11.9 * 3_600 < lives.total_seconds() <= 12 * 3_600

    too_long = await _issue(client, branch["headers"], scopes=["docs:read"],
                            ttl_seconds=73 * 3_600)
    assert too_long.status_code == 422
    assert too_long.json()["error"]["details"]["max"] == 72 * 3_600
