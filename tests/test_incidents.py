"""Security incidents: scope, 72-hour notices, append-only record (PRD AG-TC-06)."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
import pytest

from tests.conftest import run_server

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}
PASSWORD = "correct-horse-battery"


def _dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.fixture(scope="module")
async def ops(api_base_url) -> AsyncIterator[httpx.AsyncClient]:
    del api_base_url
    async with run_server({
        "PLATFORM_ADMIN_EMAIL": "ops@example.test",
        "ACCESS_LOG_FLUSH_SECONDS": "0.5",
        "INCIDENT_CHECK_INTERVAL_SECONDS": "3600",
    }, ".server-incidents.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=120) as http:
            yield http


async def _owner(client, workspace_id: str, label: str) -> dict:
    email = f"{label}-{uuid.uuid4().hex[:8]}@example.test"
    created = await client.post(
        f"/v1/admin/workspaces/{workspace_id}/human-users", headers=ADMIN,
        json={"email": email, "display_name": label, "role": "OWNER", "password": PASSWORD},
    )
    assert created.status_code in (200, 201), created.text
    login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    return {"email": email, "id": created.json()["user"]["id"],
            "headers": {"Authorization": f"Bearer {login.json()['token']}"}}


async def _tree(client, label: str) -> list[dict]:
    """root, child a, child b, grandchild a1: four nodes, one owner each."""
    root = await client.post("/v1/admin/workspaces", headers=ADMIN,
                             json={"name": f"{label} {uuid.uuid4().hex[:6]}", "tier": "pro"})
    assert root.status_code in (200, 201), root.text
    body = root.json()
    nodes = [{"name": f"{label}0", "tenant": body["workspace"]["tenant_id"],
              "workspace": body["workspace"]["id"],
              "headers": {"Authorization": f"Bearer {body['api_key']}"}}]
    for name, parent in ((f"{label}1", 0), (f"{label}2", 0), (f"{label}3", 1)):
        child = await client.post(
            f"/v1/admin/tenants/{nodes[parent]['tenant']}/children", headers=ADMIN,
            json={"display_name": name, "tier": "pro"},
        )
        assert child.status_code == 201, child.text
        data = child.json()
        assert data["tenant"]["parent_tenant_id"] == nodes[parent]["tenant"]
        nodes.append({"name": name, "tenant": data["tenant"]["id"],
                      "workspace": data["workspace"]["id"],
                      "headers": {"Authorization": f"Bearer {data['api_key']}"}})
    for node in nodes:
        node["owner"] = await _owner(client, node["workspace"], node["name"].lower())
        seeded = await client.put("/v1/documents/seed", headers=node["headers"],
                                  json={"namespace": "notes", "content": f"seed {node['name']}",
                                        "expected_version": 0})
        assert seeded.status_code in (200, 201), seeded.text
    return nodes


async def _leak(client, node: dict) -> str:
    minted = await client.post(
        f"/v1/admin/workspaces/{node['workspace']}/agent-tokens", headers=ADMIN,
        json={"agent_signature": f"runner_{uuid.uuid4().hex[:6]}", "role_scope": "ANALYST",
              "allowed_namespaces": ["*"]},
    )
    assert minted.status_code in (200, 201), minted.text
    token = {"Authorization": f"Bearer {minted.json()['api_key']}"}
    read = await client.get("/v1/documents/seed?include_unverified=true", headers=token)
    assert read.status_code == 200, read.text
    wrote = await client.put("/v1/documents/leaked-note", headers=token,
                             json={"namespace": "notes", "content": "x", "expected_version": 0})
    assert wrote.status_code in (200, 201), wrote.text
    return minted.json()["token"]["canonical_id"]


async def test_scope_notices_record_and_isolation(ops):
    client = ops
    started = datetime.now(timezone.utc) - timedelta(seconds=2)
    tree_a, tree_b, tree_c = (await _tree(client, "A"), await _tree(client, "B"),
                              await _tree(client, "C"))
    leaked_nodes = [tree_a[0], tree_a[3], tree_b[1], tree_b[2]]
    tokens = [await _leak(client, node) for node in leaked_nodes]

    created = await client.post("/v1/admin/incidents", headers=ADMIN, json={
        "title": "Runner credentials leaked", "severity": "high",
        "summary": "Agent tokens from one runner host were exposed in a public log.",
        "measures_taken": "The tokens were revoked and the host rebuilt.",
        "contact": "security@agentgraph.example",
    })
    assert created.status_code == 201, created.text
    incident = created.json()["incident_id"]
    scope = await client.post(f"/v1/admin/incidents/{incident}/scope", headers=ADMIN, json={
        "scope_inputs": {"token_ids": tokens, "time_from": started.isoformat(),
                         "time_to": (datetime.now(timezone.utc)
                                     + timedelta(minutes=1)).isoformat()},
    })
    assert scope.status_code == 200, scope.text
    nodes = {item["tenant_id"]: item for item in scope.json()["nodes"]}
    assert set(nodes) == {node["tenant"] for node in leaked_nodes}
    for item in nodes.values():
        # One read (seed) and one write (leaked-note) by one token and principal.
        assert item["affected_objects"] == {"documents": 2, "tasks": 0, "principals": 1,
                                            "tokens": 1}
        assert item["may_include_personal_info"] is True
        assert item["evidence_refs"]
    roots = {item["root_tenant_id"] for item in scope.json()["roots"]}
    assert roots == {tree_a[0]["tenant"], tree_b[0]["tenant"]}

    early = await client.post(f"/v1/admin/incidents/{incident}/notices", headers=ADMIN, json={})
    assert early.status_code == 409
    assert early.json()["error"]["code"] == "incident_not_confirmed"
    assert early.json()["error"]["details"]["confirm_with"].endswith("/confirm")
    confirmed = await client.post(f"/v1/admin/incidents/{incident}/confirm", headers=ADMIN)
    assert confirmed.json()["notify_deadline"]
    incomplete = await client.post(f"/v1/admin/incidents/{incident}/notices", headers=ADMIN,
                                   json={})
    assert incomplete.status_code == 422
    assert incomplete.json()["error"]["code"] == "notice_template_incomplete"
    assert incomplete.json()["error"]["details"]["missing"] == ["recommended_actions"]
    await client.patch(f"/v1/admin/incidents/{incident}", headers=ADMIN,
                       json={"recommended_actions": "Rotate any credentials stored with "
                                                    "your agents and review recent writes."})
    contacts = await client.put("/v1/human/tenant/security-contacts",
                                headers=tree_a[0]["owner"]["headers"],
                                json={"emails": ["sec@tree-a.example"]})
    assert contacts.status_code == 200, contacts.text
    not_root = await client.put("/v1/human/tenant/security-contacts",
                                headers=tree_a[1]["owner"]["headers"],
                                json={"emails": ["x@y.example"]})
    assert not_root.status_code == 403

    conn = await asyncpg.connect(_dsn())
    try:
        preview = await client.post(f"/v1/admin/incidents/{incident}/notices", headers=ADMIN,
                                    json={"dry_run": True})
        assert preview.status_code == 200, preview.text
        assert preview.json()["dry_run"] is True and preview.json()["notices"] == 10
        assert await conn.fetchval(
            "SELECT count(*) FROM incident_notices WHERE incident_id = $1", incident) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM email_outbox WHERE ref = $1 AND to_email = "
            "'ops@example.test'", incident) == 1
        alerts = (await client.get("/v1/admin/alerts?kind=incident.dry_run",
                                   headers=ADMIN)).json()["items"]
        assert any(item["details"]["incident_id"] == incident for item in alerts)
        quiet = await client.get("/v1/human/incident-notices",
                                 headers=tree_a[3]["owner"]["headers"])
        assert quiet.json()["items"] == []

        sent = await client.post(f"/v1/admin/incidents/{incident}/notices", headers=ADMIN,
                                 json={})
        assert sent.status_code == 200, sent.text
        # A0: owner + contact; A3: A3, A1 (inherited), A0 (root) + contact;
        # B1 and B2: own owner + root owner.
        assert sent.json()["notices"] == 10 and sent.json()["nodes"] == 4
        rows = await conn.fetch(
            "SELECT tenant_id, recipient, content, content_hash FROM incident_notices "
            "WHERE incident_id = $1", incident)
        assert len(rows) == 10
        per_node = {}
        for row in rows:
            per_node.setdefault(row["tenant_id"], set()).add(
                json.loads(row["recipient"])["email"])
        a0, a1, a3 = (tree_a[i]["owner"]["email"] for i in (0, 1, 3))
        assert per_node[tree_a[0]["tenant"]] == {a0, "sec@tree-a.example"}
        assert per_node[tree_a[3]["tenant"]] == {a3, a1, a0, "sec@tree-a.example"}
        assert per_node[tree_b[1]["tenant"]] == {tree_b[1]["owner"]["email"],
                                                 tree_b[0]["owner"]["email"]}
        tree_b_ids = {node["tenant"] for node in tree_b}
        tree_a_ids = {node["tenant"] for node in tree_a}
        for row in rows:
            body = row["content"]
            other = tree_b_ids if row["tenant_id"] in tree_a_ids else tree_a_ids
            assert not any(tenant in body for tenant in other)
            content = json.loads(body)
            for field in ("nature", "detected_at", "confirmed_at", "impact", "measures_taken",
                          "recommended_actions", "contact"):
                assert content[field] not in (None, "")
            assert "may_include_personal_info" in content["impact"]
        root_notice = json.loads(next(r["content"] for r in rows
                                      if r["tenant_id"] == tree_a[0]["tenant"]))
        assert {item["tenant_id"] for item in root_notice["impact"]["affected_nodes"]} == {
            tree_a[0]["tenant"], tree_a[3]["tenant"]}
        assert await conn.fetchval(
            "SELECT count(*) FROM email_outbox WHERE ref IN (SELECT id FROM incident_notices "
            "WHERE incident_id = $1)", incident) == 10
        event = await conn.fetchrow(
            "SELECT targets FROM workspace_events WHERE workspace_id = $1::uuid "
            "AND type = 'security.incident_notice'", tree_a[3]["workspace"])
        assert f"human:{tree_a[3]['owner']['id']}" in json.loads(event["targets"])

        mine = (await client.get("/v1/human/incident-notices",
                                 headers=tree_a[3]["owner"]["headers"])).json()
        assert mine["banner"]["incident_id"] == incident
        assert all(item["tenant_id"] == tree_a[3]["tenant"] for item in mine["items"])
        assert (await client.get("/v1/human/incident-notices",
                                 headers=tree_c[0]["owner"]["headers"])).json()["items"] == []

        again = await client.post(f"/v1/admin/incidents/{incident}/notices", headers=ADMIN,
                                  json={})
        assert again.json()["notices"] == 0  # every node was already notified

        notice_id = sent.json()["items"][0]["notice_id"]
        changed = await client.patch(f"/v1/admin/incidents/{incident}/notices/{notice_id}",
                                     headers=ADMIN)
        assert changed.status_code == 403 and changed.json()["error"]["code"] == "forbidden"
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute("UPDATE incident_notices SET content = '{}'::jsonb "
                               "WHERE id = $1", notice_id)

        update = await client.post(f"/v1/admin/incidents/{incident}/updates", headers=ADMIN,
                                   json={"message": "Forensics found no further access."})
        assert update.json()["notices"] == 10
        assert await conn.fetchval(
            "SELECT count(*) FROM incident_notices WHERE incident_id = $1 AND kind = 'initial' "
            "AND superseded_by IS NOT NULL", incident) == 10
    finally:
        await conn.close()

    record = (await client.get(f"/v1/admin/incidents/{incident}/record", headers=ADMIN)).json()
    kinds = [item["kind"] for item in record["timeline"]]
    for kind in ("created", "scope_resolved", "confirmed", "notices_dry_run", "notices_sent",
                 "update_sent"):
        assert kind in kinds
    assert record["scope"]["inputs"]["token_ids"] == sorted(tokens)
    assert len(record["scope"]["nodes"]) == 4
    assert len(record["notices"]) == 20
    assert all(item["content_hash"] and item["delivery_status"]["email"]["status"]
               for item in record["notices"])


async def test_sla_checkpoints_and_breach(ops):
    client = ops
    tree = await _tree(client, "S")
    doc = await client.get("/v1/documents/seed", headers=tree[2]["headers"])
    created = (await client.post("/v1/admin/incidents", headers=ADMIN, json={
        "title": "Shard exposure", "severity": "medium", "summary": "s",
        "measures_taken": "m", "recommended_actions": "r", "contact": "c",
    })).json()
    incident = created["incident_id"]
    scope = await client.post(f"/v1/admin/incidents/{incident}/scope", headers=ADMIN, json={
        "scope_inputs": {"object_ids": [doc.json()["id"]]}})
    assert [item["tenant_id"] for item in scope.json()["nodes"]] == [tree[2]["tenant"]]
    await client.post(f"/v1/admin/incidents/{incident}/confirm", headers=ADMIN)
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("UPDATE incidents SET confirmed_at = NOW() - INTERVAL '49 hours' "
                           "WHERE id = $1", incident)
        checked = (await client.post("/v1/admin/incidents/check-sla", headers=ADMIN)).json()
        assert checked["reminders"] >= 2 and checked["breaches"] == 0
        alerts = (await client.get("/v1/admin/alerts?kind=incident.sla_checkpoint",
                                   headers=ADMIN)).json()["items"]
        hours = sorted(item["details"]["checkpoint_hours"] for item in alerts
                       if item["details"]["incident_id"] == incident)
        assert hours == [24, 48]
        await client.post("/v1/admin/incidents/check-sla", headers=ADMIN)  # idempotent
        alerts = (await client.get("/v1/admin/alerts?kind=incident.sla_checkpoint",
                                   headers=ADMIN)).json()["items"]
        assert len([a for a in alerts if a["details"]["incident_id"] == incident]) == 2
        await conn.execute("UPDATE incidents SET confirmed_at = NOW() - INTERVAL '73 hours' "
                           "WHERE id = $1", incident)
        await client.post("/v1/admin/incidents/check-sla", headers=ADMIN)
        assert await conn.fetchval(
            "SELECT sla_breached FROM incident_nodes WHERE incident_id = $1", incident) is True
    finally:
        await conn.close()
    detail = (await client.get(f"/v1/admin/incidents/{incident}", headers=ADMIN)).json()
    assert detail["nodes"][0]["sla_breached"] is True
    record = (await client.get(f"/v1/admin/incidents/{incident}/record", headers=ADMIN)).json()
    assert {"incident.sla_checkpoint", "incident.sla_breached"} <= {
        item["kind"] for item in record["alerts"]}


async def test_incidents_are_admin_only(ops):
    client = ops
    assert (await client.get("/v1/admin/incidents")).status_code == 401
    tree = await _tree(client, "Z")
    response = await client.get("/v1/admin/incidents", headers=tree[0]["headers"])
    assert response.status_code in (401, 403)
    bad = await client.post("/v1/admin/incidents", headers=ADMIN,
                            json={"title": "x", "severity": "huge"})
    assert bad.status_code == 422


async def test_outbox_records_delivery_honestly(ops):
    """Without SMTP the outbox marks mail no_transport instead of pretending."""
    conn = await asyncpg.connect(_dsn())
    try:
        mail_id = await conn.fetchval(
            "INSERT INTO email_outbox (to_email, subject, body) VALUES "
            "('x@example.test', 's', 'b') RETURNING id")
        status = "queued"
        for _ in range(40):
            status = await conn.fetchval("SELECT status FROM email_outbox WHERE id = $1",
                                         mail_id)
            if status != "queued":
                break
            await asyncio.sleep(0.5)
        assert status == "no_transport"
    finally:
        await conn.close()
