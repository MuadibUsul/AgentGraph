"""Token tree: root token, issuance, revocation, renewal, directory (PRD AG-ID-01/02/04/05/06)."""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
import pytest

from tests.conftest import run_server

ADMIN = {"Authorization": "Bearer test-admin"}
V2026 = {"AgentGraph-Version": "2026-11"}


def _dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# ------------------------------------------------------------ pure narrowing


def test_issued_grants_are_always_subsets():
    """AG-ID-02 acceptance 8: 1,000 random (parent, request) pairs, 0 violations."""
    from app.errors import ApiError
    from app.services.token_tree import SCOPES, narrow

    rng = random.Random(20261021)
    namespaces = ["research", "goals", "ideas", "ops", "raw_inbox"]
    now = datetime.now(timezone.utc)
    issued = 0
    for _ in range(1_000):
        parent = {
            "scopes": rng.sample(SCOPES, rng.randint(1, len(SCOPES))),
            "read": ["*"] if rng.random() < 0.3 else rng.sample(namespaces, rng.randint(1, 4)),
            "expires_at": None if rng.random() < 0.3 else now + timedelta(
                seconds=rng.randint(120, 200_000)),
        }
        parent["write"] = (["*"] if "*" in parent["read"] and rng.random() < 0.5
                           else rng.sample(namespaces, rng.randint(0, 3)))
        request = {
            "scopes": rng.sample(SCOPES, rng.randint(1, 5)),
            "namespaces": {"read": rng.choice([None, ["*"], rng.sample(namespaces, 2)]),
                           "write": rng.choice([None, [], rng.sample(namespaces, 1)])},
            "ttl_seconds": rng.choice([None, 600, 3_600, 7_200, 86_400]),
            "can_issue": rng.random() < 0.3,
        }
        try:
            grant = narrow(parent, request, child_depth=rng.randint(2, 3), max_depth=2,
                           now=now)
        except ApiError:
            continue
        issued += 1
        assert set(grant["scopes"]) <= set(parent["scopes"])
        for key in ("read", "write"):
            if "*" not in parent[key]:
                assert "*" not in grant[key] and set(grant[key]) <= set(parent[key])
        if parent["expires_at"] is not None:
            assert grant["expires_at"] <= parent["expires_at"]
    assert issued > 100  # the generator exercises the success path too


# ------------------------------------------------------------- integration



@pytest.fixture(scope="module")
async def tree(api_base_url) -> AsyncIterator[httpx.AsyncClient]:
    del api_base_url
    async with run_server({"CHILD_TOKEN_ISSUANCE": "true", "LEASE_REAPER_INTERVAL_SECONDS": "5"},
                          ".server-tokens.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as http:
            yield http


async def _workspace(client) -> dict:
    created = await client.post("/v1/admin/workspaces", headers=ADMIN,
                                json={"name": f"Tree {uuid.uuid4().hex[:6]}", "tier": "pro"})
    assert created.status_code in (200, 201), created.text
    body = created.json()
    assert body["root_key"].startswith("agr_")
    owner_email = f"owner-{uuid.uuid4().hex[:8]}@example.test"
    owner = await client.post(
        f"/v1/admin/workspaces/{body['workspace']['id']}/human-users", headers=ADMIN,
        json={"email": owner_email, "display_name": "Owner", "role": "OWNER",
              "password": "correct-horse-battery"})
    login = await client.post("/v1/auth/login", json={"email": owner_email,
                                                      "password": "correct-horse-battery"})
    return {"id": body["workspace"]["id"], "key": _bearer(body["api_key"]),
            "root": _bearer(body["root_key"]), "root_secret": body["root_key"],
            "owner_id": owner.json()["user"]["id"],
            "human": _bearer(login.json()["token"])}


async def _issue(client, headers, **body) -> dict:
    payload = {"scopes": ["docs:read", "docs:write", "tasks:read", "tasks:write"],
               "purpose": "test", **body}
    payload.setdefault("agent", {"agent_id": f"a-{uuid.uuid4().hex[:8]}"})
    response = await client.post("/v1/tokens", headers=headers, json=payload)
    return response


async def _issued(client, headers, **body) -> dict:
    response = await _issue(client, headers, **body)
    assert response.status_code == 201, response.text
    data = response.json()
    data["headers"] = _bearer(data["token"])
    return data


@pytest.mark.integration
async def test_issuance_is_off_by_default(client, admin_headers):
    created = (await client.post("/v1/admin/workspaces", headers=admin_headers,
                                 json={"name": "gate", "tier": "pro"})).json()
    response = await _issue(client, _bearer(created["root_key"]))
    assert response.status_code == 403
    assert response.json()["error"]["details"]["reason"] == "child_token_issuance_disabled"


@pytest.mark.integration
async def test_root_token_only_manages_tokens(tree):
    client = tree
    ws = await _workspace(client)
    denied = await client.get("/v1/documents", headers=ws["root"])
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "root_token_data_access_denied"
    root_self = (await client.get("/v1/tokens/self", headers=ws["root"])).json()
    assert root_self["kind"] == "root"

    branch = await _issued(client, ws["root"], scopes=["docs:read", "docs:write", "token:issue"])
    assert branch["kind"] == "branch" and branch["depth"] == 0
    assert branch["parent_token_id"] == root_self["token_id"]
    conn = await asyncpg.connect(_dsn())
    try:
        targets = await conn.fetchval(
            "SELECT targets FROM workspace_events WHERE workspace_id = $1::uuid "
            "AND type = 'token.root_used' ORDER BY id DESC LIMIT 1", ws["id"])
    finally:
        await conn.close()
    assert f"human:{ws['owner_id']}" in json.loads(targets)

    key = (await client.get("/v1/tokens/self", headers=ws["key"])).json()
    assert key["kind"] == "branch" and key["parent_token_id"] == root_self["token_id"]
    written = await client.put("/v1/documents/still-works", headers=ws["key"],
                               json={"content": "x", "expected_version": 0})
    assert written.status_code in (200, 201), written.text

    rotated = await client.post(f"/v1/admin/workspaces/{ws['id']}/root-token:rotate",
                                headers=ADMIN)
    assert rotated.json()["root_token_id"] == root_self["token_id"]
    stale = await client.get("/v1/tokens/self", headers=ws["root"])
    assert stale.status_code == 401
    fresh = await client.get("/v1/tokens/self", headers=_bearer(rotated.json()["root_key"]))
    assert fresh.json()["token_id"] == root_self["token_id"]
    still = await client.get("/v1/tokens/self", headers=branch["headers"])
    assert still.json()["root_token_id"] == root_self["token_id"]
    assert (await client.get("/v1/documents", headers=branch["headers"])).status_code == 200


@pytest.mark.integration
async def test_children_only_narrow(tree):
    client = tree
    ws = await _workspace(client)
    parent = await _issued(client, ws["root"], scopes=["docs:read", "docs:write", "token:issue"],
                           namespaces={"read": ["*"], "write": ["research"]},
                           max_descendants=10)
    escalate = await _issue(client, parent["headers"], scopes=["docs:read", "export"])
    assert escalate.status_code == 403
    assert escalate.json()["error"]["code"] == "scope_escalation"
    assert escalate.json()["error"]["details"]["requested_extra"] == ["export"]
    wider = await _issue(client, parent["headers"], scopes=["docs:read", "docs:write"],
                         namespaces={"write": ["goals"]})
    assert wider.status_code == 403
    assert wider.json()["error"]["details"]["namespaces_extra"] == {"write": ["goals"]}

    reader = await _issued(client, parent["headers"], scopes=["docs:read"])
    assert reader["kind"] == "child" and reader["depth"] == 1
    assert reader["lineage"] == [*parent["lineage"], reader["token_id"]]
    blocked = await client.put("/v1/documents/research/x", headers=reader["headers"],
                               json={"namespace": "research", "content": "x",
                                     "expected_version": 0})
    assert blocked.status_code == 403
    await client.put("/v1/documents/visible", headers=ws["key"],
                     json={"content": "v", "expected_version": 0})
    assert (await client.get("/v1/documents/visible?include_unverified=true",
                             headers=reader["headers"])).status_code == 200
    conn = await asyncpg.connect(_dsn())
    try:
        # Shrinking the parent afterwards shrinks the child: the chain is intersected.
        await conn.execute("UPDATE agent_tokens SET scopes = array_remove(scopes, 'docs:read') "
                           "WHERE canonical_id = $1", parent["token_id"])
        cut = await client.get("/v1/documents/visible?include_unverified=true",
                               headers=reader["headers"])
        assert cut.status_code == 403
        await conn.execute("UPDATE agent_tokens SET scopes = array_append(scopes, 'docs:read') "
                           "WHERE canonical_id = $1", parent["token_id"])

        directory = (await client.get(f"/v1/agents/{reader['agent_id']}",
                                      headers=ws["key"])).json()
        assert reader["token_id"] in directory["token_ids"]
        assert directory["owner_human"] == ws["owner_id"]  # inherited from the tree root

        level1 = await _issued(client, parent["headers"], scopes=["docs:read", "token:issue"],
                               max_descendants=3)
        assert level1["depth"] == 1 and level1["can_issue"] is True
        cannot = await _issue(client, level1["headers"], scopes=["docs:read"], can_issue=True)
        assert cannot.status_code == 403
        assert cannot.json()["error"]["code"] == "depth_limit_exceeded"
        level2 = await _issued(client, level1["headers"], scopes=["docs:read"])
        assert level2["depth"] == 2
        too_deep = await _issue(client, level2["headers"], scopes=["docs:read"])
        assert too_deep.status_code == 403
        assert too_deep.json()["error"]["code"] == "depth_limit_exceeded"

        short = await _issued(client, parent["headers"], scopes=["docs:read", "token:issue"],
                              ttl_seconds=3_600, max_descendants=2)
        outlive = await _issue(client, short["headers"], scopes=["docs:read"], ttl_seconds=7_200)
        assert outlive.status_code == 422
        assert outlive.json()["error"]["code"] == "ttl_exceeds_parent"

        await conn.execute("UPDATE workspaces SET branch_token_quota = 6 WHERE id = $1::uuid",
                           ws["id"])
        # parent's branch now holds parent, reader, level1, level2, short = 5 tokens.
        sixth = await _issued(client, parent["headers"], scopes=["docs:read"])
        full = await _issue(client, parent["headers"], scopes=["docs:read"])
        assert full.status_code == 409
        assert full.json()["error"]["details"] == {"scope": "branch", "limit": 6, "active": 6}
        await client.post(f"/v1/tokens/{sixth['token_id']}/revoke", headers=parent["headers"],
                          json={"reason": "make room"})
        assert (await _issue(client, parent["headers"], scopes=["docs:read"])).status_code == 201
    finally:
        await conn.close()


@pytest.mark.integration
async def test_revocation_cascades_releases_and_taints(tree):
    client = tree
    ws = await _workspace(client)
    branch = await _issued(client, ws["root"], scopes=[
        "docs:read", "docs:write", "tasks:read", "tasks:write", "events:read", "token:issue"],
        max_descendants=10)
    c1 = await _issued(client, branch["headers"], scopes=[
        "docs:read", "tasks:read", "tasks:write", "events:read", "token:issue"],
        max_descendants=2)
    c2 = await _issued(client, branch["headers"], scopes=["docs:read", "docs:write"])
    g1 = await _issued(client, c1["headers"], scopes=["docs:read", "events:read"])

    task = await client.post("/v1/tasks", headers=ws["key"],
                             json={"title": "held", "assigned_role": c1["role_scope"]})
    claim = await client.post(f"/v1/tasks/{task.json()['task_key']}/claim",
                              headers=c1["headers"], json={})
    assert claim.status_code == 200, claim.text

    since = datetime.now(timezone.utc) - timedelta(seconds=1)
    for index in range(3):
        wrote = await client.put(f"/v1/documents/rev-{index}", headers=c2["headers"],
                                 json={"content": f"by c2 {index}", "expected_version": 0})
        assert wrote.status_code in (200, 201), wrote.text
    derived = await client.put("/v1/documents/derived-d9", headers=ws["key"], json={
        "content": "built on rev-0", "frontmatter": {"derived_from": ["rev-0"]},
        "expected_version": 0})
    assert derived.status_code in (200, 201), derived.text

    sibling = await client.post(f"/v1/tokens/{branch['token_id']}/revoke",
                                headers=c2["headers"], json={"reason": "not mine"})
    assert sibling.status_code == 403

    poll = asyncio.create_task(client.get("/v1/events?wait_seconds=25", headers=g1["headers"]))
    await asyncio.sleep(1.5)
    started = time.monotonic()
    revoked = await client.post(f"/v1/tokens/{branch['token_id']}/revoke", headers=ws["human"],
                                json={"reason": "credentials leaked",
                                      "taint_writes_since": since.isoformat()})
    assert revoked.status_code == 200, revoked.text
    body = revoked.json()
    assert body["count"] == 4
    assert set(body["revoked"]) == {branch["token_id"], c1["token_id"], c2["token_id"],
                                    g1["token_id"]}
    assert body["released_leases"] == 1
    assert body["tainted_documents"] == 4
    ended = await poll
    assert ended.status_code == 401 and time.monotonic() - started <= 5
    for token in (branch, c1, c2, g1):
        denied = await client.get("/v1/tokens/self", headers=token["headers"])
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "token_revoked"
        assert denied.json()["error"]["details"]["revoked_ancestor"] == branch["token_id"]

    reread = await client.get(f"/v1/tasks/{task.json()['task_key']}", headers=ws["key"])
    assert reread.json()["status"] == "READY"
    conn = await asyncpg.connect(_dsn())
    try:
        reason = await conn.fetchval(
            "SELECT payload->'details'->>'reason' FROM timeline_streams "
            "WHERE workspace_id = $1::uuid AND event_type = 'LEASE_EXPIRED' "
            "ORDER BY created_at DESC LIMIT 1", ws["id"])
        tainted = await conn.fetch(
            "SELECT doc_key FROM shared_documents WHERE workspace_id = $1::uuid AND tainted",
            ws["id"])
    finally:
        await conn.close()
    assert reason == "holder_revoked"
    assert {row["doc_key"] for row in tainted} == {"rev-0", "rev-1", "rev-2", "derived-d9"}


@pytest.mark.integration
async def test_renewal_and_expiry(tree):
    client = tree
    ws = await _workspace(client)
    parent = await _issued(client, ws["root"], scopes=["docs:read", "token:issue"],
                           max_descendants=5)
    child = await _issued(client, parent["headers"], scopes=["docs:read"], ttl_seconds=300)
    warned = await client.get("/v1/tokens/self", headers=child["headers"])
    warnings = {item["code"]: item for item in warned.json().get("warnings", [])}
    assert warnings["token_expiring"]["renew_with"] == parent["token_id"]

    own = await client.post(f"/v1/tokens/{child['token_id']}/renew", headers=child["headers"],
                            json={"ttl_seconds": 3_600})
    assert own.status_code == 403 and own.json()["error"]["code"] == "renewal_requires_parent"
    renewed = await client.post(f"/v1/tokens/{child['token_id']}/renew",
                                headers=parent["headers"], json={"ttl_seconds": 3_600})
    assert renewed.status_code == 200, renewed.text
    parent_view = (await client.get("/v1/tokens/self", headers=parent["headers"])).json()
    assert renewed.json()["expires_at"] <= parent_view["expires_at"]

    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("UPDATE agent_tokens SET expires_at = NOW() - INTERVAL '1 minute' "
                           "WHERE canonical_id = $1", child["token_id"])
        late = await client.post(f"/v1/tokens/{child['token_id']}/renew",
                                 headers=parent["headers"], json={"ttl_seconds": 600})
        assert late.status_code == 409
        assert late.json()["error"]["code"] == "token_already_expired"

        other = await _issued(client, parent["headers"], scopes=["docs:read"])
        await conn.execute("UPDATE agent_tokens SET expires_at = NOW() - INTERVAL '1 minute' "
                           "WHERE canonical_id = $1", parent["token_id"])
        expired = await client.get("/v1/tokens/self", headers=other["headers"])
        assert expired.status_code == 401
        assert expired.json()["error"]["code"] == "token_expired"
        assert expired.json()["error"]["details"]["expired_token"] == parent["token_id"]
    finally:
        await conn.close()


@pytest.mark.integration
async def test_directory_registration_and_identity(tree):
    client = tree
    ws = await _workspace(client)
    minted = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
        json={"agent_signature": f"legacy_{uuid.uuid4().hex[:6]}", "role_scope": "ANALYST"})
    agent = _bearer(minted.json()["api_key"])
    unregistered = await client.put("/v1/documents/before-registration", headers=agent,
                                    json={"content": "x", "expected_version": 0})
    assert "not_registered" in {item["code"] for item in unregistered.json()["warnings"]}
    registered = await client.post("/v1/agents/register", headers=agent,
                                   json={"agent_id": "researcher", "role": "researcher",
                                         "capabilities": ["search"]})
    assert registered.status_code == 201, registered.text
    assert registered.json()["agent_id"] == "researcher"
    again = await client.post("/v1/agents/register", headers=agent, json={"agent_id": "writer"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_registered"
    wrote = await client.put("/v1/documents/after-registration", headers={**agent, **V2026},
                             json={"content": "y", "expected_version": 0})
    document = wrote.json()["document"]
    assert document["updated_by"] == "agent:researcher"
    assert document["updated_by_label"] == minted.json()["token"]["agent_signature"]
    self_trust = await client.patch("/v1/agents/researcher", headers=agent,
                                    json={"fields": {"trust_level": "internal"}})
    assert self_trust.status_code == 403
    assert self_trust.json()["error"]["code"] == "field_not_writable"

    for index in range(3):
        await client.put(f"/v1/documents/legacy-{index}",
                         headers={**ws["key"], "X-Agent-Signature": "analyst"},
                         json={"content": "old", "expected_version": 0})
    before = await client.get("/v1/documents/legacy-0", headers={**ws["human"], **V2026})
    assert before.json()["updated_by"] == "legacy:analyst"
    merged = await client.post("/v1/human/agents/merge-legacy", headers=ws["human"],
                               json={"legacy": "analyst", "agent_id": "researcher"})
    assert merged.status_code == 200 and merged.json()["documents"] == 3
    after = await client.get("/v1/documents/legacy-0", headers={**ws["human"], **V2026})
    assert after.json()["updated_by"] == "agent:researcher"
    assert after.json()["updated_by_label"] == "analyst"
    old_style = await client.get("/v1/documents/legacy-0", headers=ws["human"])
    assert old_style.json()["updated_by"] == "analyst"

    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("UPDATE agent_directory SET last_seen_at = NOW() - INTERVAL '25 hours' "
                           "WHERE agent_id = 'researcher' AND workspace_id = $1::uuid", ws["id"])
        status = None
        for _ in range(40):
            status = await conn.fetchval(
                "SELECT status FROM agent_directory WHERE agent_id = 'researcher' "
                "AND workspace_id = $1::uuid", ws["id"])
            if status == "offline":
                break
            await asyncio.sleep(0.5)
        assert status == "offline"
        event = await conn.fetchval(
            "SELECT count(*) FROM workspace_events WHERE workspace_id = $1::uuid "
            "AND type = 'agent.status_changed'", ws["id"])
        assert event >= 1
    finally:
        await conn.close()
    listed = (await client.get("/v1/agents?capability=search", headers=ws["key"])).json()
    assert [item["agent_id"] for item in listed["items"]] == ["researcher"]


@pytest.mark.integration
async def test_orientation_uses_the_directory(tree):
    """AG-OB-01 acceptance 3 and AG-ID-06 acceptance 1, 6."""
    client = tree
    ws = await _workspace(client)
    second = f"second-{uuid.uuid4().hex[:8]}@example.test"
    await client.post(f"/v1/admin/workspaces/{ws['id']}/human-users", headers=ADMIN,
                      json={"email": second, "display_name": "Second", "role": "REVIEWER",
                            "password": "correct-horse-battery"})
    for index in range(5):
        await _issued(client, ws["root"], scopes=["docs:read"],
                      agent={"agent_id": f"peer-{index}-{uuid.uuid4().hex[:4]}",
                             "role": "researcher"})
    me = await _issued(client, ws["root"], scopes=["docs:read", "docs:write", "events:read"],
                       agent={"agent_id": f"me-{uuid.uuid4().hex[:6]}", "role": "writer",
                              "capabilities": ["drafting"]})
    pack = (await client.get("/v1/orientation", headers=me["headers"])).json()
    assert pack["me"]["registered"] is True and pack["me"]["agent_id"] == me["agent_id"]
    assert pack["me"]["owner_human"]["user_id"] == ws["owner_id"]
    assert pack["me"]["token"]["lineage"] == me["lineage"]
    collaborators = pack["collaborators"]
    assert len(collaborators) == 7
    assert {item["source"] for item in collaborators} == {"registry"}
    assert sorted(item["type"] for item in collaborators) == ["agent"] * 5 + ["human"] * 2
    assert "not_registered" not in {item["code"] for item in pack["warnings"]}
    assert pack["inbox"]["default_subscription_id"]


@pytest.mark.integration
async def test_console_lists_people_issued_tokens_and_folds_delegated_ones(tree):
    """Agent-delegated child tokens stay out of the console's token list."""
    client = tree
    ws = await _workspace(client)
    branch = await _issued(client, ws["root"],
                           scopes=["docs:read", "docs:write", "token:issue"],
                           max_descendants=10)
    children = [await _issued(client, branch["headers"], scopes=["docs:read"], ttl_seconds=600)
                for _ in range(3)]

    listed = await client.get("/v1/human/agent-tokens", headers=ws["human"])
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert all(item["issued_by"] == "human" and item["depth"] <= 1 for item in body["items"])
    child_ids = {child["token_id"] for child in children}
    assert not child_ids & {item["canonical_id"] for item in body["items"]}
    mine = next(item for item in body["items"] if item["canonical_id"] == branch["token_id"])
    expected = 3
    assert mine["delegated_total"] == expected and mine["delegated_active"] == expected
    assert body["delegated_total"] == expected and body["human_issued"] == len(body["items"])

    nested = (await client.get("/v1/human/agent-tokens", headers=ws["human"], params={
        "issued_by": "agent", "parent": branch["token_id"]})).json()
    assert {item["canonical_id"] for item in nested["items"]} >= child_ids
    assert all(item["issued_by"] == "agent" and item["issued_by_agent"] for item in nested["items"])
    everything = (await client.get(f"/v1/admin/workspaces/{ws['id']}/agent-tokens",
                                   headers=ADMIN, params={"issued_by": "all"})).json()
    assert len(everything["items"]) == body["human_issued"] + expected

    overview = (await client.get("/v1/human/agent-onboarding", headers=ws["human"])).json()
    assert overview["summary"]["tokens"] == body["human_issued"]


@pytest.mark.integration
async def test_agents_narrow_roles_and_take_delegations_back(tree):
    """Delegation without capability tokens: lower roles, short lives, one-call take-back."""
    client = tree
    ws = await _workspace(client)
    lead = await _issued(client, ws["root"], role_scope="EXECUTOR",
                         scopes=["docs:read", "docs:write", "tasks:read", "token:issue"],
                         max_descendants=10)

    scout = await _issued(client, lead["headers"], role_scope="SCOUT", scopes=["docs:read"])
    assert scout["role_scope"] == "SCOUT"
    lives = (datetime.fromisoformat(scout["expires_at"].replace("Z", "+00:00"))
             - datetime.now(timezone.utc))
    assert 0.95 * 3_600 < lives.total_seconds() <= 3_600  # default: one hour
    auditor = await _issue(client, lead["headers"], role_scope="AUDITOR", scopes=["docs:read"])
    assert auditor.status_code == 403
    assert auditor.json()["error"]["code"] == "scope_escalation"
    assert "SCOUT" in auditor.json()["error"]["details"]["allowed_roles"]

    helper = await _issued(client, lead["headers"], scopes=["docs:read"])
    taken = await client.post(f"/v1/tokens/{lead['token_id']}/revoke", headers=lead["headers"],
                              json={"reason": "task finished", "keep_self": True})
    assert taken.status_code == 200, taken.text
    assert set(taken.json()["revoked"]) == {scout["token_id"], helper["token_id"]}
    assert taken.json()["kept"] == lead["token_id"]
    assert (await client.get("/v1/documents", headers=lead["headers"])).status_code == 200
    assert (await client.get("/v1/documents", headers=helper["headers"])).status_code == 401

    # The console does the same by row id.
    again = await _issued(client, lead["headers"], scopes=["docs:read"])
    row = (await client.get("/v1/human/agent-tokens", headers=ws["human"])).json()
    lead_row = next(item for item in row["items"] if item["canonical_id"] == lead["token_id"])
    assert lead_row["delegated_active"] == 1
    console = await client.post(f"/v1/human/agent-tokens/{lead_row['id']}/revoke-delegated",
                                headers=ws["human"], json={})
    assert console.status_code == 200, console.text
    assert console.json()["revoked"] == [again["token_id"]]

    # Long-dead delegated tokens are purged; people-issued ones never are.
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("UPDATE agent_tokens SET revoked_at = NOW() - INTERVAL '8 days' "
                           "WHERE canonical_id = ANY($1::text[])",
                           [scout["token_id"], helper["token_id"]])
        await conn.execute("UPDATE agent_tokens SET revoked_at = NOW() - INTERVAL '8 days', "
                           "is_active = FALSE WHERE canonical_id = $1", lead["token_id"])
        from app.services.token_tree import purge_stale_delegated

        assert await purge_stale_delegated(force=True) >= 2
        left = await conn.fetch("SELECT canonical_id FROM agent_tokens WHERE canonical_id = "
                                "ANY($1::text[])", [scout["token_id"], helper["token_id"],
                                                    again["token_id"], lead["token_id"]])
    finally:
        await conn.close()
    assert {row["canonical_id"] for row in left} == {again["token_id"], lead["token_id"]}
