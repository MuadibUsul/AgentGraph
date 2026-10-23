"""Tenant export and import (PRD AG-ST-02): storage format, round trip, increments."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import uuid
import zipfile

import asyncpg
import pytest

from app.database import async_session_factory
from app.services import exports as export_service

pytestmark = pytest.mark.integration


def _db() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def _owner(client, admin_headers, workspace):
    email = f"export-{uuid.uuid4().hex[:8]}@example.test"
    await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/human-users",
        headers=admin_headers,
        json={"email": email, "display_name": "Owner", "role": "OWNER",
              "password": "correct-horse-battery"},
    )
    login = await client.post(
        "/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    return {"Authorization": f"Bearer {login.json()['token']}"}


async def _put(client, headers, key, content, version):
    response = await client.put(
        f"/v1/documents/{key}", headers=headers,
        json={"content": content, "expected_version": version,
              "frontmatter": {"kind": "note", "n": version}},
    )
    assert response.status_code == 200, response.text
    return response.json()["document"]


async def _seed(client, workspace):
    headers = workspace["headers"]
    keys = {}
    for name in ("alpha", "beta", "gamma", "delta"):
        key = f"exp/{name}-{uuid.uuid4().hex[:4]}"
        await _put(client, headers, key, f"# {name}\n\nfirst body of {name}\n", 0)
        keys[name] = key
    await _put(client, headers, keys["alpha"], "# alpha\n\nsecond body\n", 1)
    await _put(client, headers, keys["alpha"], "# alpha\n\nthird body ✓\n", 2)
    long_body = "x" * 300_000
    await _put(client, headers, keys["delta"], long_body, 1)
    task = await client.post("/v1/tasks", headers=headers, json={"title": "exported task"})
    assert task.status_code == 201
    return keys


async def _export(client, headers, **body):
    response = await client.post("/v1/exports", headers=headers, json=body)
    assert response.status_code == 202, response.text
    export_id = response.json()["export_id"]
    for _ in range(300):
        status = (await client.get(f"/v1/exports/{export_id}", headers=headers)).json()
        if status["status"] in ("done", "failed"):
            break
        await asyncio.sleep(0.2)
    assert status["status"] == "done", status
    archive = await client.get(f"/v1/exports/{export_id}/download", headers=headers)
    assert archive.status_code == 200
    return status, archive.content


async def _quiet(workspace_id: str) -> None:
    """Record today's self-recall probe for the workspace, so the reaper's
    probe does not run mid-test: its searches meter usage_ledger rows that
    land between the export and the comparison snapshot."""
    conn = await asyncpg.connect(_db())
    try:
        await conn.execute(
            "INSERT INTO retrieval_probes (workspace_id, sampled, hits, self_recall) "
            "VALUES ($1::uuid, 0, 0, 0)", workspace_id)
    finally:
        await conn.close()


async def _settled(workspace_id: str) -> None:
    """Wait until asynchronous bookkeeping (usage rows) stops arriving, so the
    export and the comparison snapshot see the same rows."""
    conn = await asyncpg.connect(_db())
    try:
        last = None
        for _ in range(40):
            count = await conn.fetchval(
                "SELECT (SELECT count(*) FROM usage_ledger WHERE workspace_id = $1::uuid) "
                "+ (SELECT count(*) FROM token_access_log WHERE workspace_id = $1::uuid)",
                workspace_id)
            if count == last:
                return
            last = count
            await asyncio.sleep(0.5)
    finally:
        await conn.close()


async def _snapshot(workspace_id: str) -> dict[str, list]:
    """Every exported row of the workspace and its tenant, secrets excluded."""
    conn = await asyncpg.connect(_db())
    try:
        tenant = await conn.fetchval(
            "SELECT tenant_id FROM workspaces WHERE id = $1::uuid", workspace_id
        )
        snapshot = {}
        for table, (_, excluded) in export_service.EXPORTED.items():
            drop = " - ".join(["to_jsonb(t)", *[f"'{column}'" for column in excluded]])
            if table == "tenants":
                where, arg = "t.id = $1", tenant
            elif table in ("memberships", "redaction_log"):
                where, arg = "t.tenant_id = $1", tenant
            elif table == "workspaces":
                where, arg = "t.id = $1::uuid", workspace_id
            else:
                where, arg = "t.workspace_id = $1::uuid", workspace_id
            if table == "state_audit_diffs":
                where += " AND t.target_table <> 'imports'"
            rows = await conn.fetch(f"SELECT {drop} AS row FROM {table} t WHERE {where}", arg)
            snapshot[table] = sorted(json.loads(row["row"]) and row["row"] for row in rows)
        return snapshot
    finally:
        await conn.close()


async def _erase(workspace_id: str) -> None:
    """Remove every trace of the workspace and its tenant, as on a clean target."""
    conn = await asyncpg.connect(_db())
    try:
        tenant = await conn.fetchval(
            "SELECT tenant_id FROM workspaces WHERE id = $1::uuid", workspace_id
        )
        async with conn.transaction():
            await conn.execute("SET LOCAL session_replication_role = replica")
            for table in ("document_chunks", "document_index", "human_sessions",
                          "budget_buckets", "budget_backoff", "budget_circuits"):
                await conn.execute(f"DELETE FROM {table} WHERE workspace_id = $1::uuid",
                                   workspace_id)
            for table in reversed(list(export_service.EXPORTED)):
                if table == "tenants":
                    await conn.execute("DELETE FROM tenants WHERE id = $1", tenant)
                elif table in ("memberships", "redaction_log"):
                    await conn.execute(f"DELETE FROM {table} WHERE tenant_id = $1", tenant)
                elif table == "workspaces":
                    await conn.execute("DELETE FROM workspaces WHERE id = $1::uuid", workspace_id)
                else:
                    await conn.execute(f"DELETE FROM {table} WHERE workspace_id = $1::uuid",
                                       workspace_id)
    finally:
        await conn.close()


async def _import(client, admin_headers, data, target="new_tenant", verify=True):
    return await client.post(
        "/v1/admin/imports", headers={**admin_headers, "Content-Type": "application/zip"},
        params={"target": target, "verify": str(verify).lower()}, content=data,
    )


async def test_every_table_is_classified():
    async with async_session_factory() as session:
        assert await export_service.unclassified_tables(session) == []


async def test_full_round_trip_is_exact(client, admin_headers, workspace):
    workspace_id = workspace["workspace"]["id"]
    await _quiet(workspace_id)
    owner = await _owner(client, admin_headers, workspace)
    keys = await _seed(client, workspace)
    await _settled(workspace_id)
    status, data = await _export(client, owner, scope="tenant")
    before = await _snapshot(workspace_id)

    archive = zipfile.ZipFile(io.BytesIO(data))
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["format"] == "agentgraph-export/1" and manifest["kind"] == "full"
    assert status["manifest_sha256"] == hashlib.sha256(archive.read("manifest.json")).hexdigest()
    documents = {entry["alias"]: entry for entry in manifest["documents"]}
    assert set(keys.values()) <= set(documents)
    assert len(manifest["documents"]) == len(before["shared_documents"])
    for name, meta in manifest["files"].items():
        assert hashlib.sha256(archive.read(name)).hexdigest() == meta["sha256"], name
    alpha = archive.read(documents[keys["alpha"]]["path"]).decode()
    assert alpha.endswith("# alpha\n\nthird body ✓\n")
    versions = [json.loads(line) for line in archive.read("versions.jsonl").decode().splitlines()]
    assert all(f"blobs/{item['blob']}" in archive.namelist() for item in versions)
    assert not any("embedding" in json.loads(line)["row"]
                   for line in archive.read("objects.jsonl").decode().splitlines()
                   if json.loads(line)["table"] == "shared_documents")

    collision = await _import(client, admin_headers, data)
    assert collision.status_code == 409 and collision.json()["error"]["code"] == "id_collision"

    await _erase(workspace_id)
    imported = await _import(client, admin_headers, data)
    assert imported.status_code == 201, imported.text
    body = imported.json()
    assert body["workspaces"][0]["id"] == workspace_id and body["workspaces"][0]["api_key"]
    after = await _snapshot(workspace_id)
    for table in before:
        assert after[table] == before[table], table

    # The new key works and the content is byte-exact.
    new_headers = {"Authorization": f"Bearer {body['workspaces'][0]['api_key']}"}
    read = await client.get(f"/v1/documents/{keys['delta']}", headers=new_headers)
    assert read.status_code == 200 and read.json()["content"] == "x" * 300_000


async def test_tampered_archive_is_rejected_whole(client, admin_headers, workspace):
    owner = await _owner(client, admin_headers, workspace)
    keys = await _seed(client, workspace)
    _, data = await _export(client, owner, scope="tenant")
    source = zipfile.ZipFile(io.BytesIO(data))
    manifest = json.loads(source.read("manifest.json"))
    target_path = next(entry["path"] for entry in manifest["documents"]
                       if entry["alias"] == keys["beta"])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as tampered:
        for name in source.namelist():
            content = source.read(name)
            if name == target_path:
                content = content.replace(b"first body", b"FORGED body")
            tampered.writestr(name, content)
    workspace_id = workspace["workspace"]["id"]
    await _erase(workspace_id)
    refused = await _import(client, admin_headers, buffer.getvalue())
    assert refused.status_code == 422
    error = refused.json()["error"]
    assert error["code"] == "checksum_mismatch"
    assert {item["file"] for item in error["details"]["mismatches"]} == {target_path}
    conn = await asyncpg.connect(_db())
    try:
        assert await conn.fetchval(
            "SELECT count(*) FROM workspaces WHERE id = $1::uuid", workspace_id) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM shared_documents WHERE workspace_id = $1::uuid",
            workspace_id) == 0
    finally:
        await conn.close()


async def test_increment_chain_equals_the_full_state(client, admin_headers, workspace):
    await _quiet(workspace["workspace"]["id"])
    owner = await _owner(client, admin_headers, workspace)
    keys = await _seed(client, workspace)
    headers = workspace["headers"]
    # Purging needs an archived document; archive it before the baseline.
    archived = await client.post(f"/v1/documents/{keys['alpha']}/archive", headers=headers,
                                 json={})
    assert archived.status_code == 200, archived.text
    first, base = await _export(client, owner, scope="tenant")
    await _put(client, headers, keys["beta"], "# beta\n\nchanged\n", 1)
    await _put(client, headers, keys["gamma"], "# gamma\n\nchanged\n", 1)
    created = f"exp/new-{uuid.uuid4().hex[:4]}"
    await _put(client, headers, created, "# new\n", 0)
    renamed = f"exp/renamed-{uuid.uuid4().hex[:4]}"
    response = await client.post(f"/v1/documents/{keys['delta']}/rename", headers=headers,
                                 json={"new_alias": renamed})
    assert response.status_code == 200, response.text
    purged = await client.post(f"/v1/human/documents/{keys['alpha']}/purge", headers=owner,
                               json={"reason": "test", "confirm": keys["alpha"]})
    assert purged.status_code == 200, purged.text
    second, increment = await _export(client, owner, scope="tenant",
                                      since_export_id=first["export_id"])
    assert second["kind"] == "incremental" and second["prev_export_id"] == first["export_id"]
    assert second["seq_from"] == first["seq_to"] + 1
    workspace_id = workspace["workspace"]["id"]
    state = await _snapshot(workspace_id)

    archive = zipfile.ZipFile(io.BytesIO(increment))
    manifest = json.loads(archive.read("manifest.json"))
    changed = {entry["alias"] for entry in manifest["documents"]}
    assert changed == {keys["beta"], keys["gamma"], created, renamed}
    assert manifest["counts"]["tombstones"] == 1
    deleted = [json.loads(line) for line in archive.read("deleted.jsonl").decode().splitlines()]
    assert [item["table"] for item in deleted].count("shared_documents") == 1

    await _erase(workspace_id)
    assert (await _import(client, admin_headers, base)).status_code == 201
    early = await _import(client, admin_headers, increment, target="new_tenant")
    assert early.status_code == 422  # increments are applied with target=increment
    applied = await _import(client, admin_headers, increment, target="increment")
    assert applied.status_code == 201, applied.text
    after = await _snapshot(workspace_id)
    for table in state:
        assert after[table] == state[table], table


async def test_agents_cannot_export(client, admin_headers, workspace):
    response = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        headers=admin_headers,
        json={"agent_signature": f"exporter_{uuid.uuid4().hex[:6]}", "role_scope": "EXECUTOR"},
    )
    agent = {"Authorization": f"Bearer {response.json()['api_key']}"}
    refused = await client.post("/v1/exports", headers=agent, json={"scope": "workspace"})
    assert refused.status_code == 403
    tenant = await client.post("/v1/exports", headers=workspace["headers"],
                               json={"scope": "tenant"})
    assert tenant.status_code == 403  # the workspace key exports its workspace only
    own = await client.post("/v1/exports", headers=workspace["headers"],
                            json={"scope": "workspace"})
    assert own.status_code == 202
    busy = await client.post("/v1/exports", headers=workspace["headers"],
                             json={"scope": "workspace"})
    assert busy.status_code in (202, 409)
    if busy.status_code == 409:
        assert busy.json()["error"]["code"] == "export_in_progress"


async def test_confirmed_deletion_takes_a_final_export(client, admin_headers, workspace):
    owner = await _owner(client, admin_headers, workspace)
    await _seed(client, workspace)
    tenant = (await client.get("/v1/human/tenant", headers=owner)).json()
    await client.post(f"/v1/admin/tenants/{tenant['id']}/delete", headers=admin_headers,
                      json={"confirm": tenant["alias"]})
    confirmed = await client.post("/v1/human/tenant/delete", headers=owner,
                                  json={"confirm": tenant["alias"]})
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "deleting"
    export_id = confirmed.json()["final_export_id"]
    assert export_id
    for _ in range(300):
        status = (await client.get("/v1/human/tenant/final-export", headers=owner)).json()
        if status["status"] in ("done", "failed"):
            break
        await asyncio.sleep(0.2)
    assert status["status"] == "done" and status["purpose"] == "final"
    archive = await client.get("/v1/human/tenant/final-export", headers=owner,
                               params={"download": "true"})
    assert archive.status_code == 200
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(archive.content)).read("manifest.json"))
    assert manifest["scope"] == "tenant" and manifest["kind"] == "full"
    restored = await client.post("/v1/human/tenant/restore", headers=owner, json={})
    assert restored.status_code == 200
