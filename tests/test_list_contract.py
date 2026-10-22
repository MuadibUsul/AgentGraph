"""Census listing contract for list_documents (PRD AG-ST-01)."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services.documents import _unb64, sign_list_cursor

pytestmark = pytest.mark.integration

V2 = {"AgentGraph-Version": "2026-11"}


async def _agent(client, admin_headers, workspace):
    response = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        headers=admin_headers,
        json={"agent_signature": f"lister_{uuid.uuid4().hex[:6]}", "role_scope": "EXECUTOR"},
    )
    return {"Authorization": f"Bearer {response.json()['api_key']}"}


async def _batch(client, headers, namespace, keys):
    for start in range(0, len(keys), 25):
        response = await client.post(
            "/v1/documents/batch",
            headers=headers,
            json={"items": [
                {"doc_key": key, "namespace": namespace, "content": f"body of {key}",
                 "expected_version": 0}
                for key in keys[start:start + 25]
            ]},
        )
        assert response.status_code == 200, response.text


async def _walk(client, headers, params):
    seen, pages, counts = [], 0, set()
    cursor = None
    while True:
        query = dict(params)
        if cursor:
            query["cursor"] = cursor
        response = await client.get("/v1/documents", headers=headers, params=query)
        assert response.status_code == 200, response.text
        body = response.json()
        pages += 1
        counts.add(body["snapshot_count"])
        seen += [(item["namespace"], item["doc_key"]) for item in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            return seen, pages, counts


async def test_census_lists_every_document_once(client, admin_headers, workspace):
    namespace = f"t{uuid.uuid4().hex[:6]}"
    writer = await _agent(client, admin_headers, workspace)
    reader = {**await _agent(client, admin_headers, workspace), **V2}
    keys = [f"doc-{index:03d}" for index in range(46)]
    # Another agent's drafts are part of the census.
    await _batch(client, writer, namespace, keys[:30])
    await _batch(client, workspace["headers"], namespace, keys[30:])
    seen, pages, counts = await _walk(client, reader, {"namespace": namespace, "limit": 10})
    assert len(seen) == 46 and len(set(seen)) == 46 and pages == 5
    assert {key for _, key in seen} == set(keys)
    assert counts == {46}
    assert [key for _, key in seen] == sorted(keys)  # doc_key_asc by default

    firsts = [
        (await client.get("/v1/documents", headers=reader,
                          params={"namespace": namespace, "limit": 10})).json()
        for _ in range(5)
    ]
    assert {page["snapshot_count"] for page in firsts} == {46}
    assert all(page["order"] == "doc_key_asc" and page["as_of"] for page in firsts)


async def test_thousand_documents_in_two_pages(client, workspace):
    namespace = f"k{uuid.uuid4().hex[:6]}"
    keys = [f"item-{index:04d}" for index in range(1000)]
    await _batch(client, workspace["headers"], namespace, keys)
    seen, pages, _ = await _walk(
        client, {**workspace["headers"], **V2}, {"namespace": namespace, "limit": 500}
    )
    # 1000 rows at 500 per page: two full pages (the second reports no next cursor).
    assert len(seen) == 1000 and len(set(seen)) == 1000 and pages == 2


async def test_writes_during_the_walk_appear_once(client, workspace):
    namespace = f"w{uuid.uuid4().hex[:6]}"
    headers = {**workspace["headers"], **V2}
    await _batch(client, workspace["headers"], namespace, [f"m-{i:02d}" for i in range(30)])
    first = (await client.get("/v1/documents", headers=headers,
                              params={"namespace": namespace, "limit": 10})).json()
    second = (await client.get("/v1/documents", headers=headers,
                               params={"namespace": namespace, "limit": 10,
                                       "cursor": first["next_cursor"]})).json()
    # A new document that sorts last, and an update to one already returned.
    await _batch(client, workspace["headers"], namespace, ["z-last"])
    await client.put("/v1/documents/m-00", headers=workspace["headers"],
                     json={"content": "changed", "expected_version": 1})
    seen = [item["doc_key"] for item in first["items"] + second["items"]]
    cursor = second["next_cursor"]
    while cursor:
        page = (await client.get("/v1/documents", headers=headers,
                                 params={"namespace": namespace, "limit": 10,
                                         "cursor": cursor})).json()
        seen += [item["doc_key"] for item in page["items"]]
        cursor = page["next_cursor"]
    assert seen[-1] == "z-last"
    assert len(seen) == len(set(seen)) == 31


async def test_limit_and_cursor_errors(client, workspace):
    namespace = f"e{uuid.uuid4().hex[:6]}"
    headers = {**workspace["headers"], **V2}
    await _batch(client, workspace["headers"], namespace, [f"c-{i}" for i in range(5)])
    too_big = await client.get("/v1/documents", headers=headers,
                               params={"namespace": namespace, "limit": 501})
    assert too_big.status_code == 422
    assert too_big.json()["error"]["code"] == "validation_error"
    assert too_big.json()["error"]["details"] == {"field": "limit", "limit": 500}

    page = (await client.get("/v1/documents", headers=headers,
                             params={"namespace": namespace, "limit": 2})).json()
    cursor = page["next_cursor"]
    body, _ = cursor.split(".")
    payload = json.loads(_unb64(body))

    tampered = sign_list_cursor(payload)[:-2] + "AA"
    response = await client.get("/v1/documents", headers=headers,
                                params={"namespace": namespace, "limit": 2, "cursor": tampered})
    assert response.status_code == 400 and response.json()["error"]["code"] == "cursor_invalid"

    other_filter = await client.get("/v1/documents", headers=headers,
                                    params={"namespace": namespace, "limit": 2,
                                            "cursor": cursor, "include_archived": "true"})
    assert other_filter.status_code == 400
    assert other_filter.json()["error"]["details"]["reason"] == "filter_mismatch"

    payload["t"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    expired = await client.get("/v1/documents", headers=headers,
                               params={"namespace": namespace, "limit": 2,
                                       "cursor": sign_list_cursor(payload)})
    assert expired.status_code == 410
    assert expired.json()["error"]["code"] == "cursor_expired"


async def test_fields_projection_and_updated_order(client, workspace):
    namespace = f"f{uuid.uuid4().hex[:6]}"
    headers = {**workspace["headers"], **V2}
    await _batch(client, workspace["headers"], namespace, ["b", "a", "c"])
    await client.put("/v1/documents/a", headers=workspace["headers"],
                     json={"content": "touched later", "expected_version": 1})
    page = (await client.get(
        "/v1/documents", headers=headers,
        params={"namespace": namespace, "order": "updated_at_asc",
                "fields": "version,trust,content_hash"},
    )).json()
    assert [item["doc_key"] for item in page["items"]][-1] == "a"
    assert set(page["items"][0]) == {
        "id", "canonical_id", "namespace", "doc_key", "version", "trust", "content_hash"
    }
    bad = await client.get("/v1/documents", headers=headers,
                           params={"namespace": namespace, "fields": "content"})
    assert bad.status_code == 422 and bad.json()["error"]["details"]["field"] == "fields"

    # Baseline clients keep the 3.2.1 listing (newest first, no snapshot fields).
    legacy = (await client.get("/v1/documents", headers=workspace["headers"],
                               params={"namespace": namespace})).json()
    assert legacy["order"] is None and legacy["snapshot_count"] is None
    assert legacy["items"][0]["doc_key"] == "a"


async def test_snapshot_is_stable_under_concurrent_reads(client, workspace):
    namespace = f"s{uuid.uuid4().hex[:6]}"
    headers = {**workspace["headers"], **V2}
    await _batch(client, workspace["headers"], namespace, [f"r-{i}" for i in range(12)])
    pages = await asyncio.gather(*(
        client.get("/v1/documents", headers=headers, params={"namespace": namespace})
        for _ in range(4)
    ))
    assert {page.json()["snapshot_count"] for page in pages} == {12}
