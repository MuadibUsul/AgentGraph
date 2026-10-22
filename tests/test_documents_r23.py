"""Batch semantics (AG-ST-07), archive (AG-ST-08), frontmatter filters (AG-ST-09),
log documents and hot-document protection (AG-ST-14), human edits (AG-TC-03)
and situation alerts (AG-TC-05)."""

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
V2026 = {"AgentGraph-Version": "2026-11"}
PASSWORD = "correct-horse-battery"


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
async def r23(api_base_url) -> AsyncIterator[httpx.AsyncClient]:
    del api_base_url
    async with run_server({"SITUATION_INTERVAL_SECONDS": "5"}, ".server-r23.log") as base:
        async with httpx.AsyncClient(base_url=base, timeout=120) as http:
            yield http


async def _workspace(client) -> dict:
    created = await client.post("/v1/admin/workspaces", headers=ADMIN,
                                json={"name": f"R23 {uuid.uuid4().hex[:6]}", "tier": "pro"})
    body = created.json()
    email = f"owner-{uuid.uuid4().hex[:8]}@example.test"
    owner = await client.post(
        f"/v1/admin/workspaces/{body['workspace']['id']}/human-users", headers=ADMIN,
        json={"email": email, "display_name": "Owner", "role": "OWNER", "password": PASSWORD})
    login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    return {"id": body["workspace"]["id"], "key": {**_bearer(body["api_key"]), **V2026},
            "owner_id": owner.json()["user"]["id"],
            "human": {**_bearer(login.json()["token"]), **V2026}}


async def _agent(client, ws: dict, agent_id: str) -> dict:
    minted = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
        json={"agent_signature": f"{agent_id}_sig", "role_scope": "EXECUTOR"})
    headers = {**_bearer(minted.json()["api_key"]), **V2026}
    registered = await client.post("/v1/agents/register", headers=headers,
                                   json={"agent_id": agent_id})
    assert registered.status_code == 201, registered.text
    return {"headers": headers, "signature": f"{agent_id}_sig", "agent_id": agent_id}


async def _put(client, headers, key, content, version=0, **extra):
    return await client.put(f"/v1/documents/{key}", headers=headers,
                            json={"content": content, "expected_version": version, **extra})


async def test_batch_semantics(r23):
    """AG-ST-07 acceptance 1-5."""
    client = r23
    ws = await _workspace(client)
    items = [{"doc_key": f"bulk-{index:02d}", "content": f"bulk {index}",
              "expected_version": 0} for index in range(25)]
    created = await client.post("/v1/documents:batchUpsert", headers=ws["key"],
                                json={"items": items})
    assert created.status_code == 200, created.text
    assert created.json()["succeeded"] == 25 and created.json()["failed"] == 0
    too_many = await client.post("/v1/documents:batchUpsert", headers=ws["key"],
                                 json={"items": items + [{**items[0], "doc_key": "x"}]})
    assert too_many.status_code == 422

    update = [{"doc_key": f"bulk-{index:02d}", "content": f"bulk {index} v2",
               "expected_version": 1} for index in range(25)]
    update[6]["expected_version"] = 9
    atomic = await client.post("/v1/documents:batchUpsert", headers=ws["key"],
                               json={"items": update, "atomic": True})
    assert atomic.status_code == 409
    assert atomic.json()["error"]["details"]["failed_indexes"] == [6]
    unchanged = await client.get("/v1/documents/bulk-00?include_unverified=true",
                                 headers=ws["key"])
    assert unchanged.json()["version"] == 1
    partial = await client.post("/v1/documents:batchUpsert", headers=ws["key"],
                                json={"items": update})
    assert partial.status_code == 200, partial.text
    body = partial.json()
    assert body["succeeded"] == 24 and body["failed"] == 1
    assert body["results"][6]["ok"] is False
    assert body["results"][6]["error"]["details"]["current_version"] == 1
    assert [item["index"] for item in body["results"]] == list(range(25))
    duplicate = await client.post("/v1/documents:batchUpsert", headers=ws["key"], json={
        "items": [items[0], {**items[1], "doc_key": "x1"}, items[0]]})
    assert duplicate.status_code == 422
    assert duplicate.json()["error"]["details"]["indexes"] == [0, 2]

    keys = [{"doc_key": f"bulk-{index:02d}"} for index in range(23)]
    keys.insert(4, {"doc_key": "missing-a"})
    keys.append({"doc_key": "missing-b"})
    read = await client.post("/v1/documents:batchGet", headers=ws["key"],
                             json={"keys": keys, "include_unverified": True,
                                   "fields": ["content"]})
    results = read.json()["results"]
    assert [item["ok"] for item in results].count(True) == 23
    assert results[4]["ok"] is False and results[4]["error"]["code"] == "not_found"
    assert results[5]["document"]["doc_key"] == "bulk-04"
    assert set(results[5]["document"]) <= {"content", "id", "canonical_id", "namespace",
                                           "doc_key", "version"}


async def test_archive_versions_and_refusals(r23):
    """AG-ST-08 acceptance 1-4."""
    client = r23
    ws = await _workspace(client)
    alice = await _agent(client, ws, "alice-a")
    bob = await _agent(client, ws, "bob-a")
    await _put(client, alice["headers"], "leftover", "test leftover about pelicans")
    no_reason = await client.post("/v1/documents/leftover/archive", headers=alice["headers"],
                                  json={})
    assert no_reason.status_code == 422
    stranger = await client.post("/v1/documents/leftover/archive", headers=bob["headers"],
                                 json={"reason": "not mine"})
    assert stranger.status_code == 403
    archived = await client.post("/v1/documents/leftover/archive", headers=alice["headers"],
                                 json={"reason": "test leftover"})
    assert archived.status_code == 200, archived.text
    assert archived.json()["document"]["archived"] is True
    assert archived.json()["document"]["version"] == 2
    listed = await client.get("/v1/documents", headers=alice["headers"])
    assert "leftover" not in [item["doc_key"] for item in listed.json()["items"]]
    with_archived = await client.get("/v1/documents?include_archived=true",
                                     headers=alice["headers"])
    entry = next(item for item in with_archived.json()["items"] if item["doc_key"] == "leftover")
    assert entry["archived"] is True
    write = await _put(client, alice["headers"], "leftover", "again", 2)
    assert write.status_code == 409 and write.json()["error"]["code"] == "document_archived"
    restored = await client.post("/v1/documents/leftover/restore", headers=alice["headers"],
                                 json={"reason": "needed after all"})
    assert restored.json()["document"]["version"] == 3
    audits = await _sql("SELECT count(*) FROM state_audit_diffs a JOIN shared_documents d ON "
                        "d.id = a.target_record_id WHERE d.doc_key = 'leftover' AND "
                        "a.workspace_id = $1::uuid", ws["id"])
    assert audits[0][0] >= 3


async def test_frontmatter_filters(r23):
    """AG-ST-09 acceptance 1-5."""
    client = r23
    ws = await _workspace(client)
    namespace = "default"
    items = [{"doc_key": f"deal-{amount}", "namespace": namespace, "content": f"deal {amount}",
              "frontmatter": {"amount": amount, "tags": ["infra"] if amount % 200 == 0 else []},
              "expected_version": 0} for amount in range(100, 1001, 100)]
    items.append({"doc_key": "deal-odd", "namespace": namespace, "content": "odd",
                  "frontmatter": {"amount": "abc"}, "expected_version": 0})
    items += [{"doc_key": "due-soon", "namespace": namespace, "content": "soon",
               "frontmatter": {"due": "2026-10-25", "status": "open"}, "expected_version": 0},
              {"doc_key": "due-later", "namespace": namespace, "content": "later",
               "frontmatter": {"due": "2099-11-30", "status": "blocked"},
               "expected_version": 0},
              {"doc_key": "due-none", "namespace": namespace, "content": "none",
               "frontmatter": {"status": "open"}, "expected_version": 0}]
    created = await client.post("/v1/documents:batchUpsert", headers=ws["key"],
                                json={"items": items})
    assert created.json()["succeeded"] == len(items)

    async def keys(filters):
        response = await client.post("/v1/query", headers=ws["key"], json={
            "namespace": namespace, "filters": filters, "include_unverified": True,
            "limit": 50})
        assert response.status_code == 200, response.text
        return sorted(item["doc_key"] for item in response.json()["items"])

    assert len(await keys({"amount": {"gt": 500}})) == 5
    assert await keys({"due": {"lte": "now+30d"}}) == ["due-soon"]
    assert await keys({"due": {"exists": False}, "status": "open"}) == ["due-none"]
    assert await keys({"status": {"in": ["open", "blocked"]}, "due": {"exists": True}}) == [
        "due-later", "due-soon"]
    assert len(await keys({"tags": {"contains": "infra"}})) == 5
    assert await keys({"$or": [{"amount": {"lt": 200}}, {"status": "blocked"}]}) == [
        "deal-100", "due-later"]
    assert "deal-odd" not in await keys({"amount": {"gt": 1}})
    bad = await client.post("/v1/query", headers=ws["key"], json={
        "namespace": namespace, "filters": {"amount": {"gtx": 1}}})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_filter"
    assert bad.json()["error"]["details"]["path"] == "amount.gtx"
    legacy = await client.post("/v1/query", headers={k: v for k, v in ws["key"].items()
                                                     if k != "AgentGraph-Version"},
                               json={"namespace": namespace, "filters": {"amount": {"gtx": 1}}})
    assert legacy.json()["error"]["code"] == "invalid_filter_operator"


async def test_log_documents_and_contention(r23):
    """AG-ST-14 acceptance 1-5."""
    client = r23
    ws = await _workspace(client)
    created = await _put(client, ws["key"], "progress-log", "# Progress", doc_type="log")
    assert created.status_code in (200, 201), created.text
    writers = [await _agent(client, ws, f"writer-{index:02d}") for index in range(20)]

    async def burst(writer):
        for round_ in range(5):
            response = await client.post(
                "/v1/documents/progress-log/entries", headers=writer["headers"],
                json={"entries": [{"body": f"{writer['agent_id']} {round_}-{index}"}
                                  for index in range(10)]})
            assert response.status_code == 201, response.text

    await asyncio.gather(*(burst(writer) for writer in writers))
    seqs = []
    after = 0
    while True:
        page = (await client.get("/v1/documents/progress-log/entries",
                                 headers=ws["key"], params={"after_seq": after,
                                                            "limit": 500})).json()
        seqs += [entry["seq"] for entry in page["entries"]]
        if not page["has_more"]:
            break
        after = page["next_after_seq"]
    assert seqs == list(range(1, 1001))
    overwrite = await _put(client, ws["key"], "progress-log", "overwrite", 1)
    assert overwrite.status_code == 409
    assert overwrite.json()["error"]["code"] == "log_document_append_only"
    deref = (await client.get("/v1/documents/progress-log?include_unverified=true",
                              headers=ws["key"])).json()
    assert deref["entries_total"] == 1000 and len(deref["entries"]) == 100

    await _put(client, ws["key"], "small-log", "# Small", doc_type="log")
    for index in range(100):
        await client.post("/v1/documents/small-log/entries", headers=ws["key"],
                          json={"entries": [{"body": f"entry {index}"}]})
    batches = await _sql("SELECT log_index_batches FROM shared_documents WHERE doc_key = "
                         "'small-log' AND workspace_id = $1::uuid", ws["id"])
    assert 1 <= batches[0][0] <= 5

    await _put(client, ws["key"], "plan", "# Plan\n\n## A\n\nalpha\n\n## B\n\nbeta\n")
    sections = (await client.get("/v1/documents/plan/sections", headers=ws["key"])).json()
    hashes = {item["heading"]: item["section_hash"] for item in sections["sections"]}
    patch_a, patch_b = await asyncio.gather(*(
        client.post("/v1/documents/plan/patch-section", headers=ws["key"],
                    json={"section_heading": heading, "new_markdown": f"{heading} changed",
                          "section_hash": hashes[heading]})
        for heading in ("A", "B")))
    assert patch_a.status_code == 200 and patch_b.status_code == 200
    plan = (await client.get("/v1/documents/plan?include_unverified=true",
                             headers=ws["key"])).json()
    assert plan["version"] == 3
    assert "A changed" in plan["content"] and "B changed" in plan["content"]
    stale = await client.post("/v1/documents/plan/patch-section", headers=ws["key"],
                              json={"section_heading": "A", "new_markdown": "late",
                                    "section_hash": hashes["A"]})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "section_conflict"

    agent = await _agent(client, ws, "racer")
    for _ in range(6):
        await _put(client, agent["headers"], "plan", "overwrite", 1)
    contended = await _put(client, agent["headers"], "plan", "overwrite", 3)
    assert contended.status_code == 409
    error = contended.json()["error"]
    assert error["code"] == "contention" and error["details"]["suggest"]
    assert contended.headers.get("Retry-After")


async def test_human_edit_semantics(r23):
    """AG-TC-03 acceptance 1, 2, 4."""
    client = r23
    ws = await _workspace(client)
    keeper = await _agent(client, ws, "keeper")
    await _put(client, keeper["headers"], "runbook", "v1",
               frontmatter={"maintainer": "agent:keeper"})
    for version in range(1, 4):
        await _put(client, keeper["headers"], "runbook", f"v{version + 1}", version,
                   frontmatter={"maintainer": "agent:keeper"})
    cursor = (await client.get("/v1/events", headers=keeper["headers"],
                               params={"limit": 500})).json()["next_cursor"]
    saved = await client.put("/v1/human/documents/runbook", headers=ws["human"],
                             json={"content": "v5 by a person", "expected_version": 4,
                                   "lock": False})
    assert saved.status_code == 200, saved.text
    document = saved.json()["document"]
    assert document["version"] == 5 and document["last_human_edit"]["version"] == 5
    mine = (await client.get("/v1/events", headers=keeper["headers"],
                             params={"cursor": cursor, "for_me": "true"})).json()
    updated = [event for event in mine["events"] if event["type"] == "doc.updated"]
    assert updated and updated[0]["actor"]["type"] == "human"
    versions = await _sql("SELECT human_edited FROM document_versions v JOIN shared_documents d "
                          "ON d.id = v.document_id WHERE d.doc_key = 'runbook' AND "
                          "d.workspace_id = $1::uuid AND v.version = 5", ws["id"])
    assert versions[0][0] is True

    late = await _put(client, keeper["headers"], "runbook", "agent overwrite", 4)
    assert late.status_code == 409
    assert late.json()["error"]["details"]["updated_by_type"] == "human"
    assert late.json()["error"]["details"]["current_version"] == 5

    locked = await client.put("/v1/human/documents/runbook", headers=ws["human"],
                              json={"content": "v6 locked", "expected_version": 5,
                                    "lock": True})
    assert locked.json()["document"]["locked_by_human"] is True
    blocked = await _put(client, keeper["headers"], "runbook", "try", 6)
    assert blocked.status_code in (403, 409, 423)
    proposed = await client.post("/v1/documents/runbook/proposals", headers=keeper["headers"],
                                 json={"content": "suggestion", "base_version": 6,
                                       "note": "please consider"})
    assert proposed.status_code in (200, 201), proposed.text


async def test_situation_alerts(r23):
    """AG-TC-05 acceptance 1, 2, 4."""
    client = r23
    ws = await _workspace(client)
    looper = await _agent(client, ws, "looper")
    other = await _agent(client, ws, "other-s")
    await _put(client, looper["headers"], "hot", "v1")
    await _put(client, looper["headers"], "hot", "v2", 1)
    for _ in range(40):
        await _put(client, looper["headers"], "hot", "stale", 1)
    task = await client.post("/v1/tasks", headers=ws["key"], json={"title": "Nobody wants it"})
    await _sql("UPDATE collab_tasks SET updated_at = NOW() - INTERVAL '3 hours' WHERE "
               "task_key = $1 AND workspace_id = $2::uuid", task.json()["task_key"], ws["id"])

    found = {}
    for _ in range(60):
        listed = (await client.get("/v1/alerts", headers=ws["human"])).json()["items"]
        found = {item["rule"]: item for item in listed}
        if {"retry_storm", "unclaimed_task"} <= set(found):
            break
        await asyncio.sleep(1)
    assert found["retry_storm"]["severity"] == "high"
    assert found["unclaimed_task"]["subject"]["ref"] == task.json()["task_key"]
    raised = await _sql("SELECT targets FROM workspace_events WHERE workspace_id = $1::uuid "
                        "AND type = 'alert.raised'", ws["id"])
    assert any(f"human:{ws['owner_id']}" in json.loads(row["targets"]) for row in raised)
    assert len([row for row in raised]) == 1  # debounced: one notification per 30 minutes

    drill = (await client.get(f"/v1/metrics?task={task.json()['task_key']}",
                              headers=ws["human"])).json()
    assert drill["waiting_seconds"] >= 3 * 3600 - 60 and drill["timeline"]
    mine = (await client.get("/v1/metrics", headers=looper["headers"])).json()
    assert mine["conflicts"] >= 30
    other_metrics = await client.get(f"/v1/metrics?principal={mine['principal']}",
                                     headers=other["headers"])
    assert other_metrics.status_code == 403
    by_agent = await client.post(f"/v1/alerts/{found['retry_storm']['alert_id']}/ack",
                                 headers=looper["headers"], json={})
    assert by_agent.status_code == 403
    acked = await client.post(f"/v1/alerts/{found['retry_storm']['alert_id']}/ack",
                              headers=ws["human"], json={"note": "looking"})
    assert acked.json()["state"] == "acked"
