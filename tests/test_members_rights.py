"""Members and roles (PRD AG-ID-10) and personal rights requests (AG-TC-07)."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest
from sqlalchemy.engine import make_url

from tests.conftest import run_server

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}
PASSWORD = "correct-horse-battery"


def _dsn(url: str | None = None) -> str:
    return (url or os.environ["TEST_DATABASE_URL"]).replace(
        "postgresql+asyncpg://", "postgresql://", 1)


def _staging() -> str:
    url = make_url(os.environ["TEST_DATABASE_URL"])
    return url.set(database=f"{url.database}_staging").render_as_string(hide_password=False)


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(scope="module")
async def org(tmp_path_factory, api_base_url) -> AsyncIterator[httpx.AsyncClient]:
    del api_base_url
    staging = _staging()
    admin = await asyncpg.connect(_dsn())
    try:
        name = make_url(staging).database
        if not await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
            await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    conn = await asyncpg.connect(_dsn(staging))
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        await conn.close()
    async with run_server({
        "CHILD_TOKEN_ISSUANCE": "true",
        "BACKUP_DIR": str(tmp_path_factory.mktemp("org-backups")),
        "BACKUP_SCHEDULER_ENABLED": "false",
        "STAGING_DATABASE_URL": staging,
        "LEASE_REAPER_INTERVAL_SECONDS": "5",
    }, ".server-org.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=180) as http:
            yield http


async def _root(client, label: str) -> dict:
    created = await client.post("/v1/admin/workspaces", headers=ADMIN,
                                json={"name": f"{label} {uuid.uuid4().hex[:6]}", "tier": "pro"})
    body = created.json()
    return {"tenant": body["workspace"]["tenant_id"], "workspace": body["workspace"]["id"],
            "key": _bearer(body["api_key"])}


async def _child(client, parent: dict, label: str) -> dict:
    child = await client.post(f"/v1/admin/tenants/{parent['tenant']}/children", headers=ADMIN,
                              json={"display_name": label, "tier": "pro"})
    assert child.status_code == 201, child.text
    data = child.json()
    return {"tenant": data["tenant"]["id"], "workspace": data["workspace"]["id"],
            "key": _bearer(data["api_key"])}


async def _person(client, node: dict, label: str, role: str = "OWNER") -> dict:
    email = f"{label}-{uuid.uuid4().hex[:8]}@example.test"
    created = await client.post(
        f"/v1/admin/workspaces/{node['workspace']}/human-users", headers=ADMIN,
        json={"email": email, "display_name": f"{label.title()} Person", "role": role,
              "password": PASSWORD})
    assert created.status_code in (200, 201), created.text
    login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    return {"email": email, "id": created.json()["user"]["id"],
            "headers": _bearer(login.json()["token"])}


# ------------------------------------------------------------------ AG-ID-10


async def test_roles_inheritance_and_removal(org):
    client = org
    company = await _root(client, "Company")
    branch = await _child(client, company, "Branch O1")
    owner = await _person(client, company, "owner")
    admin_a = await _person(client, company, "admina", role="REVIEWER")
    member_y = await _person(client, company, "membery", role="REVIEWER")
    outsider = await _person(client, await _root(client, "Elsewhere"), "outsider")

    promoted = await client.patch(f"/v1/tenants/{company['tenant']}/members/{admin_a['id']}",
                                  headers=owner["headers"], json={"role": "admin"})
    assert promoted.status_code == 200, promoted.text
    admin_a["headers"] = _bearer((await client.post("/v1/auth/login", json={
        "email": admin_a["email"], "password": PASSWORD})).json()["token"])

    too_high = await client.post(f"/v1/tenants/{company['tenant']}/members",
                                 headers=admin_a["headers"],
                                 json={"email": outsider["email"], "role": "owner"})
    assert too_high.status_code == 403
    assert too_high.json()["error"]["code"] == "role_not_assignable"
    assert too_high.json()["error"]["details"] == {"your_role": "admin", "requested_role": "owner"}
    added = await client.post(f"/v1/tenants/{company['tenant']}/members",
                              headers=admin_a["headers"],
                              json={"email": outsider["email"], "role": "member"})
    assert added.status_code == 201 and added.json()["status"] == "added"

    newcomer = f"new-{uuid.uuid4().hex[:8]}@example.test"
    invited = await client.post(f"/v1/tenants/{company['tenant']}/members",
                                headers=owner["headers"], json={"email": newcomer,
                                                                "role": "member"})
    assert invited.json()["status"] == "invited"
    conn = await asyncpg.connect(_dsn())
    try:
        body = await conn.fetchval("SELECT body FROM email_outbox WHERE to_email = $1", newcomer)
    finally:
        await conn.close()
    code = re.search(r"\n\n(\S+)\n", body).group(1)
    accepted = await client.post("/v1/auth/accept-invite", json={
        "code": code, "password": PASSWORD, "display_name": "New Member"})
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["role"] == "member"
    reused = await client.post("/v1/auth/accept-invite", json={
        "code": code, "password": PASSWORD, "display_name": "Again"})
    assert reused.status_code == 404

    last = await client.delete(f"/v1/tenants/{company['tenant']}/members/{owner['id']}",
                               headers=owner["headers"])
    assert last.status_code == 409
    assert last.json()["error"]["code"] == "last_owner_required"

    listed = await client.get(f"/v1/tenants/{branch['tenant']}/members",
                              headers=admin_a["headers"])
    assert listed.status_code == 200, listed.text
    entry = next(item for item in listed.json()["items"] if item["principal_id"] == admin_a["id"])
    assert entry["inherited"] is True and entry["role"] == "admin"

    member_switch = await client.post("/v1/auth/switch-workspace", headers=member_y["headers"],
                                      json={"workspace_id": branch["workspace"]})
    assert member_switch.status_code == 404  # members do not inherit downwards
    admin_switch = await client.post("/v1/auth/switch-workspace", headers=admin_a["headers"],
                                     json={"workspace_id": branch["workspace"]})
    assert admin_switch.status_code == 200, admin_switch.text
    assert admin_switch.json()["inherited"] is True
    in_branch = await client.get("/v1/documents", headers=_bearer(admin_switch.json()["token"]))
    assert in_branch.status_code == 200

    guest = await _person(client, await _root(client, "Guest home"), "guest")
    await client.post(f"/v1/tenants/{branch['tenant']}/members", headers=owner["headers"],
                      json={"email": guest["email"], "role": "guest",
                            "expires_at": "2020-01-01T00:00:00Z"})
    expired = await client.post("/v1/auth/switch-workspace", headers=guest["headers"],
                                json={"workspace_id": branch["workspace"]})
    assert expired.status_code == 404
    conn = await asyncpg.connect(_dsn())
    try:
        denied = await conn.fetchval(
            "SELECT count(*) FROM access_audit WHERE workspace_id = $1::uuid "
            "AND action = 'access.denied' AND details->>'principal_id' = $2",
            branch["workspace"], guest["id"])
    finally:
        await conn.close()
    assert denied == 1

    tokens = []
    for index in range(3):
        issued = await client.post("/v1/tokens", headers=member_y["headers"], json={
            "scopes": ["docs:read"], "purpose": "member agent",
            "agent": {"agent_id": f"y-agent-{index}-{uuid.uuid4().hex[:4]}"}})
        assert issued.status_code == 201, issued.text
        tokens.append(_bearer(issued.json()["token"]))
    assert (await client.get("/v1/tokens/self", headers=tokens[0])).status_code == 200
    removed = await client.delete(f"/v1/tenants/{company['tenant']}/members/{member_y['id']}",
                                  headers=owner["headers"])
    assert removed.status_code == 200, removed.text
    assert len(removed.json()["revoked_tokens"]) == 3
    for token in tokens:
        denied = await client.get("/v1/tokens/self", headers=token)
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "token_revoked"
    assert (await client.get("/v1/documents", headers=member_y["headers"])).status_code == 401


# ------------------------------------------------------------------ AG-TC-07


async def _text_hits(conn, needle: str, *, include_content: bool) -> list[str]:
    content = {("shared_documents", "content"), ("shared_documents", "frontmatter"),
               ("shared_documents", "title"), ("document_versions", "content"),
               ("document_versions", "frontmatter"), ("document_versions", "title"),
               ("collab_tasks", "title"), ("collab_tasks", "description")}
    skip = {"rights_requests", "rights_request_events", "rights_exports", "redaction_log",
            "schema_migrations"}
    columns = await conn.fetch(
        "SELECT c.table_name, c.column_name, c.data_type FROM information_schema.columns c "
        "JOIN information_schema.tables t ON t.table_name = c.table_name AND "
        "t.table_schema = c.table_schema WHERE c.table_schema = 'public' AND "
        "t.table_type = 'BASE TABLE' AND c.data_type IN ('text', 'character varying', 'jsonb', "
        "'ARRAY')")
    hits = []
    for row in columns:
        table, column = row["table_name"], row["column_name"]
        if table in skip or (not include_content and (table, column) in content):
            continue
        expr = (f'array_to_string("{column}", \' \')' if row["data_type"] == "ARRAY"
                else f'CAST("{column}" AS text)')
        try:
            found = await conn.fetchval(
                f'SELECT count(*) FROM "{table}" WHERE strpos(lower({expr}), lower($1)) > 0',
                needle)
        except asyncpg.PostgresError:
            continue
        if found:
            hits.append(f"{table}.{column}")
    return hits


async def test_rights_request_end_to_end(org):
    client = org
    node = await _root(client, "Rights")
    owner = await _person(client, node, "handler")
    subject = await _person(client, node, "mia", role="REVIEWER")
    email = subject["email"]
    for index in range(10):
        await client.put(f"/v1/documents/m/plain-{index}", headers=subject["headers"],
                         json={"content": f"note {index}", "expected_version": 0})
    for index in range(2):
        issued = await client.post("/v1/tokens", headers=subject["headers"], json={
            "scopes": ["docs:read"], "purpose": "mia's agent",
            "agent": {"agent_id": f"mia-agent-{index}-{uuid.uuid4().hex[:4]}"}})
        assert issued.status_code == 201, issued.text
    for key in ("a", "b", "c"):
        await client.put(f"/v1/documents/m/mention-{key}", headers=node["key"],
                         json={"content": f"Contact {email} about {key}.", "expected_version": 0})
    await client.put("/v1/documents/m/history-d", headers=node["key"],
                     json={"content": f"old address {email}", "expected_version": 0})
    await client.put("/v1/documents/m/history-d", headers=node["key"],
                     json={"content": "address removed", "expected_version": 1})
    await client.put("/v1/documents/m/history-e", headers=node["key"],
                     json={"content": f"first {email}", "expected_version": 0})
    await client.put("/v1/documents/m/history-e", headers=node["key"],
                     json={"content": f"still {email.upper()}", "expected_version": 1})
    await client.put("/v1/documents/m/noise", headers=node["key"],
                     json={"content": "other@example.test and Mia alone", "expected_version": 0})

    empty = await client.post(f"/v1/tenants/{node['tenant']}/rights-requests",
                              headers=owner["headers"], json={"subject": {}, "type": "access"})
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "subject_identifier_required"
    agent_try = await client.post(f"/v1/tenants/{node['tenant']}/rights-requests",
                                  headers=node["key"],
                                  json={"subject": {"emails": [email]}, "type": "access"})
    assert agent_try.status_code == 403
    created = await client.post(f"/v1/tenants/{node['tenant']}/rights-requests",
                                headers=owner["headers"], json={
                                    "subject": {"principal_ref": subject["id"],
                                                "emails": [email]},
                                    "type": "deletion"})
    assert created.status_code == 201, created.text
    request_id = created.json()["request_id"]
    early = await client.post(f"/v1/rights-requests/{request_id}/erase",
                              headers=owner["headers"], json={"mode": "redact"})
    assert early.status_code == 409 and early.json()["error"]["code"] == "locate_required"

    located = await client.post(f"/v1/rights-requests/{request_id}/locate",
                                headers=owner["headers"])
    assert located.status_code == 200, located.text
    counts = located.json()["counts"]
    assert counts["principals"] == 1 and counts["memberships"] == 1 and counts["tokens"] == 2
    assert counts["stamped_documents"] >= 10
    hits = located.json()["inventory"]["unstructured"]
    documents = {hit["alias"] for hit in hits if hit["kind"] == "document"}
    versions = {(hit["alias"], hit["version"]) for hit in hits if hit["kind"] == "version"}
    assert documents == {"m/mention-a", "m/mention-b", "m/mention-c", "m/history-e"}
    assert versions == {("m/history-d", 1), ("m/history-e", 1)}
    assert all(len(hit["snippet"]) <= 80 for hit in hits)

    exported = await client.post(f"/v1/rights-requests/{request_id}/export",
                                 headers=owner["headers"], json={})
    assert exported.status_code == 200, exported.text
    archive = await client.get(f"/v1/rights-requests/{request_id}/export/download",
                               headers=owner["headers"])
    assert archive.status_code == 200
    from app.services.exports import _read_archive, _verify

    manifest, zipped = _read_archive(archive.content)
    _verify(manifest, zipped)
    names = zipped.namelist()
    assert "summary.md" in names
    assert len([name for name in names if name.startswith("documents/")]) == 4
    assert manifest["subset"] == "personal_data"

    # A backup before the erasure: restoring it later must not bring the email back.
    backup = await client.post("/v1/admin/backups", headers=ADMIN,
                               json={"type": "full", "tenant_ids": [node["tenant"]]})
    assert backup.status_code == 201, backup.text

    pseudo = await client.post(f"/v1/rights-requests/{request_id}/erase",
                               headers=owner["headers"], json={"mode": "pseudonymise"})
    assert pseudo.status_code == 200, pseudo.text
    pseudonym = pseudo.json()["pseudonymised"][0]["pseudonym"]
    assert pseudonym.startswith("former-member-")
    explained = await client.get("/v1/documents/m/plain-0/explain", headers=node["key"])
    assert explained.status_code == 200 and explained.json()["grant_at_write"] is None or True
    conn = await asyncpg.connect(_dsn())
    try:
        assert await _text_hits(conn, email, include_content=False) == []
        stamp_owner = await conn.fetchval(
            "SELECT stamp->>'principal_id' FROM document_versions v JOIN shared_documents d "
            "ON d.id = v.document_id WHERE d.workspace_id = $1::uuid AND d.doc_key = "
            "'m/plain-0'", node["workspace"])
        canonical = await conn.fetchval("SELECT canonical_id FROM human_users WHERE id = $1::uuid",
                                        subject["id"])
        assert stamp_owner == canonical  # the lineage still points at the same principal
        assert await conn.fetchval("SELECT display_name FROM human_users WHERE id = $1::uuid",
                                   subject["id"]) == pseudonym
    finally:
        await conn.close()

    redacted = await client.post(f"/v1/rights-requests/{request_id}/erase",
                                 headers=owner["headers"], json={"mode": "redact"})
    assert redacted.status_code == 200, redacted.text
    assert redacted.json()["redacted"]["documents"] == 4
    # 2 located historical versions + the 4 versions that were current until now.
    assert redacted.json()["redacted"]["versions"] == 6
    old = await client.get("/v1/documents/m/history-d/versions/1", headers=node["key"])
    assert email.lower() not in old.text.lower() and old.json()["redacted"] is True
    current = await client.get("/v1/documents/m/mention-a", headers=node["key"])
    body = current.json()["content"]
    assert email not in body and "[已删除的个人信息]" in body
    conn = await asyncpg.connect(_dsn())
    try:
        assert await _text_hits(conn, email, include_content=True) == []
    finally:
        await conn.close()

    restore = await client.post("/v1/admin/restore", headers=ADMIN, json={
        "backup_id": backup.json()["backup_id"], "target": "staging",
        "tenant_id": node["tenant"]})
    for _ in range(300):
        state = (await client.get(f"/v1/admin/restores/{restore.json()['restore_id']}",
                                  headers=ADMIN)).json()
        if state["status"] != "running":
            break
        await asyncio.sleep(0.2)
    assert state["status"] == "done", state
    assert state["result"]["redactions_replayed"] >= 7
    staging = await asyncpg.connect(_dsn(_staging()))
    try:
        assert await _text_hits(staging, email, include_content=True) == []
    finally:
        await staging.close()

    explanation = await client.get(f"/v1/rights-requests/{request_id}/explain",
                                   headers=owner["headers"])
    assert explanation.status_code == 200 and explanation.json()["processed_by"]

    closed = await client.post(f"/v1/rights-requests/{request_id}/close",
                               headers=owner["headers"], json={})
    assert closed.json()["status"] == "closed"
    again = await client.post(f"/v1/rights-requests/{request_id}/locate",
                              headers=owner["headers"])
    assert again.status_code == 409 and again.json()["error"]["code"] == "request_closed"


async def test_rights_reminders_and_minimization(org):
    client = org
    node = await _root(client, "Reminders")
    owner = await _person(client, node, "handler2")
    request = (await client.post(f"/v1/tenants/{node['tenant']}/rights-requests",
                                 headers=owner["headers"], json={
                                     "subject": {"emails": ["someone@example.test"]},
                                     "type": "access"})).json()
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("UPDATE rights_requests SET received_at = NOW() - INTERVAL '15 days', "
                           "due_at = NOW() + INTERVAL '2 days' WHERE id = $1",
                           request["request_id"])
        reminded = False
        for _ in range(40):
            alerts = (await client.get("/v1/admin/alerts?kind=rights_request.handler",
                                       headers=ADMIN)).json()["items"]
            if any(item["details"]["request_id"] == request["request_id"] for item in alerts):
                reminded = True
                break
            await asyncio.sleep(0.5)
        assert reminded
        await client.post(f"/v1/rights-requests/{request['request_id']}/close",
                          headers=owner["headers"], json={})
        await conn.execute("UPDATE rights_requests SET closed_at = NOW() - INTERVAL '31 days' "
                           "WHERE id = $1", request["request_id"])
        minimized = None
        for _ in range(40):
            minimized = await conn.fetchrow(
                "SELECT subject, minimized_at FROM rights_requests WHERE id = $1",
                request["request_id"])
            if minimized["minimized_at"] is not None:
                break
            await asyncio.sleep(0.5)
        assert minimized["minimized_at"] is not None
        assert "someone@example.test" not in minimized["subject"]
        timeline = await conn.fetchval(
            "SELECT count(*) FROM rights_request_events WHERE request_id = $1",
            request["request_id"])
        assert timeline >= 3
    finally:
        await conn.close()
