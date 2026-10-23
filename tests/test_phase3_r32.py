"""Phase 3 (R3.2) and the known gaps: AG-ST-12 retrieval evaluation, AG-ID-08
lineage attestations, scheduled exports, the partitioned event log."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import httpx
import pytest

from tests.conftest import run_server

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import verify_lineage  # noqa: E402

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}


def _dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(scope="module")
async def r32(api_base_url) -> AsyncIterator[httpx.AsyncClient]:
    del api_base_url
    async with run_server({"SCHEDULED_EXPORTS": "true"}, ".server-r32.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=120) as http:
            yield http


async def _sql(query: str, *args):
    conn = await asyncpg.connect(_dsn())
    try:
        return await conn.fetch(query, *args)
    finally:
        await conn.close()


async def _workspace(client) -> dict:
    created = await client.post("/v1/admin/workspaces", headers=ADMIN,
                                json={"name": f"R32 {uuid.uuid4().hex[:6]}", "tier": "pro"})
    body = created.json()
    alias = (await _sql("SELECT t.alias FROM tenants t JOIN workspaces w ON w.tenant_id = t.id "
                        "WHERE w.id = $1::uuid", body["workspace"]["id"]))[0]["alias"]
    return {"id": body["workspace"]["id"], "key": _bearer(body["api_key"]), "alias": alias,
            "tenant_id": body["workspace"]["tenant_id"]}


async def _agent(client, ws: dict, name: str) -> dict:
    minted = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
        json={"agent_signature": f"{name}_sig", "role_scope": "EXECUTOR"})
    assert minted.status_code in (200, 201), minted.text
    token_id = (await _sql("SELECT canonical_id FROM agent_tokens WHERE agent_signature = $1 "
                           "AND workspace_id = $2::uuid", f"{name}_sig", ws["id"]))[0][0]
    return {"headers": _bearer(minted.json()["api_key"]), "token_id": token_id}


async def test_retrieval_eval_history_and_gate(r32):
    """AG-ST-12 acceptance 1 and 2 (the gate)."""
    client = r32
    run = await client.post("/v1/admin/eval/retrieval", headers=ADMIN,
                            json={"dataset": "v1", "k": [10, 50]})
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["n_queries"] >= 100 and body["index_size"] >= 100
    assert {"recall@10", "recall@50", "mrr"} <= set(body)
    assert body["run_id"].startswith("evr_")
    history = await client.get("/v1/admin/eval/retrieval/runs", headers=ADMIN,
                               params={"dataset": "v1"})
    assert body["run_id"] in [item["run_id"] for item in history.json()["items"]]

    baseline = json.loads((ROOT / "eval" / "retrieval" / "baseline.json").read_text())
    gate = await client.post("/v1/admin/eval/retrieval/gate", headers=ADMIN,
                             json={"baseline": baseline, "result": body})
    assert gate.json()["passed"] is True, gate.json()
    degraded = {**body, "recall@50": baseline["recall@50"] - 0.05}
    failed = await client.post("/v1/admin/eval/retrieval/gate", headers=ADMIN,
                               json={"baseline": baseline, "result": degraded})
    assert failed.json()["passed"] is False

    unknown = await client.post("/v1/admin/eval/retrieval", headers=ADMIN,
                                json={"dataset": "nope"})
    assert unknown.status_code == 404


async def test_self_recall_probe(r32):
    """AG-ST-12 acceptance 3: every workspace gets a self_recall@50 point."""
    client = r32
    ws = await _workspace(client)
    for index in range(5):
        wrote = await client.put(f"/v1/documents/probe-{index}", headers=ws["key"], json={
            "content": f"Probe document number {index} about topic {uuid.uuid4().hex}",
            "title": f"Probe topic {index}", "expected_version": 0})
        assert wrote.status_code in (200, 201), wrote.text
    ran = await client.post("/v1/admin/eval/self-recall/run", headers=ADMIN,
                            params={"workspace_id": ws["id"]})
    assert ran.status_code == 200, ran.text
    assert ran.json()["sampled"] >= 1 and 0 <= ran.json()["self_recall@50"] <= 1
    listed = await client.get("/v1/admin/eval/self-recall", headers=ADMIN,
                              params={"workspace_id": ws["id"]})
    assert listed.json()["items"][0]["workspace_id"] == ws["id"]


async def test_lineage_attestation(r32):
    """AG-ID-08 acceptance 1-3."""
    client = r32
    ws = await _workspace(client)
    agent = await _agent(client, ws, f"att-{uuid.uuid4().hex[:6]}")
    wrote = await client.put("/v1/documents/attested", headers=agent["headers"],
                             json={"content": "signed work", "expected_version": 0})
    assert wrote.status_code in (200, 201), wrote.text
    doc_id = wrote.json()["document"]["canonical_id"]
    jwks = (await client.get(f"/.well-known/agentgraph/tenants/{ws['alias']}/jwks.json")).json()
    assert jwks["keys"][0]["crv"] == "Ed25519" and "d" not in jwks["keys"][0]

    signed = await client.post("/v1/lineage/attestations", headers=agent["headers"], json={
        "subject": {"doc_id": doc_id, "version": 1}, "audience": "tenant-b"})
    assert signed.status_code == 200, signed.text
    token = signed.json()["attestation"]
    payload = verify_lineage.verify(token, jwks, "tenant-b")
    assert payload["subject"]["version"] == 1 and payload["subject"]["doc_hash"]
    assert len(payload["chain"]) == 2
    raw = json.dumps(payload)
    assert agent["token_id"] not in raw and "att-" not in raw
    with pytest.raises(verify_lineage.AttestationError):
        verify_lineage.verify(token, jwks, "tenant-c")
    with pytest.raises(verify_lineage.AttestationError):
        verify_lineage.verify(token, jwks, "tenant-b", now=time.time() + 16 * 60)
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(verify_lineage.AttestationError):
        verify_lineage.verify(tampered, jwks, "tenant-b")

    for_b = (await client.post("/v1/lineage/attestations", headers=agent["headers"], json={
        "subject": {"token_id": agent["token_id"]}, "audience": "tenant-b"})).json()
    for_c = (await client.post("/v1/lineage/attestations", headers=agent["headers"], json={
        "subject": {"token_id": agent["token_id"]}, "audience": "tenant-c"})).json()
    ref_b = verify_lineage.verify(for_b["attestation"], jwks, "tenant-b")["chain"][-1]["ref"]
    ref_c = verify_lineage.verify(for_c["attestation"], jwks, "tenant-c")["chain"][-1]["ref"]
    assert ref_b != ref_c
    too_long = await client.post("/v1/lineage/attestations", headers=agent["headers"], json={
        "subject": {"token_id": agent["token_id"]}, "audience": "b", "ttl_seconds": 901})
    assert too_long.status_code == 422

    revoked_url = f"/.well-known/agentgraph/tenants/{ws['alias']}/revoked"
    live = await client.get(revoked_url, params={"ref": ref_b, "aud": "tenant-b"})
    assert live.json() == {**live.json(), "known": True, "revoked": False}
    await _sql("UPDATE agent_tokens SET revoked_at = NOW() WHERE canonical_id = $1",
               agent["token_id"])
    gone = await client.get(revoked_url, params={"ref": ref_b, "aud": "tenant-b"})
    assert gone.json()["revoked"] is True

    rotated = await client.post(f"/v1/admin/tenants/{ws['tenant_id']}/signing-keys/rotate",
                                headers=ADMIN)
    assert rotated.status_code == 200, rotated.text
    keys = (await client.get(f"/.well-known/agentgraph/tenants/{ws['alias']}/jwks.json")).json()
    assert len(keys["keys"]) == 2
    # An attestation signed before the rotation still verifies during the overlap.
    verify_lineage.verify(token, keys, "tenant-b")


async def test_scheduled_exports(r32):
    """AG-ST-02 point 6: a weekly full baseline and daily increments."""
    client = r32
    ws = await _workspace(client)
    await client.put("/v1/documents/sched-a", headers=ws["key"],
                     json={"content": "one", "expected_version": 0})
    ran = await client.post("/v1/admin/exports/scheduled/run", headers=ADMIN)
    assert ran.status_code == 200, ran.text

    async def scheduled():
        return await _sql("SELECT id, kind, status, base_export_id FROM exports WHERE "
                          "tenant_id = $1 AND purpose = 'scheduled' ORDER BY created_at",
                          ws["tenant_id"])

    for _ in range(60):
        rows = await scheduled()
        if rows and rows[0]["status"] == "done":
            break
        time.sleep(0.5)
    assert [row["kind"] for row in rows] == ["full"] and rows[0]["status"] == "done"
    await client.post("/v1/admin/exports/scheduled/run", headers=ADMIN)
    assert len(await scheduled()) == 1  # nothing due yet
    await _sql("UPDATE exports SET created_at = NOW() - INTERVAL '25 hours' WHERE id = $1",
               rows[0]["id"])
    await client.post("/v1/admin/exports/scheduled/run", headers=ADMIN)
    rows = await scheduled()
    assert [row["kind"] for row in rows] == ["full", "incremental"]
    assert rows[1]["base_export_id"] == rows[0]["id"]


async def test_event_log_is_partitioned(r32):
    kind = await _sql("SELECT relkind FROM pg_class WHERE oid = 'workspace_events'::regclass")
    assert kind[0]["relkind"] == b"p" or kind[0]["relkind"] == "p"
    parts = await _sql("SELECT count(*) AS n FROM pg_inherits "
                       "WHERE inhparent = 'workspace_events'::regclass")
    assert parts[0]["n"] == 16
    client = r32
    ws = await _workspace(client)
    head = (await client.get("/v1/events", headers=ws["key"])).json()["next_cursor"]
    for namespace in ("default", "ops"):
        await client.post("/v1/namespaces", headers=ws["key"], json={
            "namespace": namespace, "purpose": "test"})
        await client.put(f"/v1/documents/part-{namespace}", headers=ws["key"], json={
            "content": "x", "expected_version": 0, "namespace": namespace})
    events = (await client.get("/v1/events", headers=ws["key"],
                               params={"types": "doc.created", "cursor": head})
              ).json()["events"]
    seqs = [event["seq"] for event in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs) >= 2
