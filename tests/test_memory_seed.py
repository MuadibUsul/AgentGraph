"""Memory seed and directory registration (2026-10-23 spec, acceptance A-01 to
A-06; A-07, the did-you-mean on a wrongly cased label, is in test_poke_delivery)."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from tests.conftest import run_server

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}
PASSWORD = "correct-horse-battery"
MARKER = "SECRET-RULE-TEXT-7f3a"


async def _workspace(client) -> dict:
    created = (await client.post("/v1/admin/workspaces", headers=ADMIN, json={
        "name": f"seed-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
    wid = created["workspace"]["id"]
    email = f"owner-{uuid.uuid4().hex[:8]}@example.test"
    await client.post(f"/v1/admin/workspaces/{wid}/human-users", headers=ADMIN, json={
        "email": email, "display_name": "Owner", "role": "OWNER", "password": PASSWORD})
    login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    return {"id": wid, "key": {"Authorization": f"Bearer {created['api_key']}"},
            "human": {"Authorization": f"Bearer {login.json()['token']}"}}


async def _agent(client, ws: dict, label: str | None = None) -> dict:
    signature = label or f"Seed{uuid.uuid4().hex[:6]}"
    minted = await client.post(f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
                               json={"agent_signature": signature, "role_scope": "EXECUTOR",
                                     "allowed_namespaces": ["*"]})
    assert minted.status_code in (200, 201), minted.text
    return {"headers": {"Authorization": f"Bearer {minted.json()['api_key']}"},
            "signature": signature}


async def _constitution(client, ws: dict, text: str) -> None:
    current = await client.get(f"/v1/admin/workspaces/{ws['id']}/documents/system/constitution",
                               headers=ADMIN)
    updated = await client.put(
        f"/v1/admin/workspaces/{ws['id']}/documents/system/constitution", headers=ADMIN,
        json={"content": text, "expected_version": current.json()["version"]})
    assert updated.status_code == 200, updated.text


async def test_seed_is_small_versioned_and_stops_once_stored(client):
    """A-01 to A-04."""
    ws = await _workspace(client)
    await _constitution(client, ws, f"# Rules\n\n{MARKER}\n")
    agent = await _agent(client, ws)
    pack = (await client.get("/v1/orientation", headers=agent["headers"])).json()
    seed = pack["memory_seed"]
    assert len(json.dumps(seed, ensure_ascii=False)) <= 1_500
    assert len(seed["rules_digest"]) <= 600
    assert seed["identity"]["label"] == f"agent:{agent['signature']}"
    uris = [pointer["uri"] for pointer in seed["pointers"]]
    assert uris[:2] == ["doc://system/constitution", "doc://system/namespaces/default"]
    assert MARKER not in json.dumps(seed)  # pointers, never document text (A-04)

    # Until stored, the compact seed rides along on key responses.
    listed = await client.get("/v1/documents", headers=agent["headers"])
    assert listed.json()["onboarding"]["memory_seed"]["seed_version"] == seed["seed_version"]
    acked = await client.post("/v1/orientation/memory-seed/ack", headers=agent["headers"],
                              json={"version": seed["seed_version"]})
    assert acked.status_code == 200, acked.text
    assert acked.json()["current"] is True
    again = await client.get("/v1/documents", headers=agent["headers"])
    assert "memory_seed" not in (again.json().get("onboarding") or {})
    overview = await client.get("/v1/human/agent-onboarding", headers=ws["human"])
    mine = next(item for item in overview.json()["items"]
                if item["agent_signature"] == agent["signature"])
    assert mine["memory_seed_version"] == seed["seed_version"]

    # A-02: a new constitution version moves the seed; the stored one is stale.
    await _constitution(client, ws, "# Rules\n\nversion two\n")
    moved = (await client.get("/v1/orientation", headers=agent["headers"])).json()["memory_seed"]
    assert moved["seed_version"] > seed["seed_version"]
    assert moved["stale"] is True and moved["stored_version"] == seed["seed_version"]


async def test_registration_is_step_four_and_tokens_are_backfilled(client):
    """A-05, A-06 (R-01 to R-04)."""
    ws = await _workspace(client)
    old = await _agent(client, ws)  # never registers itself
    async with run_server({}, ".server-seed.log") as base_url:  # startup backfills
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as fresh:
            listed = (await fresh.get("/v1/agents", headers=ws["key"])).json()["items"]
    auto = next(item for item in listed if item["address"] == f"agent:{old['signature']}")
    assert auto["source"] == "auto" and auto["agent_id"] == old["signature"].lower()
    me = (await client.get("/v1/orientation", headers=old["headers"])).json()["me"]
    assert me["registered"] is False and me["directory_source"] == "auto"
    assigned = await client.post("/v1/tasks", headers=ws["key"], json={
        "title": "for the old agent", "assignee": f"agent:{old['signature']}"})
    assert assigned.status_code == 201, assigned.text

    completed = await client.post("/v1/agents/register", headers=old["headers"], json={
        "capabilities": ["writes reports"], "reach": {"webhook": False, "note": "poll"}})
    assert completed.status_code == 201, completed.text
    entry = completed.json()
    assert entry["source"] == "registered" and entry["principal_id"] == auto["principal_id"]
    assert entry["display_name"] == old["signature"]  # the label, case kept
    assert entry["reach"]["note"] == "poll"
    assert "default_subscription_id" in entry["reach"]

    fresh_agent = await _agent(client, ws)
    await client.get("/v1/orientation", headers=fresh_agent["headers"])
    registered = await client.post("/v1/agents/register", headers=fresh_agent["headers"],
                                   json={"capabilities": ["claims tasks"]})
    assert registered.status_code == 201, registered.text
    others = (await client.get("/v1/agents", headers=old["headers"])).json()["items"]
    found = next(item for item in others
                 if item["address"] == f"agent:{fresh_agent['signature']}")
    assert found["capabilities"] == ["claims tasks"] and found["role"] == "EXECUTOR"
