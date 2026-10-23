"""Asynchronous indexing and write semantics (PRD AG-ST-13, AG-ST-06).

The test server runs with INDEX_WORKER_ENABLED=false; these tests drive the
worker in-process through ``indexer.process_jobs`` / ``_index_one``.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest

from app.services import indexer, retrieval

pytestmark = pytest.mark.integration

V2 = {"AgentGraph-Version": "2026-11"}


def _db() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def _fetch(sql: str, *args):
    conn = await asyncpg.connect(_db())
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def _put(client, headers, key, content, version):
    return await client.put(
        f"/v1/documents/{key}",
        headers=headers,
        json={"content": content, "expected_version": version},
    )


async def _events(workspace_id: str, event_type: str, doc_key: str):
    return await _fetch(
        "SELECT payload FROM timeline_streams WHERE workspace_id = $1::uuid "
        "AND event_type = $2 AND payload->>'doc_key' = $3",
        workspace_id,
        event_type,
        doc_key,
    )


async def _embedding_tokens(workspace_id: str) -> int:
    rows = await _fetch(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM usage_ledger "
        "WHERE workspace_id = $1::uuid AND category = 'embedding_tokens'",
        workspace_id,
    )
    return int(rows[0]["total"])


async def test_baseline_write_is_searchable_immediately(client, workspace):
    key = f"sync-{uuid.uuid4().hex[:6]}"
    response = await _put(client, workspace["headers"], key, "baseline indexing text", 0)
    assert response.status_code == 200, response.text
    document = response.json()["document"]
    assert document["index_state"] == "indexed"
    assert document["indexed_version"] == document["version"]


async def test_async_write_is_stored_then_indexed_with_event(client, workspace):
    workspace_id = workspace["workspace"]["id"]
    headers = {**workspace["headers"], **V2}
    key = f"async-{uuid.uuid4().hex[:6]}"
    before = await _embedding_tokens(workspace_id)
    response = await _put(client, headers, key, "asynchronously indexed content", 0)
    assert response.status_code == 200, response.text
    document = response.json()["document"]
    assert document["stored"] is True
    assert document["index_state"] == "pending"
    assert document["indexed_version"] is None
    assert await _embedding_tokens(workspace_id) == before  # nothing billed at write time

    status = (await client.get("/v1/index/status", headers=headers)).json()
    assert status["pending_jobs"] >= 1

    # Dereference works while the document is not yet searchable.
    read = await client.get(f"/v1/documents/{key}", headers=headers)
    assert read.status_code == 200
    assert read.json()["index_state"] == "pending"

    await indexer.process_jobs(limit=200)
    read = await client.get(f"/v1/documents/{key}", headers=headers)
    assert read.json()["index_state"] == "indexed"
    assert read.json()["indexed_version"] == 1
    assert len(await _events(workspace_id, "DOC_INDEXED", key)) == 1
    assert await _embedding_tokens(workspace_id) > before

    query = await client.post(
        "/v1/query", headers=headers, json={"query": "asynchronously indexed content"}
    )
    assert query.status_code == 200
    assert "index_lag" in query.json()


async def test_queued_writes_coalesce_into_one_embedding(client, workspace, monkeypatch):
    headers = {**workspace["headers"], **V2}
    key = f"coalesce-{uuid.uuid4().hex[:6]}"
    for version in range(5):
        response = await _put(client, headers, key, f"revision number {version}", version)
        assert response.status_code == 200, response.text
    document_id = (
        await _fetch("SELECT id FROM shared_documents WHERE doc_key = $1", key)
    )[0]["id"]
    calls: list[int] = []
    original = retrieval.reindex_document_chunks

    async def counting(session, document):
        if document.id == document_id:
            calls.append(document.version)
        return await original(session, document)

    monkeypatch.setattr(retrieval, "reindex_document_chunks", counting)
    await indexer.process_jobs(limit=200)
    assert calls == [5]
    row = (
        await _fetch(
            "SELECT state, indexed_version, superseded FROM document_index WHERE document_id = $1",
            document_id,
        )
    )[0]
    assert row["state"] == "indexed" and row["indexed_version"] == 5
    assert row["superseded"] == 4
    status = (await client.get("/v1/index/status", headers=headers)).json()
    assert status["superseded_total"] >= 4


async def test_repeated_failures_end_failed_with_event(client, workspace, monkeypatch):
    workspace_id = workspace["workspace"]["id"]
    headers = {**workspace["headers"], **V2}
    key = f"fail-{uuid.uuid4().hex[:6]}"
    response = await _put(client, headers, key, "provider will fail on this", 0)
    assert response.status_code == 200
    document_id = (
        await _fetch("SELECT id FROM shared_documents WHERE doc_key = $1", key)
    )[0]["id"]

    async def broken(session, document):
        raise RuntimeError("embedding provider unavailable")

    monkeypatch.setattr(indexer.settings, "index_max_attempts", 3)
    for _ in range(3):
        await indexer._index_one(document_id, uuid.UUID(workspace_id), broken)
    read = await client.get(f"/v1/documents/{key}", headers=headers)
    assert read.status_code == 200  # still readable by key
    assert read.json()["index_state"] == "failed"
    events = await _events(workspace_id, "DOC_INDEX_FAILED", key)
    assert len(events) == 1
    status = (await client.get("/v1/index/status", headers=headers)).json()
    assert status["failed_jobs"] >= 1


async def test_identical_write_is_a_noop(client, workspace):
    key = f"noop-{uuid.uuid4().hex[:6]}"
    first = await _put(client, workspace["headers"], key, "same content", 0)
    assert first.status_code == 200
    again = await _put(client, workspace["headers"], key, "same content", 1)
    assert again.status_code == 200, again.text
    assert again.json()["noop"] is True
    assert again.json()["document"]["version"] == 1
    changed = await _put(client, workspace["headers"], key, "different content", 1)
    assert changed.json()["noop"] is False
    assert changed.json()["document"]["version"] == 2


async def test_concurrent_writers_never_see_unexpected_5xx(client, workspace):
    headers = {**workspace["headers"], **V2}
    prefix = uuid.uuid4().hex[:6]
    responses = await asyncio.gather(
        *(
            _put(client, headers, f"load-{prefix}-{index}", f"load content {index}", 0)
            for index in range(40)
        )
    )
    statuses = {response.status_code for response in responses}
    assert statuses <= {200, 429, 503}, statuses
    for response in responses:
        if response.status_code == 503:
            assert response.json()["error"]["code"] == "overloaded"
            assert response.headers.get("Retry-After")


async def _chunk_payers(workspace_id: str, since) -> list[str]:
    rows = await _fetch(
        "SELECT metadata->>'principal_key' AS payer FROM usage_ledger "
        "WHERE workspace_id = $1::uuid AND category = 'embedding_tokens' "
        "AND metadata->>'kind' = 'chunks' AND created_at > $2 ORDER BY created_at",
        workspace_id, since,
    )
    return [row["payer"] for row in rows]


async def test_rebuilding_an_index_is_not_billed_to_the_last_writer(client, workspace):
    """A cost anomaly fired on a token with 0 requests: its old documents were
    being re-embedded. First indexing bills the writer; a rebuild bills the platform."""
    workspace_id = workspace["workspace"]["id"]
    headers = {**workspace["headers"], **V2}
    key = f"rebuild-{uuid.uuid4().hex[:6]}"
    start = (await _fetch("SELECT NOW() AS now"))[0]["now"]
    response = await _put(client, headers, key, "content indexed once, then rebuilt", 0)
    assert response.status_code == 200, response.text
    await indexer.process_jobs()
    first = await _chunk_payers(workspace_id, start)
    assert first and "system:reindex" not in first

    middle = (await _fetch("SELECT NOW() AS now"))[0]["now"]
    await _fetch(
        "UPDATE document_index SET state = 'pending', next_attempt_at = NOW() "
        "WHERE document_id = (SELECT id FROM shared_documents "
        "WHERE workspace_id = $1::uuid AND doc_key = $2)", workspace_id, key)
    await indexer.process_jobs()
    assert await _chunk_payers(workspace_id, middle) == ["system:reindex"]
