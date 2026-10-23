"""Tenant dimension acceptance (PRD AG-ID-07)."""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.integration

SECRET = "TENANT-A-SECRET-MARKER"


def db_dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def _workspace(client, admin_headers, name="tenant"):
    response = await client.post(
        "/v1/admin/workspaces",
        headers=admin_headers,
        json={"name": f"{name}-{uuid.uuid4().hex[:6]}"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return {
        "id": body["workspace"]["id"],
        "headers": {"Authorization": f"Bearer {body['api_key']}"},
    }


async def _owner(client, admin_headers, workspace_id):
    email = f"owner-{uuid.uuid4().hex[:8]}@example.test"
    created = await client.post(
        f"/v1/admin/workspaces/{workspace_id}/human-users",
        headers=admin_headers,
        json={
            "email": email,
            "display_name": "Owner",
            "role": "OWNER",
            "password": "correct-horse-battery",
        },
    )
    assert created.status_code == 201, created.text
    login = await client.post(
        "/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    return {"Authorization": f"Bearer {login.json()['token']}"}


async def _tenant_of(client, headers) -> dict:
    whoami = await client.get("/v1/whoami", headers=headers)
    assert whoami.status_code == 200, whoami.text
    return whoami.json()["tenant"]


async def test_whoami_reports_a_root_tenant(client, admin_headers):
    workspace = await _workspace(client, admin_headers)
    tenant = await _tenant_of(client, workspace["headers"])
    assert tenant["id"].startswith("ten_") and len(tenant["id"]) == 30
    assert tenant["depth"] == 1 and tenant["path"] == [tenant["id"]]
    assert tenant["root_tenant_id"] == tenant["id"]
    assert tenant["status"] == "active"


async def test_cross_tenant_fuzz_leaks_nothing(client, admin_headers):
    a = await _workspace(client, admin_headers, "fuzz-a")
    b = await _workspace(client, admin_headers, "fuzz-b")
    keys = [f"fuzz/doc-{index:03d}" for index in range(100)]
    for start in range(0, len(keys), 25):
        batch = await client.post(
            "/v1/documents/batch",
            headers=a["headers"],
            json={
                "items": [
                    {"doc_key": key, "content": f"# {SECRET} {key}", "expected_version": 0}
                    for key in keys[start : start + 25]
                ]
            },
        )
        assert batch.status_code == 200, batch.text
    # Same alias in B does not collide with A (acceptance 4).
    same = await client.put(
        f"/v1/documents/{keys[0]}",
        headers=b["headers"],
        json={"content": "b's own", "expected_version": 0},
    )
    assert same.status_code == 200, same.text
    for index in range(10):
        task = await client.post(
            "/v1/tasks",
            headers=a["headers"],
            json={"task_key": f"FUZZ-{index}", "title": f"{SECRET} task"},
        )
        assert task.status_code == 201, task.text

    requests = 0
    for key in keys[1:]:
        for method, path, body in (
            ("GET", f"/v1/documents/{key}", None),
            ("GET", f"/v1/documents/{key}/versions", None),
            ("POST", "/v1/dereference", {"uri": f"doc://{key}"}),
            ("PUT", f"/v1/documents/{key}", {"content": "x", "expected_version": 1}),
            ("POST", f"/v1/documents/{key}/archive", {}),
        ):
            response = await client.request(method, path, headers=b["headers"], json=body)
            requests += 1
            assert SECRET not in response.text, (method, path)
            assert response.status_code in (404, 409, 422), (method, path, response.text)
    for index in range(10):
        response = await client.get(f"/v1/tasks/FUZZ-{index}", headers=b["headers"])
        requests += 1
        assert response.status_code == 404
    for path in ("/v1/documents?limit=500", "/v1/tasks", "/v1/workspace/map", "/v1/stats",
                 "/v1/timeline", "/v1/proposals", "/v1/agent-docs"):
        response = await client.get(path, headers=b["headers"])
        requests += 1
        assert SECRET not in response.text, path
    query = await client.post(
        "/v1/query", headers=b["headers"], json={"query": SECRET, "limit": 50}
    )
    assert SECRET not in query.text
    assert requests >= 500


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("PUT", "/v1/documents/t/x", {"json": {"content": "x", "expected_version": 0,
                                                "tenant_id": "ten_x"}}),
        ("GET", "/v1/documents?tenant_id=ten_x", {}),
        ("GET", "/v1/whoami", {"headers": {"X-Tenant-Id": "ten_x"}}),
    ],
)
async def test_client_supplied_tenant_id_is_rejected(
    client, admin_headers, method, path, kwargs
):
    workspace = await _workspace(client, admin_headers)
    headers = {**workspace["headers"], **kwargs.pop("headers", {})}
    response = await client.request(method, path, headers=headers, **kwargs)
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "tenant_id_not_allowed"
    assert error["class"] == "fix_request"


async def test_nested_tenant_id_in_user_data_is_allowed(client, admin_headers):
    workspace = await _workspace(client, admin_headers)
    response = await client.put(
        "/v1/documents/t/frontmatter",
        headers=workspace["headers"],
        json={"content": "x", "frontmatter": {"tenant_id": "customer"}, "expected_version": 0},
    )
    assert response.status_code == 200, response.text


async def test_every_owned_row_carries_a_derived_tenant(client, admin_headers):
    workspace = await _workspace(client, admin_headers)
    await client.put(
        "/v1/documents/t/derived",
        headers=workspace["headers"],
        json={"content": "x", "expected_version": 0},
    )
    tenant = await _tenant_of(client, workspace["headers"])
    conn = await asyncpg.connect(db_dsn())
    try:
        nullable = await conn.fetch(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = 'tenant_id' "
            "AND is_nullable = 'YES' AND table_name <> 'tenants'"
        )
        assert [row["table_name"] for row in nullable] == []
        missing = await conn.fetch(
            "SELECT c.table_name FROM information_schema.columns c "
            "WHERE c.table_schema = 'public' AND c.column_name = 'workspace_id' "
            # Operator-only tables (RLS forced, no application-role policy):
            # a CLI code or a product event may exist before any tenant does.
            "AND c.table_name NOT IN ('rate_limit_windows', 'product_events', 'cli_sessions') "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM information_schema.columns t WHERE t.table_schema = 'public' "
            "  AND t.table_name = c.table_name AND t.column_name = 'tenant_id')"
        )
        assert [row["table_name"] for row in missing] == []
        derived = await conn.fetchval(
            "SELECT tenant_id FROM shared_documents WHERE workspace_id = $1::uuid "
            "AND doc_key = 't/derived'",
            workspace["id"],
        )
        assert derived == tenant["id"]
        # A writer cannot forge tenant_id: the trigger re-derives it.
        await conn.execute(
            "UPDATE shared_documents SET tenant_id = 'ten_forged' "
            "WHERE workspace_id = $1::uuid AND doc_key = 't/derived'",
            workspace["id"],
        )
        assert await conn.fetchval(
            "SELECT tenant_id FROM shared_documents WHERE workspace_id = $1::uuid "
            "AND doc_key = 't/derived'",
            workspace["id"],
        ) == tenant["id"]
    finally:
        await conn.close()


async def test_tree_constraints_are_enforced_in_the_database(client, admin_headers):
    workspace = await _workspace(client, admin_headers)
    root = await _tenant_of(client, workspace["headers"])
    conn = await asyncpg.connect(db_dsn())
    suffix = uuid.uuid4().hex[:8]
    try:
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "INSERT INTO tenants (alias, display_name, tenant_type, parent_tenant_id) "
                "VALUES ($1, 'c', 'company', $2)",
                f"company-child-{suffix}",
                root["id"],
            )
        personal = await conn.fetchval(
            "INSERT INTO tenants (alias, display_name, tenant_type, parent_tenant_id) "
            "VALUES ($1, 'p', 'personal', $2) RETURNING id",
            f"personal-{suffix}",
            root["id"],
        )
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "INSERT INTO tenants (alias, display_name, tenant_type, parent_tenant_id) "
                "VALUES ($1, 'x', 'personal', $2)",
                f"under-personal-{suffix}",
                personal,
            )
        level2 = await conn.fetchval(
            "INSERT INTO tenants (alias, display_name, tenant_type, parent_tenant_id) "
            "VALUES ($1, 'o2', 'organization', $2) RETURNING id",
            f"org2-{suffix}",
            root["id"],
        )
        level3 = await conn.fetchrow(
            "INSERT INTO tenants (alias, display_name, tenant_type, parent_tenant_id) "
            "VALUES ($1, 'o3', 'organization', $2) RETURNING id, depth, path, root_tenant_id",
            f"org3-{suffix}",
            level2,
        )
        assert level3["depth"] == 3
        assert list(level3["path"]) == [root["id"], level2, level3["id"]]
        assert level3["root_tenant_id"] == root["id"]
        # Three levels below the root (AG-ID-09): depth counts the root as 1.
        level4 = await conn.fetchval(
            "INSERT INTO tenants (alias, display_name, tenant_type, parent_tenant_id) "
            "VALUES ($1, 'deep', 'personal', $2) RETURNING depth",
            f"depth4-{suffix}",
            level3["id"],
        )
        assert level4 == 4
        level3b = await conn.fetchval(
            "INSERT INTO tenants (alias, display_name, tenant_type, parent_tenant_id) "
            "VALUES ($1, 'o4', 'organization', $2) RETURNING id",
            f"org4-{suffix}",
            level3["id"],
        )
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "INSERT INTO tenants (alias, display_name, tenant_type, parent_tenant_id) "
                "VALUES ($1, 'deeper', 'personal', $2)",
                f"depth5-{suffix}",
                level3b,
            )
    finally:
        await conn.close()


async def test_suspended_tenant_reads_but_cannot_write(client, admin_headers):
    workspace = await _workspace(client, admin_headers)
    tenant = await _tenant_of(client, workspace["headers"])
    suspended = await client.post(
        f"/v1/admin/tenants/{tenant['id']}/suspend",
        headers=admin_headers,
        json={"reason": "billing"},
    )
    assert suspended.status_code == 200, suspended.text
    write = await client.put(
        "/v1/documents/t/blocked",
        headers=workspace["headers"],
        json={"content": "x", "expected_version": 0},
    )
    assert write.status_code == 403
    assert write.json()["error"]["code"] == "tenant_suspended"
    assert write.json()["error"]["class"] == "escalate_human"
    assert (await client.get("/v1/documents", headers=workspace["headers"])).status_code == 200
    query = await client.post("/v1/query", headers=workspace["headers"], json={"limit": 5})
    assert query.status_code == 200
    resumed = await client.post(
        f"/v1/admin/tenants/{tenant['id']}/resume", headers=admin_headers
    )
    assert resumed.status_code == 200
    write = await client.put(
        "/v1/documents/t/blocked",
        headers=workspace["headers"],
        json={"content": "x", "expected_version": 0},
    )
    assert write.status_code == 200


async def test_two_party_deletion_grace_restore_and_purge(client, admin_headers):
    workspace = await _workspace(client, admin_headers)
    owner = await _owner(client, admin_headers, workspace["id"])
    tenant = await _tenant_of(client, workspace["headers"])
    tid, alias = tenant["id"], tenant["alias"]

    wrong = await client.post(
        f"/v1/admin/tenants/{tid}/delete", headers=admin_headers, json={"confirm": "nope"}
    )
    assert wrong.status_code == 409
    first = await client.post(
        f"/v1/admin/tenants/{tid}/delete", headers=admin_headers, json={"confirm": alias}
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "active"
    assert first.json()["pending_confirmations"] == ["owner"]

    second = await client.post(
        "/v1/human/tenant/delete", headers=owner, json={"confirm": alias}
    )
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "deleting"
    assert second.json()["purge_after"] is not None

    blocked = await client.get("/v1/documents", headers=workspace["headers"])
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "tenant_deleting"
    assert blocked.json()["error"]["details"]["restorable"] is True
    assert (await client.get("/v1/human/tenant", headers=owner)).status_code == 200

    restored = await client.post("/v1/human/tenant/restore", headers=owner)
    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == "active"
    assert (await client.get("/v1/documents", headers=workspace["headers"])).status_code == 200

    again = await client.post(
        f"/v1/admin/tenants/{tid}/delete", headers=admin_headers, json={"confirm": alias}
    )
    assert again.status_code == 200
    assert (
        await client.post("/v1/human/tenant/delete", headers=owner, json={"confirm": alias})
    ).status_code == 200

    from app.database import async_session_factory
    from app.services.tenants import purge_due_tenants

    conn = await asyncpg.connect(db_dsn())
    try:
        await conn.execute(
            "UPDATE tenants SET purge_after = NOW() - INTERVAL '1 minute' WHERE id = $1", tid
        )
        # The server's reaper may purge first; either way the end state matters.
        async with async_session_factory() as session:
            await purge_due_tenants(session)
        row = await conn.fetchrow(
            "SELECT status, display_name, alias FROM tenants WHERE id = $1", tid
        )
        assert row["status"] == "deleted" and row["display_name"] == "[deleted]"
        assert await conn.fetchval(
            "SELECT count(*) FROM workspaces WHERE id = $1::uuid", workspace["id"]
        ) == 0
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO tenants (alias, display_name, tenant_type) "
                "VALUES ($1, 'reuse', 'organization')",
                alias,
            )
    finally:
        await conn.close()
    gone = await client.get("/v1/whoami", headers=workspace["headers"])
    assert gone.status_code == 401
