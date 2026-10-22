"""Unified event log, long poll, sync changes and subscriptions (PRD AG-EV-01/02)."""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.integration

V2 = {"AgentGraph-Version": "2026-11"}


def _db() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def _agent(client, admin_headers, workspace, namespaces=None):
    body = {"agent_signature": f"ev_{uuid.uuid4().hex[:6]}", "role_scope": "EXECUTOR"}
    if namespaces is not None:
        body["allowed_namespaces"] = namespaces
    response = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        headers=admin_headers, json=body,
    )
    assert response.status_code in (200, 201), response.text
    return {"Authorization": f"Bearer {response.json()['api_key']}"}, body["agent_signature"]


async def _put(client, headers, key, content, version=0, namespace=None, **extra):
    body = {"content": content, "expected_version": version, **extra}
    if namespace:
        body["namespace"] = namespace
    response = await client.put(f"/v1/documents/{key}", headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()["document"]


async def _head(client, headers):
    return (await client.get("/v1/events", headers=headers)).json()["next_cursor"]


async def test_long_poll_wakes_on_write_and_times_out(client, workspace):
    headers = workspace["headers"]
    cursor = await _head(client, headers)

    async def write_later():
        await asyncio.sleep(2)
        await _put(client, headers, f"wake-{uuid.uuid4().hex[:6]}", "wake up")
        return time.monotonic()

    writer = asyncio.create_task(write_later())
    started = time.monotonic()
    response = await client.get("/v1/events", headers=headers,
                                params={"cursor": cursor, "wait_seconds": 25,
                                        "types": "doc.created"})
    returned = time.monotonic()
    written = await writer
    assert response.status_code == 200
    events = response.json()["events"]
    assert [event["type"] for event in events] == ["doc.created"]
    assert returned - written <= 1.0
    assert returned - started < 10
    event = events[0]
    assert event["event_id"].startswith("evt_") and event["refs"]["doc_id"].startswith("doc_")
    assert "content" not in event and event["actor"]["chain"]

    quiet = time.monotonic()
    empty = await client.get("/v1/events", headers=headers,
                             params={"cursor": response.json()["next_cursor"],
                                     "wait_seconds": 3, "types": "doc.created"})
    assert empty.json()["events"] == []
    assert 2.5 <= time.monotonic() - quiet <= 4.5


async def test_seq_is_gap_free_under_concurrent_writes(client, workspace):
    headers = {**workspace["headers"], **V2}
    cursor = await _head(client, headers)
    namespace = f"burst{uuid.uuid4().hex[:4]}"

    async def batch(start):
        response = await client.post("/v1/documents/batch", headers=workspace["headers"], json={
            "items": [{"doc_key": f"b-{start + index:04d}", "namespace": namespace,
                       "content": f"burst {start + index}", "expected_version": 0}
                      for index in range(25)],
        })
        assert response.status_code == 200, response.text

    await asyncio.gather(*(batch(start) for start in range(0, 1000, 25)))
    seen, created = [], 0
    while True:
        page = (await client.get("/v1/events", headers=headers,
                                 params={"cursor": cursor, "limit": 500})).json()
        seen += [event["seq"] for event in page["events"]]
        created += sum(event["type"] == "doc.created" and event["namespace"] == namespace
                       for event in page["events"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert created == 1000
    assert seen == list(range(seen[0], seen[0] + len(seen)))  # strictly increasing, no gaps


async def test_sync_since_returns_each_document_once(client, admin_headers, workspace):
    headers = {**workspace["headers"], **V2}
    namespace = f"goals{uuid.uuid4().hex[:4]}"
    base = await _put(client, workspace["headers"], "base", "base v1", namespace=namespace)
    to_archive = await _put(client, workspace["headers"], "old", "old", namespace=namespace)
    first = (await client.get("/v1/sync", headers=headers)).json()
    c1 = first["sync_cursor"]
    assert c1.startswith("ev1.")
    for name in ("one", "two", "three"):
        await _put(client, workspace["headers"], name, f"{name} body", namespace=namespace)
    archived = await client.post(f"/v1/documents/{to_archive['doc_key']}/archive",
                                 headers=workspace["headers"], json={})
    assert archived.status_code == 200
    for version in range(1, 6):
        await _put(client, workspace["headers"], base["doc_key"], f"base v{version + 1}",
                   version=version)
    since = (await client.get("/v1/sync", headers=headers,
                              params={"since": c1, "namespaces": namespace})).json()
    changes = since["changes"]
    keys = [change["doc_key"] for change in changes]
    assert sorted(keys) == sorted(["one", "two", "three", "old", "base"])
    by_key = {change["doc_key"]: change for change in changes}
    assert by_key["base"]["version"] == 6 and by_key["base"]["op"] == "upsert"
    assert by_key["old"]["op"] == "archive"

    # A poll_events cursor and a sync cursor are the same thing.
    polled = (await client.get("/v1/events", headers=headers,
                               params={"cursor": c1, "limit": 500})).json()
    again = (await client.get("/v1/sync", headers=headers,
                              params={"since": polled["next_cursor"]})).json()
    assert again["changes"] == []

    counts = {row["namespace"]: row for row in since["namespace_counts"]}
    census = (await client.get("/v1/documents", headers=headers,
                               params={"namespace": namespace})).json()
    assert counts[namespace]["documents"] == census["snapshot_count"] == 4
    assert counts[namespace]["archived"] == 1


async def test_events_respect_namespace_permissions(client, admin_headers, workspace):
    ideas_only, _ = await _agent(client, admin_headers, workspace, namespaces=["ideas"])
    cursor = await _head(client, ideas_only)
    await _put(client, workspace["headers"], f"g-{uuid.uuid4().hex[:4]}", "secret goal",
               namespace="goals")
    await _put(client, workspace["headers"], f"i-{uuid.uuid4().hex[:4]}", "an idea",
               namespace="ideas")
    events = (await client.get("/v1/events", headers=ideas_only,
                               params={"cursor": cursor, "types": "doc.*"})).json()["events"]
    assert {event["namespace"] for event in events} == {"ideas"}


async def test_expired_and_invalid_cursors(client, workspace):
    headers = workspace["headers"]
    cursor = await _head(client, headers)
    for index in range(3):
        await _put(client, headers, f"exp-{uuid.uuid4().hex[:4]}-{index}", "x")
    conn = await asyncpg.connect(_db())
    try:
        # Retention removed the first two events after the cursor.
        await conn.execute(
            "DELETE FROM workspace_events WHERE workspace_id = $1::uuid AND seq IN "
            "(SELECT seq FROM workspace_events WHERE workspace_id = $1::uuid "
            "ORDER BY seq LIMIT 2 OFFSET 0)",
            workspace["workspace"]["id"],
        )
        oldest = await conn.fetchval(
            "SELECT MIN(seq) FROM workspace_events WHERE workspace_id = $1::uuid",
            workspace["workspace"]["id"])
    finally:
        await conn.close()
    from app.services.events import encode_cursor

    expired = await client.get("/v1/events", headers=headers,
                               params={"cursor": encode_cursor(oldest - 3)})
    assert expired.status_code == 410
    assert expired.json()["error"]["code"] == "cursor_expired"
    assert expired.json()["error"]["details"]["resync"] == "workspace_sync(since=null)"
    assert (await client.get("/v1/events", headers=headers, params={"cursor": cursor})
            ).status_code in (200, 410)
    bad = await client.get("/v1/events", headers=headers, params={"cursor": "ev1.!!"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "cursor_invalid"
    too_long = await client.get("/v1/events", headers=headers, params={"wait_seconds": 26})
    assert too_long.status_code == 422


async def test_writes_and_events_commit_together(client, workspace):
    namespace = f"atom{uuid.uuid4().hex[:4]}"
    for index in range(5):
        await _put(client, workspace["headers"], f"a-{index}", "x", namespace=namespace)
    conn = await asyncpg.connect(_db())
    try:
        orphans = await conn.fetchval(
            "SELECT count(*) FROM shared_documents d WHERE d.workspace_id = $1::uuid "
            "AND d.namespace = $2 AND NOT EXISTS (SELECT 1 FROM workspace_events e "
            "WHERE e.record_id = d.id AND e.type = 'doc.created')",
            workspace["workspace"]["id"], namespace,
        )
    finally:
        await conn.close()
    assert orphans == 0


async def test_prefix_subscription_and_addressed_events(client, admin_headers, workspace):
    agent, signature = await _agent(client, admin_headers, workspace)
    subscribed = await client.post("/v1/subscriptions", headers=agent,
                                   json={"prefixes": ["research/2026-"], "name": "r26"})
    assert subscribed.status_code == 201, subscribed.text
    subscription = subscribed.json()
    await _put(client, workspace["headers"], "2025-x", "old year", namespace="research")
    await _put(client, workspace["headers"], "2026-y", "this year", namespace="research")
    # Addressed to the agent in a namespace outside its subscription.
    assigned = await client.post("/v1/tasks", headers=workspace["headers"],
                                 json={"title": "for you", "namespace": "ops",
                                       "assignee": f"agent:{signature}"})
    assert assigned.status_code == 201, assigned.text
    events = (await client.get("/v1/events", headers=agent, params={
        "subscription_id": subscription["subscription_id"],
        "cursor": subscription["cursor"],
    })).json()["events"]
    received = [(event["type"], event["refs"].get("alias")) for event in events]
    assert ("doc.created", "2026-y") in received
    assert not any(alias == "2025-x" for _, alias in received)
    assert ("task.assigned", assigned.json()["task_key"]) in received
    mine = (await client.get("/v1/events", headers=agent, params={
        "cursor": subscription["cursor"], "for_me": "true",
    })).json()["events"]
    assert [event["type"] for event in mine] == ["task.assigned"]

    listed = (await client.get("/v1/subscriptions", headers=agent)).json()["items"]
    assert [item["name"] for item in listed] == ["r26"]
    removed = await client.delete(f"/v1/subscriptions/{subscription['subscription_id']}",
                                  headers=agent)
    assert removed.status_code == 204


async def test_orientation_returns_a_default_subscription(client, admin_headers, workspace):
    agent, _ = await _agent(client, admin_headers, workspace, namespaces=["ideas", "goals"])
    pack = (await client.get("/v1/orientation", headers=agent)).json()
    default = pack["events"]["default_subscription"]
    assert default["is_default"] is True
    assert sorted(default["prefixes"]) == ["goals/", "ideas/"]
    assert default["cursor"].startswith("ev1.")
    from app.agent_guide import sections

    catalog = sections()["events"]
    assert "doc.created" in catalog["types"] and catalog["retention_days"] == 7
