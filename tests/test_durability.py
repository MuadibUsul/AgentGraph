"""Backups, point-in-time restore to staging, restore drills (PRD AG-ST-10)."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import httpx
import pytest
from sqlalchemy.engine import make_url

from tests.conftest import run_server

pytestmark = pytest.mark.integration


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _staging_url() -> str:
    url = make_url(os.environ["TEST_DATABASE_URL"])
    return url.set(database=f"{url.database}_staging").render_as_string(hide_password=False)


@pytest.fixture(scope="module")
async def backup_client(tmp_path_factory, api_base_url) -> AsyncIterator[httpx.AsyncClient]:
    del api_base_url  # the main server has migrated the database already
    staging = _staging_url()
    admin = await asyncpg.connect(_dsn(os.environ["TEST_DATABASE_URL"]))
    try:
        name = make_url(staging).database
        exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name)
        if not exists:
            await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    conn = await asyncpg.connect(_dsn(staging))
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        await conn.close()
    backup_dir = tmp_path_factory.mktemp("backups")
    async with run_server({
        "BACKUP_DIR": str(backup_dir),
        "BACKUP_SCHEDULER_ENABLED": "false",
        "STAGING_DATABASE_URL": staging,
        "ACCESS_LOG_FLUSH_SECONDS": "0.5",
    }, ".server-backups.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=180) as http:
            http.backup_dir = backup_dir  # type: ignore[attr-defined]
            yield http


async def _workspace(client, admin_headers) -> dict:
    created = await client.post("/v1/admin/workspaces", headers=admin_headers,
                                json={"name": f"Backup {uuid.uuid4().hex[:6]}", "tier": "pro"})
    assert created.status_code in (200, 201), created.text
    body = created.json()
    return {"id": body["workspace"]["id"], "tenant_id": body["workspace"]["tenant_id"],
            "headers": {"Authorization": f"Bearer {body['api_key']}"}}


async def _write(client, workspace, key: str, content: str) -> dict:
    current = await client.get(f"/v1/documents/{key}", headers=workspace["headers"])
    version = current.json()["version"] if current.status_code == 200 else 0
    response = await client.put(f"/v1/documents/{key}", headers=workspace["headers"],
                                json={"namespace": "notes", "content": content,
                                      "expected_version": version})
    assert response.status_code in (200, 201), response.text
    return response.json()


async def _wait_restore(client, admin_headers, restore_id: str) -> dict:
    for _ in range(600):
        body = (await client.get(f"/v1/admin/restores/{restore_id}", headers=admin_headers)).json()
        if body["status"] != "running":
            return body
        await asyncio.sleep(0.2)
    raise AssertionError("restore did not finish")


async def test_backup_needs_the_admin_and_configuration(client, admin_headers):
    assert (await client.get("/v1/admin/backups")).status_code == 401
    # The main test server has no BACKUP_DIR.
    response = await client.post("/v1/admin/backups", headers=admin_headers, json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "backups_not_configured"
    health = (await client.get("/healthz")).json()
    assert set(health) >= {"status", "last_backup_at", "last_restore_drill_at"}


async def test_full_and_incremental_backup_then_point_in_time_restore(
    backup_client, admin_headers
):
    client = backup_client
    workspace = await _workspace(client, admin_headers)
    for index in range(5):
        await _write(client, workspace, f"pitr-{index}", f"# doc {index}\n")
    full = await client.post("/v1/admin/backups", headers=admin_headers,
                             json={"type": "full", "tenant_ids": [workspace["tenant_id"]]})
    assert full.status_code == 201, full.text
    full = full.json()
    assert full["type"] == "full" and full["status"] == "done" and full["verified_at"]
    assert full["documents"] >= 5 and full["sha256"] and full["bytes"] > 0

    await _write(client, workspace, "pitr-0", "# doc 0, edited\n")
    await _write(client, workspace, "pitr-late", "# written after the full backup\n")
    increment = (await client.post(
        "/v1/admin/backups", headers=admin_headers,
        json={"type": "incremental", "tenant_ids": [workspace["tenant_id"]]},
    )).json()
    assert increment["type"] == "incremental"
    assert increment["base_backup_id"] == full["backup_id"]
    assert increment["prev_backup_id"] == full["backup_id"]
    detail = (await client.get(f"/v1/admin/backups/{increment['backup_id']}",
                               headers=admin_headers)).json()
    entry = detail["entries"][0]
    assert entry["kind"] == "incremental"
    archive = Path(detail["location"]) / entry["file"]
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == entry["sha256"]
    # Backups live outside the database.
    assert str(client.backup_dir) in detail["location"]

    point = datetime.now(timezone.utc)
    await _write(client, workspace, "pitr-after-point", "# not in the restore\n")
    requested = await client.post("/v1/admin/restore", headers=admin_headers, json={
        "point_in_time": point.isoformat(), "target": "staging",
        "tenant_id": workspace["tenant_id"],
    })
    assert requested.status_code == 202, requested.text
    restore = await _wait_restore(client, admin_headers, requested.json()["restore_id"])
    assert restore["status"] == "done", restore
    assert restore["backup_id"] == increment["backup_id"]
    assert restore["chain"] == [full["backup_id"], increment["backup_id"]]

    source = await asyncpg.connect(_dsn(os.environ["TEST_DATABASE_URL"]))
    staging = await asyncpg.connect(_dsn(_staging_url()))
    try:
        query = ("SELECT doc_key, version, content_hash, updated_at FROM shared_documents "
                 "WHERE workspace_id = $1::uuid AND namespace = 'notes' ORDER BY doc_key")
        live = {row["doc_key"]: row for row in await source.fetch(query, workspace["id"])}
        restored = {row["doc_key"]: row for row in await staging.fetch(query, workspace["id"])}
        assert "pitr-after-point" in live and "pitr-after-point" not in restored
        assert set(restored) == set(live) - {"pitr-after-point"}
        for key, row in restored.items():
            assert (row["version"], row["content_hash"]) == (
                live[key]["version"], live[key]["content_hash"])
        newest = max(row["updated_at"] for row in restored.values())
        assert newest >= point - timedelta(minutes=15)
        versions = "SELECT count(*) FROM document_versions WHERE workspace_id = $1::uuid"
        assert await staging.fetchval(versions, workspace["id"]) == await source.fetchval(
            versions + " AND created_at <= $2", workspace["id"], point)
    finally:
        await source.close()
        await staging.close()

    listing = (await client.get("/v1/admin/backups", headers=admin_headers)).json()
    first = listing["items"][0]
    assert {"backup_id", "type", "started_at", "finished_at", "bytes", "sha256",
            "verified_at"} <= set(first)
    health = (await client.get("/healthz")).json()
    last = datetime.fromisoformat(health["last_backup_at"])
    assert datetime.now(timezone.utc) - last <= timedelta(hours=24)


async def test_restore_drill_passes_and_is_recorded(backup_client, admin_headers):
    client = backup_client
    workspace = await _workspace(client, admin_headers)
    for index in range(3):
        await _write(client, workspace, f"drill-{index}", f"drill body {index}")
    backup = await client.post("/v1/admin/backups", headers=admin_headers,
                               json={"type": "full", "tenant_ids": [workspace["tenant_id"]]})
    assert backup.status_code == 201, backup.text
    drill = await client.post("/v1/admin/restore-drills", headers=admin_headers)
    assert drill.status_code == 201, drill.text
    body = drill.json()
    assert body["status"] == "passed", body
    assert body["documents_expected"] == body["documents_restored"] > 0
    assert body["sampled"] == min(100, body["documents_expected"])
    assert body["mismatches"] == []
    drills = (await client.get("/v1/admin/restore-drills", headers=admin_headers)).json()
    assert drills["items"][0]["drill_id"] == body["drill_id"]
    health = (await client.get("/healthz")).json()
    assert health["last_restore_drill_at"] is not None


async def test_restore_never_overwrites_production_by_default(backup_client, admin_headers):
    client = backup_client
    workspace = await _workspace(client, admin_headers)
    await _write(client, workspace, "keep", "production data")
    backup = (await client.post("/v1/admin/backups", headers=admin_headers,
                                json={"type": "full",
                                      "tenant_ids": [workspace["tenant_id"]]})).json()
    unconfirmed = await client.post("/v1/admin/restore", headers=admin_headers, json={
        "backup_id": backup["backup_id"], "target": "in_place",
        "tenant_id": workspace["tenant_id"],
    })
    assert unconfirmed.status_code == 409
    assert unconfirmed.json()["error"]["code"] == "confirmation_required"
    # new_workspace keeps ids, so restoring next to the live copy collides.
    clash = await client.post("/v1/admin/restore", headers=admin_headers, json={
        "backup_id": backup["backup_id"], "target": "new_workspace",
        "tenant_id": workspace["tenant_id"],
    })
    result = await _wait_restore(client, admin_headers, clash.json()["restore_id"])
    assert result["status"] == "failed" and result["error"]["code"] == "id_collision"
    still = await client.get("/v1/documents/keep", headers=workspace["headers"])
    assert still.json()["content"] == "production data"


async def test_in_place_restore_with_confirmation(backup_client, admin_headers):
    client = backup_client
    workspace = await _workspace(client, admin_headers)
    email = f"owner-{uuid.uuid4().hex[:8]}@example.test"
    owner = await client.post(
        f"/v1/admin/workspaces/{workspace['id']}/human-users", headers=admin_headers,
        json={"email": email, "display_name": "O", "role": "OWNER",
              "password": "correct-horse-battery"},
    )
    assert owner.status_code in (200, 201), owner.text
    await _write(client, workspace, "state", "good state")
    backup = (await client.post("/v1/admin/backups", headers=admin_headers,
                                json={"type": "full",
                                      "tenant_ids": [workspace["tenant_id"]]})).json()
    await _write(client, workspace, "state", "corrupted state")
    requested = await client.post("/v1/admin/restore", headers=admin_headers, json={
        "backup_id": backup["backup_id"], "target": "in_place",
        "tenant_id": workspace["tenant_id"], "confirm": workspace["tenant_id"],
    })
    result = await _wait_restore(client, admin_headers, requested.json()["restore_id"])
    assert result["status"] == "done", result
    assert result["result"]["pre_restore_export_id"]
    # The owner can still sign in and sees the restored state.
    login = await client.post("/v1/auth/login",
                              json={"email": email, "password": "correct-horse-battery"})
    assert login.status_code == 200, login.text
    human = {"Authorization": f"Bearer {login.json()['token']}"}
    document = await client.get("/v1/documents/state", headers=human)
    assert document.status_code == 200, document.text
    assert document.json()["content"] == "good state"
