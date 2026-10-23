"""The tenant tree in use: structure (AG-ID-09), inheritance (AG-ID-11),
visibility and sharing (AG-ID-12), per-node export, deletion and detaching
(AG-ID-13)."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest

from tests.conftest import run_server

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}
PASSWORD = "correct-horse-battery"
TEAM_DOCS = ("SELECT s.canonical_id, s.doc_key FROM shared_documents s JOIN workspaces w "
             "ON w.id = s.workspace_id WHERE w.tenant_id = $1 AND s.doc_key LIKE 'team-doc-%' "
             "ORDER BY s.doc_key")


def _dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _sql(query: str, *args):
    conn = await asyncpg.connect(_dsn())
    try:
        return await conn.fetch(query, *args)
    finally:
        await conn.close()


@pytest.fixture(scope="module")
async def org(api_base_url) -> AsyncIterator[httpx.AsyncClient]:
    del api_base_url
    async with run_server({"CHILD_TOKEN_ISSUANCE": "true"}, ".server-tree.log") as base:
        async with httpx.AsyncClient(base_url=base, timeout=120) as http:
            yield http


async def _root(client, label: str, *, company: bool = False) -> dict:
    created = await client.post("/v1/admin/workspaces", headers=ADMIN,
                                json={"name": f"{label} {uuid.uuid4().hex[:6]}", "tier": "pro"})
    body = created.json()
    tenant = body["workspace"]["tenant_id"]
    if company:
        await _sql("UPDATE tenants SET tenant_type = 'company' WHERE id = $1", tenant)
    alias = (await _sql("SELECT alias FROM tenants WHERE id = $1", tenant))[0][0]
    return {"tenant": tenant, "alias": alias, "workspace": body["workspace"]["id"],
            "key": _bearer(body["api_key"])}


async def _person(client, node: dict, label: str, role: str = "OWNER") -> dict:
    await _operator_setup(node)
    email = f"{label}-{uuid.uuid4().hex[:8]}@example.test"
    created = await client.post(
        f"/v1/admin/workspaces/{node['workspace']}/human-users", headers=ADMIN,
        json={"email": email, "display_name": label.title(), "role": role, "password": PASSWORD})
    assert created.status_code in (200, 201), created.text
    login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    return {"email": email, "id": created.json()["user"]["id"],
            "headers": _bearer(login.json()["token"])}


async def _switch(client, person: dict, workspace: str) -> dict:
    switched = await client.post("/v1/auth/switch-workspace", headers=person["headers"],
                                 json={"workspace_id": workspace})
    assert switched.status_code == 200, switched.text
    return _bearer(switched.json()["token"])


async def _child(client, person: dict, parent: str, label: str,
                 tenant_type: str = "organization") -> httpx.Response:
    return await client.post(f"/v1/tenants/{parent}/children", headers=person["headers"],
                             json={"display_name": label, "tenant_type": tenant_type})


def _node(response: httpx.Response) -> dict:
    assert response.status_code == 201, response.text
    body = response.json()
    return {"tenant": body["tenant"]["id"], "alias": body["tenant"]["alias"],
            "workspace": body["workspace"]["id"], "key": _bearer(body["api_key"]),
            "path": body["tenant"]["path"], "depth": body["tenant"]["depth"]}


async def _operator_setup(node: dict) -> None:
    """Fixture shortcut: let the operator path seed this customer-created node.

    Child workspaces made through /v1/tenants are private (v3.35); these tests
    seed agents and people with the admin API, so the node is marked
    operator-managed first. Privacy itself is tested in test_operator_privacy.
    """
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("UPDATE workspaces SET operator_managed = TRUE WHERE id = $1::uuid",
                           node["workspace"])
    finally:
        await conn.close()


async def _agent(client, node: dict, agent_id: str) -> dict:
    await _operator_setup(node)
    minted = await client.post(
        f"/v1/admin/workspaces/{node['workspace']}/agent-tokens", headers=ADMIN,
        json={"agent_signature": agent_id, "role_scope": "EXECUTOR"})
    headers = _bearer(minted.json()["api_key"])
    await client.post("/v1/agents/register", headers=headers, json={"agent_id": agent_id})
    return {"headers": headers, "agent_id": agent_id}


async def test_structure_operations(org):
    """AG-ID-09 acceptance 1-4, 6."""
    client = org
    company = await _root(client, "Company", company=True)
    owner = await _person(client, company, "owner")
    o1 = _node(await _child(client, owner, company["tenant"], "Dept"))
    o2 = _node(await _child(client, owner, o1["tenant"], "Team"))
    personal = _node(await _child(client, owner, o2["tenant"], "Me", "personal"))
    assert personal["path"] == [company["tenant"], o1["tenant"], o2["tenant"],
                                personal["tenant"]]
    under_personal = await _child(client, owner, personal["tenant"], "Nope")
    assert under_personal.status_code == 422
    assert under_personal.json()["error"]["code"] == "tenant_type_invalid_parent"
    o3 = _node(await _child(client, owner, o2["tenant"], "Squad"))
    too_deep = await _child(client, owner, o3["tenant"], "Deep", "personal")
    assert too_deep.status_code == 422
    assert too_deep.json()["error"]["code"] == "tenant_depth_exceeded"

    agent = await _agent(client, o1, "dept-bot")
    by_agent = await client.post(f"/v1/tenants/{o1['tenant']}/children",
                                 headers=agent["headers"], json={"display_name": "x"})
    assert by_agent.status_code == 403 and by_agent.json()["error"]["code"] == "forbidden"

    other_company = await _root(client, "Other", company=True)
    other_owner = await _person(client, other_company, "rival")
    company_under = await client.post(f"/v1/tenants/{other_company['tenant']}/attach",
                                      headers=other_owner["headers"],
                                      json={"parent": o1["tenant"]})
    assert company_under.status_code == 422
    assert company_under.json()["error"]["code"] == "tenant_type_invalid_parent"

    team = await _root(client, "Team")
    team_owner = await _person(client, team, "teamlead")
    items = [{"doc_key": f"team-doc-{index:02d}", "content": f"team doc {index}",
              "expected_version": 0} for index in range(25)]
    for start in (0, 25):
        batch = [{**item, "doc_key": f"{item['doc_key']}-{start}"} for item in items]
        await client.post("/v1/documents/batch", headers=team["key"], json={"items": batch})
    before = await _sql(TEAM_DOCS, team["tenant"])
    assert len(before) == 50
    outsider = await client.post(f"/v1/tenants/{team['tenant']}/attach",
                                 headers=other_owner["headers"],
                                 json={"parent": company["tenant"]})
    assert outsider.status_code in (403, 404)
    asked = await client.post(f"/v1/tenants/{team['tenant']}/attach",
                              headers=owner["headers"], json={"parent": company["tenant"]})
    assert asked.status_code == 409
    details = asked.json()["error"]["details"]
    assert asked.json()["error"]["code"] == "confirmation_required"
    assert details["pending_confirmations"] == [team["tenant"]]
    confirmed = await client.post(f"/v1/tenants/{team['tenant']}/attach",
                                  headers=team_owner["headers"],
                                  json={"parent": company["tenant"],
                                        "confirmation_id": details["confirmation_id"]})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["root_tenant_id"] == company["tenant"]
    after = await _sql(TEAM_DOCS, team["tenant"])
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    still = await client.get(f"/v1/documents/{before[0]['canonical_id']}?include_unverified=true",
                             headers=team["key"])
    assert still.status_code == 200

    moved = await client.post(f"/v1/tenants/{o2['tenant']}/move", headers=owner["headers"],
                              json={"new_parent": company["tenant"]})
    assert moved.status_code == 200, moved.text
    assert moved.json()["path"] == [company["tenant"], o2["tenant"]]
    moved_personal = await _sql("SELECT path FROM tenants WHERE id = $1", personal["tenant"])
    assert list(moved_personal[0][0]) == [company["tenant"], o2["tenant"], personal["tenant"]]
    events = await _sql("SELECT count(*) FROM workspace_events WHERE type = 'tenant.moved' "
                        "AND workspace_id = $1::uuid", o2["workspace"])
    assert events[0][0] == 1
    tree = (await client.get(f"/v1/tenants/{company['tenant']}/tree",
                             headers=owner["headers"])).json()
    children = {item["alias"] for item in tree["root"]["children"]}
    assert {o1["alias"], o2["alias"]} <= children


async def test_inheritance(org):
    """AG-ID-11 acceptance 2-4, 6 and the quota rule."""
    client = org
    company = await _root(client, "Holding", company=True)
    owner = await _person(client, company, "holder")
    o1 = _node(await _child(client, owner, company["tenant"], "Unit"))
    o2 = _node(await _child(client, owner, o1["tenant"], "Cell"))

    locked = await client.put(f"/v1/tenants/{company['tenant']}/policies/external_agents_allowed",
                              headers=owner["headers"], json={"value": False, "locked": True})
    assert locked.status_code == 200, locked.text
    loosen = await client.put(f"/v1/tenants/{o1['tenant']}/policies/external_agents_allowed",
                              headers=owner["headers"], json={"value": True})
    assert loosen.status_code == 422
    assert loosen.json()["error"]["code"] == "policy_loosening_not_allowed"
    assert loosen.json()["error"]["details"]["ancestor"] == company["alias"]
    ttl = await client.put(f"/v1/tenants/{company['tenant']}/policies/token_max_ttl_seconds",
                           headers=owner["headers"], json={"value": 86_400})
    assert ttl.status_code == 200
    effective = (await client.get(f"/v1/tenants/{o2['tenant']}/effective-policy",
                                  headers=owner["headers"])).json()["policy"]
    assert effective["token_max_ttl_seconds"] == {"value": 86_400,
                                                  "source_node": company["alias"],
                                                  "locked": False}
    in_cell = await _switch(client, owner, o2["workspace"])
    issued = await client.post("/v1/tokens", headers=in_cell, json={
        "scopes": ["docs:read"], "purpose": "test", "ttl_seconds": 7 * 86_400,
        "agent": {"agent_id": f"a-{uuid.uuid4().hex[:8]}"}})
    assert issued.status_code == 201, issued.text
    assert "ttl_capped_by_policy" in {item["code"] for item in issued.json()["warnings"]}

    company_human = await _switch(client, owner, company["workspace"])
    current = (await client.get("/v1/documents/system/constitution?include_unverified=true",
                                headers=company["key"]))
    if current.status_code == 404:
        await client.get("/v1/orientation", headers=company["key"])
        current = await client.get("/v1/documents/system/constitution?include_unverified=true",
                                   headers=company["key"])
    saved = await client.put("/v1/human/documents/system/constitution", headers=company_human,
                             json={"content": "# Constitution\n\n## Publishing\n\nExternal "
                                              "publishing needs human approval.\n",
                                   "frontmatter": {"locked_sections": ["Publishing"]},
                                   "expected_version": current.json()["version"],
                                   "lock": False})
    assert saved.status_code == 200, saved.text
    agent = await _agent(client, o2, "cell-bot")
    orientation = (await client.get("/v1/orientation", headers=agent["headers"])).json()
    rules = orientation["constitution"]["inherited"]
    assert {"section": "Publishing", "from": company["alias"], "locked": True}.items() <= \
        next(rule for rule in rules if rule["section"] == "Publishing").items()

    assert (await client.put(f"/v1/tenants/{company['tenant']}/quotas/embedding_tokens",
                             headers=owner["headers"], json={"limit": 1_000_000})).status_code \
        == 200
    assert (await client.put(f"/v1/tenants/{o1['tenant']}/quotas/embedding_tokens",
                             headers=owner["headers"], json={"limit": 600_000})).status_code \
        == 200
    over = await client.put(f"/v1/tenants/{o2['tenant']}/quotas/embedding_tokens",
                            headers=owner["headers"], json={"limit": 700_000})
    assert over.status_code == 422 and over.json()["error"]["code"] == "quota_exceeds_parent"
    assert over.json()["error"]["details"]["parent_limit"] == 600_000

    for node in (o1, o2):
        await client.put("/v1/documents/usage-doc", headers=node["key"],
                         json={"content": "some text to embed", "expected_version": 0})
    usage = (await client.get(f"/v1/tenants/{company['tenant']}/usage",
                              headers=owner["headers"])).json()
    for category, total in usage["total"].items():
        assert total == sum(node["usage"].get(category, 0) for node in usage["nodes"])
    assert len(usage["nodes"]) == 3


async def test_visibility_sharing_and_admin_access(org):
    """AG-ID-12 acceptance 1-4."""
    client = org
    company = await _root(client, "Group", company=True)
    owner = await _person(client, company, "groupowner")
    o1 = _node(await _child(client, owner, company["tenant"], "Left"))
    o2 = _node(await _child(client, owner, company["tenant"], "Right"))
    left_owner = await _person(client, o1, "leftowner")
    secret = await client.put("/v1/documents/left-secret", headers=o1["key"],
                              json={"content": "left plans only", "expected_version": 0})
    secret_id = secret.json()["document"]["canonical_id"]
    right_agent = await _agent(client, o2, "right-bot")
    for ref in ("left-secret", secret_id) * 5:
        probe = await client.get(f"/v1/documents/{ref}?include_unverified=true",
                                 headers=right_agent["headers"])
        assert probe.status_code == 404

    in_left = await _switch(client, owner, o1["workspace"])
    listed = await client.get("/v1/documents", headers=in_left)
    assert listed.status_code == 200  # metadata
    blocked = await client.get("/v1/documents/left-secret?include_unverified=true",
                               headers=in_left)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "admin_access_required"
    opened = await client.post(f"/v1/tenants/{o1['tenant']}/access-sessions",
                               headers=owner["headers"],
                               json={"reason": "quarterly audit", "duration_s": 3600})
    assert opened.status_code == 201, opened.text
    allowed = await client.get("/v1/documents/left-secret?include_unverified=true",
                               headers=in_left)
    assert allowed.status_code == 200
    events = await _sql("SELECT targets FROM workspace_events WHERE workspace_id = $1::uuid "
                        "AND type = 'tenant.admin_access'", o1["workspace"])
    assert f"human:{left_owner['id']}" in json.loads(events[0]["targets"])
    audit = await _sql("SELECT action FROM access_audit WHERE workspace_id = $1::uuid AND "
                       "action LIKE 'admin_access.%'", o1["workspace"])
    assert {"admin_access.open", "admin_access.read"} <= {row["action"] for row in audit}

    shared = await client.post("/v1/shares", headers=left_owner["headers"], json={
        "resource_kind": "document", "resource_ref": "left-secret",
        "grantee": f"node:{o2['tenant']}", "access": "read"})
    assert shared.status_code == 201, shared.text
    hits = (await client.post("/v1/query", headers=right_agent["headers"], json={
        "query": "left plans", "include_shared": True, "include_unverified": True})).json()
    hit = next(item for item in hits["items"] if item["doc_key"] == "left-secret")
    assert hit["tenant_alias"] == o1["alias"]
    read = await client.get(f"/v1/documents/{secret_id}", headers=right_agent["headers"])
    assert read.status_code == 200, read.text
    assert read.json()["tenant_alias"] == o1["alias"]
    write = await client.put(f"/v1/documents/{secret_id}", headers=right_agent["headers"],
                             json={"content": "overwrite", "expected_version": 1})
    assert write.status_code == 403
    revoked = await client.delete(f"/v1/shares/{shared.json()['share_id']}",
                                  headers=left_owner["headers"])
    assert revoked.status_code == 200
    gone = await client.get(f"/v1/documents/{secret_id}", headers=right_agent["headers"])
    assert gone.status_code == 404

    stranger = await _root(client, "Stranger")
    cross = await client.post("/v1/shares", headers=left_owner["headers"], json={
        "resource_kind": "document", "resource_ref": "left-secret",
        "grantee": f"node:{stranger['tenant']}"})
    assert cross.status_code == 422
    assert cross.json()["error"]["code"] == "share_cross_root_not_allowed"


async def test_node_lifecycle(org):
    """AG-ID-13 acceptance 1 (export), 2, 4, 5."""
    client = org
    company = await _root(client, "Parent", company=True)
    owner = await _person(client, company, "parentowner")
    o1 = _node(await _child(client, owner, company["tenant"], "Branch"))
    o2 = _node(await _child(client, owner, o1["tenant"], "Leaf"))
    await client.put("/v1/documents/branch-doc", headers=o1["key"],
                     json={"content": "branch", "expected_version": 0})

    exported = await client.post("/v1/exports", headers=owner["headers"], json={
        "scope": "tenant", "scope_ref": o1["tenant"], "include_subtree": True})
    assert exported.status_code in (200, 201, 202), exported.text
    export_id = exported.json()["export_id"]
    status = None
    for _ in range(60):
        status = (await client.get(f"/v1/exports/{export_id}", headers=owner["headers"])).json()
        if status["status"] == "done":
            break
        await asyncio.sleep(0.5)
    assert status["status"] == "done"

    refused = await client.post(f"/v1/tenants/{o1['tenant']}/delete", headers=owner["headers"],
                                json={"confirm": o1["alias"]})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "tenant_has_children"
    deleting = await client.post(f"/v1/tenants/{o1['tenant']}/delete", headers=owner["headers"],
                                 json={"confirm": o1["alias"], "cascade": True})
    assert deleting.status_code == 200, deleting.text
    assert set(deleting.json()["nodes"]) == {o1["tenant"], o2["tenant"]}
    assert deleting.json()["final_export_id"]
    restored = await client.post(f"/v1/tenants/{o1['tenant']}/restore", headers=owner["headers"])
    assert set(restored.json()["restored"]) == {o1["tenant"], o2["tenant"]}
    states = await _sql("SELECT status FROM tenants WHERE id = ANY($1::text[])",
                        [o1["tenant"], o2["tenant"]])
    assert {row[0] for row in states} == {"active"}

    member = await _person(client, o1, "leaver", role="REVIEWER")
    space = _node(await _child(client, owner, o1["tenant"], "Leaver space", "personal"))
    await _sql("INSERT INTO memberships (tenant_id, principal_id, role) VALUES ($1, $2::uuid, "
               "'owner')", space["tenant"], member["id"])
    removed = await client.delete(f"/v1/tenants/{o1['tenant']}/members/{member['id']}",
                                  headers=owner["headers"])
    assert removed.status_code == 200, removed.text
    assert removed.json()["personal_spaces_handed_over"] == [space["tenant"]]
    owners = await _sql("SELECT principal_id::text FROM memberships WHERE tenant_id = $1 AND "
                        "role = 'owner'", space["tenant"])
    assert owner["id"] in {row[0] for row in owners}
    login = await client.post("/v1/auth/login", json={"email": member["email"],
                                                      "password": PASSWORD})
    if login.status_code == 200:
        denied = await client.post("/v1/auth/switch-workspace",
                                   headers=_bearer(login.json()["token"]),
                                   json={"workspace_id": space["workspace"]})
        assert denied.status_code == 404

    detached = await client.post(f"/v1/tenants/{o1['tenant']}/detach", headers=owner["headers"],
                                 json={})
    assert detached.status_code == 200, detached.text
    body = detached.json()
    assert body["tenant"]["root_tenant_id"] == o1["tenant"]
    assert body["handover_export_id"]
    tree = (await client.get(f"/v1/tenants/{company['tenant']}/tree",
                             headers=owner["headers"])).json()
    assert tree["root"], tree
    assert o1["alias"] not in {item["alias"] for item in tree["root"]["children"]}
    gone = await client.post("/v1/auth/switch-workspace", headers=owner["headers"],
                             json={"workspace_id": o1["workspace"]})
    assert gone.status_code == 404
