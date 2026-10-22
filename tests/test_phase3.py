"""Phase 3 (R3.1): AG-ST-11 lifecycle, AG-EV-04 watermarks and digests,
AG-EV-05 webhook delta, AG-CO-06 peer reviews, AG-CO-07 scarce resources,
AG-CO-12 amendment display."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest

from tests.conftest import run_server

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}
PASSWORD = "correct-horse-battery"
V2 = {"AgentGraph-Version": "2026-11"}


def _dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(scope="module")
async def phase3(api_base_url) -> AsyncIterator[httpx.AsyncClient]:
    del api_base_url
    async with run_server({"LEASE_REAPER_INTERVAL_SECONDS": "5"},
                          ".server-phase3.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as http:
            yield http


async def _sql(query: str, *args):
    conn = await asyncpg.connect(_dsn())
    try:
        return await conn.fetch(query, *args)
    finally:
        await conn.close()


async def _workspace(client) -> dict:
    created = await client.post("/v1/admin/workspaces", headers=ADMIN,
                                json={"name": f"P3 {uuid.uuid4().hex[:6]}", "tier": "pro"})
    body = created.json()
    email = f"owner-{uuid.uuid4().hex[:8]}@example.test"
    owner = await client.post(
        f"/v1/admin/workspaces/{body['workspace']['id']}/human-users", headers=ADMIN,
        json={"email": email, "display_name": "Owner", "role": "OWNER", "password": PASSWORD})
    login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    return {"id": body["workspace"]["id"], "key": _bearer(body["api_key"]),
            "owner_id": owner.json()["user"]["id"], "human": _bearer(login.json()["token"])}


async def _agent(client, ws: dict, agent_id: str, *, role: str | None = None) -> dict:
    minted = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
        json={"agent_signature": f"{agent_id}_sig", "role_scope": "EXECUTOR"})
    assert minted.status_code in (200, 201), minted.text
    headers = _bearer(minted.json()["api_key"])
    registered = await client.post("/v1/agents/register", headers=headers, json={
        "agent_id": agent_id, "role": role, "capabilities": []})
    assert registered.status_code == 201, registered.text
    return {"headers": headers, "signature": f"{agent_id}_sig", "agent_id": agent_id,
            "token": registered.json().get("token_ids", [None])[0]}


async def _put(client, headers: dict, key: str, content: str, version: int = 0,
               frontmatter: dict | None = None) -> httpx.Response:
    return await client.put(f"/v1/documents/{key}", headers=headers, json={
        "content": content, "expected_version": version, "frontmatter": frontmatter or {}})


async def test_lifecycle_supersede_expire_and_queries(phase3):
    """AG-ST-11: reserved fields, supersede, default exclusion and excluded counts."""
    client = phase3
    ws = await _workspace(client)
    headers = {**ws["key"], **V2}
    computed = await _put(client, headers, "lc-bad", "x", frontmatter={"lifecycle": "active"})
    assert computed.status_code == 422
    assert computed.json()["error"]["details"]["field"] == "frontmatter.lifecycle"
    bad_date = await _put(client, headers, "lc-bad", "x", frontmatter={"expires_at": "soon"})
    assert bad_date.status_code == 422

    for key in ("lc-old", "lc-new", "lc-live"):
        assert (await _put(client, headers, key, f"plan {key}")).status_code in (200, 201)
    expired = await _put(client, headers, "lc-expired", "plan expired",
                         frontmatter={"expires_at": "2020-01-01T00:00:00Z"})
    assert expired.status_code in (200, 201)

    superseded = await client.post("/v1/supersessions", headers=headers, json={
        "old": {"doc_key": "lc-old"}, "new": {"doc_key": "lc-new"}, "reason": "rewritten"})
    assert superseded.status_code == 200, superseded.text
    assert superseded.json()["old"]["version"] == 2
    assert superseded.json()["new"]["supersedes"]["doc_key"] == "lc-old"

    old = await client.get("/v1/documents/lc-old", headers=headers)
    assert old.json()["lifecycle"] == "superseded"
    assert old.json()["superseded_by"]["doc_key"] == "lc-new"
    assert (await client.get("/v1/documents/lc-expired", headers=headers)
            ).json()["lifecycle"] == "expired"

    async def query(**extra):
        response = await client.post("/v1/query", headers=headers, json={
            "filters": {}, "include_unverified": True, "limit": 50, **extra})
        assert response.status_code == 200, response.text
        return response.json()

    default = await query()
    keys = {item["doc_key"] for item in default["items"]}
    assert {"lc-new", "lc-live"} <= keys and not {"lc-old", "lc-expired"} & keys
    assert default["excluded"].get("superseded") == 1
    assert default["excluded"].get("expired") == 1
    wide = await query(include_lifecycle=["superseded", "expired"])
    assert {"lc-old", "lc-expired"} <= {item["doc_key"] for item in wide["items"]}

    await _sql("UPDATE shared_documents SET updated_at = NOW() - INTERVAL '120 days', "
               "last_read_at = NULL WHERE doc_key = 'lc-live' AND workspace_id = $1::uuid",
               ws["id"])
    cold = await query()
    assert "lc-live" not in {item["doc_key"] for item in cold["items"]}
    assert cold["excluded"].get("cold") == 1
    read = await client.get("/v1/documents/lc-live", headers=headers)
    assert read.json()["lifecycle"] == "active"
    await asyncio.sleep(0.5)
    assert "lc-live" in {item["doc_key"] for item in (await query())["items"]}

    legacy = await client.post("/v1/query", headers=ws["key"], json={
        "filters": {}, "include_unverified": True, "limit": 50})
    assert "lc-old" in {item["doc_key"] for item in legacy.json()["items"]}


async def test_watermarks_digest_and_orientation(phase3):
    """AG-EV-04: named watermarks, the digest and since_last_seen."""
    client = phase3
    ws = await _workspace(client)
    agent = await _agent(client, ws, "digest-a")
    other = await _agent(client, ws, "digest-b")
    head = (await client.get("/v1/events", headers=agent["headers"])).json()["next_cursor"]
    saved = await client.put("/v1/watermarks/morning", headers=agent["headers"],
                             json={"cursor": head})
    assert saved.status_code == 200, saved.text
    assert saved.json()["owner"].startswith("agent:prn_")
    bad = await client.put("/v1/watermarks/morning", headers=agent["headers"],
                           json={"cursor": "nope"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "cursor_invalid"
    assert (await client.get("/v1/watermarks/never", headers=agent["headers"])
            ).status_code == 404

    for index in range(3):
        await _put(client, other["headers"], f"dg-{index}", "first")
    await _put(client, other["headers"], "dg-0", "second", version=1)
    task = await client.post("/v1/tasks", headers=other["headers"], json={
        "title": "For you", "assignee": "agent:digest-a"})
    assert task.status_code == 201, task.text

    digest = await client.get("/v1/digest", headers=agent["headers"],
                              params={"since": "watermark:morning",
                                      "advance_watermark": "morning"})
    assert digest.status_code == 200, digest.text
    body = digest.json()
    assert body["counts"]["by_type"].get("doc.created") == 3
    assert body["counts"]["by_type"].get("doc.updated") == 1
    changed = {item["doc_key"]: item for item in body["changed_documents"]}
    assert set(changed) == {"dg-0", "dg-1", "dg-2"}
    assert changed["dg-0"]["changes"] == 2 and changed["dg-0"]["to_version"] == 2
    assert any(item["type"].startswith("task.") for item in body["for_me"])
    moved = await client.get("/v1/watermarks/morning", headers=agent["headers"])
    assert moved.json()["cursor"] == body["cursor"]
    quiet = await client.get("/v1/digest", headers=agent["headers"],
                             params={"since": "watermark:morning"})
    assert quiet.json()["total_events"] == 0
    too_many = await client.get("/v1/digest", headers=agent["headers"],
                                params={"max_items": 500})
    assert too_many.status_code == 422

    first = await client.get("/v1/orientation", headers=agent["headers"])
    assert first.status_code == 200, first.text
    assert "since_last_seen" in first.json()
    await _put(client, other["headers"], "dg-9", "after")
    second = (await client.get("/v1/orientation", headers=agent["headers"])).json()
    assert second["since_last_seen"]["counts"]["by_type"].get("doc.created") == 1
    amendment = second["constitution"]["amendment"]
    assert amendment["human_approval"] == "required"
    assert amendment["via"] == "propose_document_change"


async def test_webhooks_need_scope_and_carry_the_event(phase3):
    """AG-EV-05: scope gate, quota, a secret shown once."""
    client = phase3
    ws = await _workspace(client)
    agent = await _agent(client, ws, "hook-a")
    denied = await client.get("/v1/webhooks", headers=agent["headers"])
    assert denied.status_code == 403
    assert denied.json()["error"]["details"]["scope"] == "webhooks:manage"
    rule = {"name": "hook", "url": "http://127.0.0.1:9/hook", "event_types": ["DOC_CREATED"]}
    created = await client.post("/v1/webhooks", headers=ws["key"], json=rule)
    assert created.status_code == 201, created.text
    assert created.json()["secret"]
    listed = await client.get("/v1/webhooks", headers=ws["key"])
    assert all(item.get("secret") is None for item in listed.json()["items"])
    for index in range(9):
        made = await client.post("/v1/webhooks", headers=ws["key"],
                                 json={**rule, "name": f"hook-{index}"})
        assert made.status_code == 201, made.text
    full = await client.post("/v1/webhooks", headers=ws["key"], json=rule)
    assert full.json()["error"]["code"] == "quota_exceeded"
    removed = await client.delete(f"/v1/webhooks/{created.json()['id']}", headers=ws["key"])
    assert removed.status_code == 204


async def test_peer_reviews(phase3):
    """AG-CO-06 acceptance 1-3."""
    client = phase3
    ws = await _workspace(client)
    author = await _agent(client, ws, "author-a")
    b = await _agent(client, ws, "verifier-b", role="verifier")
    c = await _agent(client, ws, "verifier-c", role="verifier")
    await _put(client, author["headers"], "rv-doc", "draft one")
    await _put(client, author["headers"], "rv-doc", "draft two", version=1)

    self_review = await client.post("/v1/peer-reviews", headers=author["headers"], json={
        "output": {"doc_key": "rv-doc", "version": 2}, "reviewer": "agent:author-a"})
    assert self_review.status_code == 422
    assert self_review.json()["error"]["code"] == "reviewer_conflict"
    unversioned = await client.post("/v1/peer-reviews", headers=author["headers"], json={
        "output": {"doc_key": "rv-doc"}, "reviewer": "role:verifier"})
    assert unversioned.status_code == 422

    async def request(reviewer):
        response = await client.post("/v1/peer-reviews", headers=author["headers"], json={
            "output": {"doc_key": "rv-doc", "version": 2}, "reviewer": reviewer,
            "kind": "red_team", "rubric": "Find the holes"})
        assert response.status_code == 201, response.text
        return response.json()["task"]["task_key"]

    task_b = await request("agent:verifier-b")
    task_c = await request("agent:verifier-c")
    claimed = await client.post(f"/v1/tasks/{task_b}/claim", headers=b["headers"], json={})
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["context"][0]["version"] == 2
    submitted = await client.post(f"/v1/peer-reviews/{task_b}/submit", headers=b["headers"],
                                  json={"verdict": "fail", "summary": "Two holes",
                                        "findings": [
                                            {"severity": "major", "location": "s1",
                                             "note": "unbounded"},
                                            {"severity": "major", "location": "s2",
                                             "note": "unchecked"}]})
    assert submitted.status_code == 200, submitted.text
    review_key = submitted.json()["review"]["review_ref"]["doc_key"]
    assert review_key == "reviews/default.rv-doc.v2.verifier-b"

    reviewed = await client.get("/v1/documents/rv-doc", headers=ws["key"])
    assert reviewed.json()["reviews"][0]["verdict"] == "fail"
    review = await client.get(f"/v1/documents/{review_key}", headers=ws["key"],
                              params={"include_unverified": True})
    assert review.json()["frontmatter"]["kind"] == "peer_review"
    assert review.json()["frontmatter"]["rubric_hash"]

    sealed = await client.get(f"/v1/documents/{review_key}", headers=c["headers"],
                              params={"include_unverified": True})
    assert sealed.status_code == 403, sealed.text
    assert sealed.json()["error"]["code"] == "review_sealed"
    mine = await client.post(f"/v1/peer-reviews/{task_c}/submit", headers=c["headers"],
                             json={"verdict": "pass"})
    assert mine.status_code == 200, mine.text
    opened = await client.get(f"/v1/documents/{review_key}", headers=c["headers"],
                              params={"include_unverified": True})
    assert opened.status_code == 200, opened.text

    failing = await client.get("/v1/documents", headers=ws["key"],
                               params={"review_verdict": "fail"})
    assert [item["doc_key"] for item in failing.json()["items"]] == ["rv-doc"]
    events = await _sql("SELECT targets FROM workspace_events WHERE workspace_id = $1::uuid "
                        "AND type = 'review.submitted'", ws["id"])
    assert len(events) == 2

    # A token inside the author's subtree cannot review either.
    stamp = await _sql("SELECT stamp->>'token_id' AS t FROM document_versions v JOIN "
                       "shared_documents d ON d.id = v.document_id WHERE d.doc_key = 'rv-doc' "
                       "AND v.version = 2 AND d.workspace_id = $1::uuid", ws["id"])
    author_token = stamp[0]["t"]
    assert author_token
    await _agent(client, ws, "child-d")
    await _sql("UPDATE agent_tokens SET lineage = ARRAY[$1::varchar, canonical_id] "
               "WHERE agent_signature = 'child-d_sig' AND workspace_id = $2::uuid",
               author_token, ws["id"])
    conflict = await client.post("/v1/peer-reviews", headers=author["headers"], json={
        "output": {"doc_key": "rv-doc", "version": 2}, "reviewer": "agent:child-d"})
    assert conflict.status_code == 422
    assert conflict.json()["error"]["code"] == "reviewer_conflict"


async def test_scarce_resources_queue(phase3):
    """AG-CO-07 acceptance 1-3."""
    client = phase3
    ws = await _workspace(client)
    a = await _agent(client, ws, "res-a")
    b = await _agent(client, ws, "res-b")
    c = await _agent(client, ws, "res-c")
    spec = {"resource_key": "browser-1", "capacity": 1, "max_hold_seconds": 600,
            "description": "The shared browser"}
    agent_define = await client.post("/v1/resources", headers=a["headers"], json=spec)
    assert agent_define.status_code == 403
    defined = await client.post("/v1/resources", headers=ws["human"], json=spec)
    assert defined.status_code == 201, defined.text

    async def acquire(who, **extra):
        response = await client.post("/v1/resources/acquire", headers=who["headers"],
                                     json={"resource_key": "browser-1", "ttl_seconds": 120,
                                           **extra})
        assert response.status_code == 200, response.text
        return response.json()

    held = await acquire(a)
    assert held["granted"] and held["lease_id"].startswith("lease_")
    too_long = await client.post("/v1/resources/acquire", headers=b["headers"],
                                 json={"resource_key": "browser-1", "ttl_seconds": 900})
    assert too_long.status_code == 422
    no_queue = await acquire(b, queue=False)
    assert no_queue["granted"] is False and no_queue["queue_ticket"] is None
    queued = await acquire(b)
    assert queued == {**queued, "granted": False, "queue_position": 1}
    ticket_b = queued["queue_ticket"]
    assert ticket_b.startswith("rqt_")
    queued_c = await acquire(c)
    assert queued_c["queue_position"] == 2
    listed = await client.get("/v1/resources", headers=a["headers"])
    assert listed.json()["items"][0]["held"] == 1 and listed.json()["items"][0]["queued"] == 2

    # 1. A releases; B is offered the slot and confirms.
    released = await client.post(f"/v1/resources/release/{held['lease_id']}",
                                 headers=a["headers"])
    assert released.status_code == 200, released.text
    grants = await _sql("SELECT targets FROM workspace_events WHERE workspace_id = $1::uuid "
                        "AND type = 'resource.granted' ORDER BY seq", ws["id"])
    assert len(grants) == 1 and "agent:res-b_sig" in grants[0]["targets"]
    jump = await acquire(a)
    assert jump["granted"] is False
    confirmed = await acquire(b, queue_ticket=ticket_b)
    assert confirmed["granted"] is True
    heartbeat = await client.post(f"/v1/leases/{confirmed['lease_id']}/heartbeat",
                                  headers=b["headers"], json={"note": "browsing"})
    assert heartbeat.status_code == 200, heartbeat.text

    # 2. B stops heartbeating: the lease expires and C (next in line) is offered.
    await _sql("UPDATE leases SET expires_at = NOW() - INTERVAL '1 second' "
               "WHERE lease_id = $1", confirmed["lease_id"])
    for _ in range(30):
        offers = await _sql("SELECT state FROM resource_queue WHERE ticket = $1",
                            queued_c["queue_ticket"])
        if offers[0]["state"] == "offered":
            break
        await asyncio.sleep(0.5)
    assert offers[0]["state"] == "offered"
    lost = await client.post(f"/v1/leases/{confirmed['lease_id']}/heartbeat",
                             headers=b["headers"], json={})
    assert lost.status_code == 409 and lost.json()["error"]["code"] == "lease_lost"

    # 3. C does not confirm within 60 s: the offer moves on to A.
    await _sql("UPDATE resource_queue SET offered_at = NOW() - INTERVAL '61 seconds' "
               "WHERE ticket = $1", queued_c["queue_ticket"])
    ticket_a = jump["queue_ticket"]
    for _ in range(30):
        state = await _sql("SELECT state FROM resource_queue WHERE ticket = $1", ticket_a)
        if state[0]["state"] == "offered":
            break
        await asyncio.sleep(0.5)
    assert state[0]["state"] == "offered"
    late = await client.post("/v1/resources/acquire", headers=c["headers"], json={
        "resource_key": "browser-1", "ttl_seconds": 60,
        "queue_ticket": queued_c["queue_ticket"]})
    assert late.status_code == 409 and late.json()["error"]["code"] == "queue_ticket_expired"
    assert (await acquire(a, queue_ticket=ticket_a))["granted"] is True
