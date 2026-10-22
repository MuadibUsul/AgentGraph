"""Original PRD leftovers: state-slot locks on the unified lease table (AG-CO-07
point 4) and merged reading of hand-split documents (AG-TC-04 point 4)."""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}


async def _agent(client, workspace_id: str) -> dict:
    signature = f"slotter-{uuid.uuid4().hex[:6]}"
    minted = await client.post(f"/v1/admin/workspaces/{workspace_id}/agent-tokens",
                               headers=ADMIN, json={"agent_signature": signature,
                                                    "role_scope": "EXECUTOR",
                                                    "allowed_namespaces": ["*"]})
    assert minted.status_code in (200, 201), minted.text
    return {"Authorization": f"Bearer {minted.json()['api_key']}"}


async def _slot_leases(client, headers) -> list[dict]:
    listed = await client.get("/v1/leases", headers=headers)
    assert listed.status_code == 200, listed.text
    return [item for item in listed.json()["items"] if item["resource_kind"] == "slot"]


async def test_slot_locks_are_leases(client):
    created = (await client.post("/v1/admin/workspaces", headers=ADMIN, json={
        "name": f"slots-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
    wid = created["workspace"]["id"]
    a, b = await _agent(client, wid), await _agent(client, wid)
    key = f"grid/{uuid.uuid4().hex[:6]}"

    written = await client.put(f"/v1/slots/{key}", headers=a, json={
        "state": {"step": 1}, "expected_version": 0, "lease_seconds": 300})
    assert written.status_code in (200, 201), written.text
    leases = await _slot_leases(client, a)
    assert len(leases) == 1 and leases[0]["state"] == "active"
    lease = leases[0]
    assert lease["resource_ref"] == key and lease["ttl_seconds"] == 300

    again = await client.put(f"/v1/slots/{key}", headers=a, json={
        "state": {"step": 2}, "expected_version": 1, "lease_seconds": 300})
    assert again.status_code == 200, again.text
    renewed = await _slot_leases(client, a)
    assert [item["lease_id"] for item in renewed] == [lease["lease_id"]]  # same lease, renewed

    blocked = await client.put(f"/v1/slots/{key}", headers=b, json={
        "state": {"step": 9}, "expected_version": 2})
    assert blocked.status_code == 409

    beat = await client.post(f"/v1/leases/{lease['lease_id']}/heartbeat", headers=a,
                             json={"note": "still working"})
    assert beat.status_code == 200, beat.text
    released = await client.post(f"/v1/leases/{lease['lease_id']}/release", headers=a,
                                 json={"reason": "done"})
    assert released.status_code == 200, released.text
    assert released.json()["state"] == "released"

    free = await client.put(f"/v1/slots/{key}", headers=b, json={
        "state": {"step": 3}, "expected_version": 2})
    assert free.status_code == 200, free.text
    lost = await client.post(f"/v1/leases/{lease['lease_id']}/heartbeat", headers=a, json={})
    assert lost.status_code == 409
    assert lost.json()["error"]["code"] == "lease_lost"


async def test_hand_split_documents_read_as_one(client):
    created = (await client.post("/v1/admin/workspaces", headers=ADMIN, json={
        "name": f"parts-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
    wid = created["workspace"]["id"]
    key = {"Authorization": f"Bearer {created['api_key']}"}
    for index, name in ((2, "b"), (1, "a"), (10, "c")):
        written = await client.put(f"/v1/documents/report/{name}", headers=key, json={
            "content": f"part {index}", "expected_version": 0,
            "frontmatter": {"part_of": "report", "part_index": index}})
        assert written.status_code in (200, 201), written.text
    await client.put("/v1/documents/other", headers=key, json={
        "content": "unrelated", "expected_version": 0, "frontmatter": {"part_of": "else"}})
    merged = await client.get(f"/v1/admin/workspaces/{wid}/document-parts", headers=ADMIN,
                              params={"part_of": "report"})
    assert merged.status_code == 200, merged.text
    body = merged.json()
    assert body["count"] == 3 and body["truncated"] is False
    assert [part["content"] for part in body["parts"]] == ["part 1", "part 2", "part 10"]
