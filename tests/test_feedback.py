from __future__ import annotations

import asyncio
import uuid

import pytest

from app.root_page import ROOT_PRIMITIVES

pytestmark = pytest.mark.integration


async def make_agent(
    client,
    admin_headers: dict[str, str],
    workspace: dict,
    role: str = "EXECUTOR",
) -> dict:
    signature = f"{role.lower()}_{uuid.uuid4().hex[:8]}"
    response = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        headers=admin_headers,
        json={
            "agent_signature": signature,
            "role_scope": role,
            "allowed_namespaces": ["*"],
        },
    )
    assert response.status_code in (200, 201), response.text
    body = response.json()
    return {
        "signature": signature,
        "api_key": body["api_key"],
        "headers": {"Authorization": f"Bearer {body['api_key']}"},
    }


async def upsert_doc(
    client,
    headers: dict[str, str],
    doc_key: str,
    *,
    namespace: str = "feedback",
    frontmatter: dict | None = None,
    expected_version: int = 0,
    content: str | None = None,
) -> dict:
    response = await client.put(
        f"/v1/documents/{doc_key}",
        headers=headers,
        json={
            "content": content or f"# {doc_key}\n\nbody",
            "frontmatter": frontmatter or {},
            "expected_version": expected_version,
            "namespace": namespace,
        },
    )
    return response


async def test_browser_root_serves_human_page_and_json_stays_available(client):
    response = await client.get("/", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "accept" in response.headers["vary"].lower()
    assert 'id="flow"' in response.text
    assert 'href="/dashboard/"' in response.text
    assert 'class="entry"' in response.text
    assert 'primitives' in response.text
    assert f'<span class="number">{len(ROOT_PRIMITIVES)}</span>' in response.text
    assert "COPY RESPONSE" in response.text
    assert 'href="/?format=json"' in response.text

    api = await client.get("/", headers={"Accept": "application/json"})
    assert api.status_code == 200
    assert api.headers["content-type"].startswith("application/json")
    assert "accept" in api.headers["vary"].lower()
    assert api.json()["name"] == "AgentGraph"
    assert len(api.json()["primitives"]) == len(ROOT_PRIMITIVES)

    raw = await client.get("/?format=json", headers={"Accept": "text/html"})
    assert raw.status_code == 200
    assert raw.headers["content-type"].startswith("application/json")
    assert raw.json()["first_call"] == "get_orientation"
    assert len(raw.json()["primitives"]) == len(ROOT_PRIMITIVES)


async def test_exhaustive_cursor_listing_and_archive(client, workspace):
    namespace = f"census-{uuid.uuid4().hex[:6]}"
    keys = [f"{namespace}/doc-{index:02d}" for index in range(7)]
    for index, key in enumerate(keys):
        response = await upsert_doc(
            client,
            workspace["headers"],
            key,
            namespace=namespace,
            frontmatter={"index": index},
        )
        assert response.status_code in (200, 201), response.text

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"namespace": namespace, "limit": 3}
        if cursor:
            params["cursor"] = cursor
        page = await client.get(
            "/v1/documents", headers=workspace["headers"], params=params
        )
        assert page.status_code == 200, page.text
        payload = page.json()
        assert payload["namespace"] == namespace
        assert payload["count"] == len(payload["items"])
        seen.extend(item["doc_key"] for item in payload["items"])
        cursor = payload["next_cursor"]
        pages += 1
        if cursor is None:
            break
        assert pages < 10
    assert sorted(seen) == sorted(keys)
    assert len(seen) == len(set(seen))
    assert page.json()["total"] == len(keys)
    assert page.json()["count"] <= 3

    oversized = await client.get(
        "/v1/documents",
        headers=workspace["headers"],
        params={"namespace": namespace, "limit": 501},
    )
    assert oversized.status_code == 422, oversized.text
    offset = await client.get(
        "/v1/documents",
        headers=workspace["headers"],
        params={"namespace": namespace, "offset": 0},
    )
    assert offset.status_code == 422, offset.text
    assert offset.json()["error"]["code"] == "offset_not_supported"

    status_filter = await client.get(
        "/v1/documents",
        headers=workspace["headers"],
        params={
            "namespace": namespace,
            "verification_status": "DRAFT",
            "limit": 100,
        },
    )
    assert status_filter.status_code == 200, status_filter.text
    assert status_filter.json()["total"] == len(keys)

    sorted_page = await client.get(
        "/v1/documents",
        headers=workspace["headers"],
        params={
            "namespace": namespace,
            "sort": "updated_asc",
            "limit": 100,
        },
    )
    assert sorted_page.status_code == 200, sorted_page.text
    assert sorted_page.json()["items"][0]["doc_key"] == keys[0]

    invalid_status = await client.get(
        "/v1/documents",
        headers=workspace["headers"],
        params={"namespace": namespace, "verification_status": "BOGUS"},
    )
    assert invalid_status.status_code == 422, invalid_status.text

    invalid = await client.get(
        "/v1/documents",
        headers=workspace["headers"],
        params={"namespace": namespace, "cursor": "not-a-cursor"},
    )
    assert invalid.status_code == 400, invalid.text
    assert invalid.json()["error"]["code"] == "invalid_cursor"

    archived = await client.post(
        f"/v1/documents/{keys[0]}/archive",
        headers=workspace["headers"],
        json={"reason": "test residue"},
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["document"]["archived_by"]

    default_list = await client.get(
        "/v1/documents",
        headers=workspace["headers"],
        params={"namespace": namespace, "limit": 100},
    )
    assert default_list.status_code == 200
    assert keys[0] not in {item["doc_key"] for item in default_list.json()["items"]}

    with_archived = await client.get(
        "/v1/documents",
        headers=workspace["headers"],
        params={
            "namespace": namespace,
            "limit": 100,
            "include_archived": "true",
        },
    )
    assert with_archived.status_code == 200
    assert keys[0] in {item["doc_key"] for item in with_archived.json()["items"]}

    still_readable = await client.get(
        f"/v1/documents/{keys[0]}", headers=workspace["headers"]
    )
    assert still_readable.status_code == 200

    restored = await client.post(
        f"/v1/documents/{keys[0]}/restore",
        headers=workspace["headers"],
        json={"reason": "recovered"},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["document"]["archived_at"] is None


async def test_own_draft_visibility_and_archive_permissions(
    client, admin_headers, workspace
):
    writer = await make_agent(client, admin_headers, workspace)
    other = await make_agent(client, admin_headers, workspace)
    key = f"feedback/own-{uuid.uuid4().hex[:6]}"
    marker = uuid.uuid4().hex
    created = await upsert_doc(
        client, writer["headers"], key, frontmatter={"marker": marker}
    )
    assert created.status_code in (200, 201), created.text

    own = await client.post(
        "/v1/query",
        headers=writer["headers"],
        json={"filters": {"marker": marker}, "limit": 10},
    )
    assert own.status_code == 200, own.text
    assert key in {item["doc_key"] for item in own.json()["items"]}

    other_default = await client.post(
        "/v1/query",
        headers=other["headers"],
        json={"filters": {"marker": marker}, "limit": 10},
    )
    assert other_default.status_code == 200
    assert other_default.json()["items"] == []

    other_explicit = await client.post(
        "/v1/query",
        headers=other["headers"],
        json={"filters": {"marker": marker}, "limit": 10, "include_unverified": True},
    )
    assert key in {item["doc_key"] for item in other_explicit.json()["items"]}

    denied = await client.post(
        f"/v1/documents/{key}/archive",
        headers=other["headers"],
        json={"reason": "not mine"},
    )
    assert denied.status_code == 403, denied.text

    archived = await client.post(
        f"/v1/documents/{key}/archive",
        headers=writer["headers"],
        json={"reason": "cleanup"},
    )
    assert archived.status_code == 200, archived.text

    hidden = await client.post(
        "/v1/query",
        headers=writer["headers"],
        json={"filters": {"marker": marker}, "limit": 10},
    )
    assert key not in {item["doc_key"] for item in hidden.json()["items"]}

    included = await client.post(
        "/v1/query",
        headers=writer["headers"],
        json={"filters": {"marker": marker}, "limit": 10, "include_archived": True},
    )
    assert key in {item["doc_key"] for item in included.json()["items"]}


async def test_batch_upsert_is_atomic_and_batch_dereference(client, workspace):
    namespace = f"batch-{uuid.uuid4().hex[:6]}"
    good = f"{namespace}/good"
    bad = f"{namespace}/bad"

    failed = await client.post(
        "/v1/documents/batch",
        headers=workspace["headers"],
        json={
            "items": [
                {
                    "doc_key": good,
                    "content": "# good\n\nok",
                    "expected_version": 0,
                    "namespace": namespace,
                },
                {
                    "doc_key": bad,
                    "content": "# bad\n\nstale",
                    "expected_version": 5,
                    "namespace": namespace,
                },
            ]
        },
    )
    assert failed.status_code == 409, failed.text
    assert failed.json()["error"]["details"]["index"] == 1
    missing = await client.get(
        f"/v1/documents/{good}", headers=workspace["headers"]
    )
    assert missing.status_code == 404

    success = await client.post(
        "/v1/documents/batch",
        headers=workspace["headers"],
        json={
            "items": [
                {
                    "doc_key": good,
                    "content": "# good\n\nok",
                    "expected_version": 0,
                    "namespace": namespace,
                },
                {
                    "doc_key": bad,
                    "content": "# bad\n\nok",
                    "expected_version": 0,
                    "namespace": namespace,
                },
            ]
        },
    )
    assert success.status_code == 200, success.text
    assert {item["document"]["doc_key"] for item in success.json()["items"]} == {
        good,
        bad,
    }
    assert all(item["ok"] is True for item in success.json()["items"])

    many = await client.post(
        "/v1/dereference-many",
        headers=workspace["headers"],
        json={"uris": [f"doc://{good}", "doc://missing/doc"]},
    )
    assert many.status_code == 200, many.text
    results = {item["uri"]: item for item in many.json()["items"]}
    assert results[f"doc://{good}"]["ok"] is True
    assert results[f"doc://{good}"]["document"]["doc_key"] == good
    assert results["doc://missing/doc"]["ok"] is False
    assert results["doc://missing/doc"]["error"]["code"] == "not_found"

    oversized = await client.post(
        "/v1/documents/batch",
        headers=workspace["headers"],
        json={
            "items": [
                {
                    "doc_key": f"{namespace}/too-many-{index}",
                    "content": "x",
                    "expected_version": 0,
                    "namespace": namespace,
                }
                for index in range(26)
            ]
        },
    )
    assert oversized.status_code == 422, oversized.text


async def test_frontmatter_range_filters_and_conflict_payload(client, workspace):
    namespace = f"range-{uuid.uuid4().hex[:6]}"
    docs = {
        "a": {
            "amount": 50,
            "due_at": "2026-11-02",
            "owner": {"name": "alice"},
        },
        "b": {
            "amount": 1500,
            "due_at": "2026-11-20",
            "owner": {"name": "bob"},
        },
        "c": {"amount": 900, "due_at": "2026-12-05"},
    }
    for name, frontmatter in docs.items():
        response = await upsert_doc(
            client,
            workspace["headers"],
            f"{namespace}/{name}",
            namespace=namespace,
            frontmatter=frontmatter,
        )
        assert response.status_code in (200, 201), response.text

    query = await client.post(
        "/v1/query",
        headers=workspace["headers"],
        json={
            "namespace": namespace,
            "filters": {
                "due_at": {"gte": "2026-11-01", "lte": "2026-11-30"},
                "amount": {"gt": 1000},
                "owner": {"exists": True},
            },
            "limit": 20,
        },
    )
    assert query.status_code == 200, query.text
    assert [item["doc_key"] for item in query.json()["items"]] == [
        f"{namespace}/b"
    ]

    eq_query = await client.post(
        "/v1/query",
        headers=workspace["headers"],
        json={
            "namespace": namespace,
            "filters": {"amount": {"eq": 900}},
            "limit": 20,
        },
    )
    assert eq_query.status_code == 200, eq_query.text
    assert [item["doc_key"] for item in eq_query.json()["items"]] == [
        f"{namespace}/c"
    ]

    invalid = await client.post(
        "/v1/query",
        headers=workspace["headers"],
        json={
            "namespace": namespace,
            "filters": {"amount": {"between": [1, 2]}},
            "limit": 20,
        },
    )
    assert invalid.status_code == 422, invalid.text
    assert invalid.json()["error"]["code"] == "invalid_filter_operator"
    assert invalid.json()["error"]["details"]["field"] == "filters.amount"

    conflict = await upsert_doc(
        client,
        workspace["headers"],
        f"{namespace}/b",
        namespace=namespace,
        frontmatter=docs["b"],
        expected_version=999,
    )
    assert conflict.status_code == 409, conflict.text
    details = conflict.json()["error"]["details"]
    assert details["current_version"] == 1
    assert details["current_hash"]
    assert details["latest"]["doc_key"] == f"{namespace}/b"


async def test_incremental_workspace_sync_and_namespace_counts(client, workspace):
    namespace = f"sync-{uuid.uuid4().hex[:6]}"
    baseline = await client.get("/v1/workspace/map", headers=workspace["headers"])
    assert baseline.status_code == 200, baseline.text
    cursor = baseline.json()["next_cursor"]
    assert cursor
    assert isinstance(baseline.json()["namespace_counts"]["documents"], dict)

    await asyncio.sleep(0.02)
    key = f"{namespace}/changed"
    claimed = await upsert_doc(
        client,
        workspace["headers"],
        key,
        namespace=namespace,
        frontmatter={"status": "DRAFT"},
    )
    assert claimed.status_code in (200, 201), claimed.text
    task_key = f"SYNC-{uuid.uuid4().hex[:6].upper()}"
    task = await client.post(
        "/v1/tasks",
        headers=workspace["headers"],
        json={
            "task_key": task_key,
            "title": "incremental task",
            "namespace": namespace,
        },
    )
    assert task.status_code in (200, 201), task.text

    incremental = await client.get(
        "/v1/workspace/map",
        headers=workspace["headers"],
        params={"since": cursor},
    )
    assert incremental.status_code == 200, incremental.text
    payload = incremental.json()
    assert payload["incremental"] is True
    assert key in {item["doc_key"] for item in payload["documents"]}
    assert task_key in {item["task_key"] for item in payload["tasks"]}
    assert payload["next_cursor"] and payload["next_cursor"] != cursor
    assert any(
        event["event_type"] == "DOC_CREATED"
        and event["payload"].get("doc_key") == key
        for event in payload["timeline"]
    )
    assert payload["namespace_counts"]["documents"][namespace] >= 1
    assert payload["namespace_counts"]["tasks"][namespace] >= 1

    no_changes = await client.get(
        "/v1/workspace/map",
        headers=workspace["headers"],
        params={"since": payload["next_cursor"]},
    )
    assert no_changes.status_code == 200, no_changes.text
    assert no_changes.json()["documents"] == []
    assert no_changes.json()["tasks"] == []
    assert no_changes.json()["timeline"] == []

    invalid = await client.get(
        "/v1/workspace/map",
        headers=workspace["headers"],
        params={"since": "bad-cursor"},
    )
    assert invalid.status_code == 400, invalid.text
    assert invalid.json()["error"]["code"] == "invalid_cursor"


async def test_agent_docs_are_readable_without_admin(client, workspace):
    docs = await client.get("/v1/agent-docs", headers=workspace["headers"])
    assert docs.status_code == 200, docs.text
    payload = docs.json()
    assert payload["base_url"].endswith("/v1")
    assert payload["mcp_endpoint"].endswith("/mcp/")
    assert "/v1/documents" in payload["openapi"]["paths"]
    assert not any(path.startswith("/v1/admin") for path in payload["openapi"]["paths"])

    anonymous = await client.get("/v1/agent-docs")
    assert anonymous.status_code == 401
