"""Task context pointers (2026-10-23 confirmed defects B-01 and B-02): split
{namespace, doc_key} pointers resolve to the canonical key, and an assignee
reads its task's context documents without include_unverified."""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}


async def _agent(client, workspace_id: str) -> dict:
    signature = f"ctx-{uuid.uuid4().hex[:6]}"
    minted = await client.post(f"/v1/admin/workspaces/{workspace_id}/agent-tokens",
                               headers=ADMIN, json={"agent_signature": signature,
                                                    "role_scope": "EXECUTOR",
                                                    "allowed_namespaces": ["*"]})
    assert minted.status_code in (200, 201), minted.text
    return {"headers": {"Authorization": f"Bearer {minted.json()['api_key']}"},
            "signature": signature}


async def test_split_pointers_resolve_and_assignees_can_read(client):
    created = (await client.post("/v1/admin/workspaces", headers=ADMIN, json={
        "name": f"ctx-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
    wid = created["workspace"]["id"]
    author, assignee, stranger = [await _agent(client, wid) for _ in range(3)]
    written = await client.put("/v1/documents/specs/plan", headers=author["headers"], json={
        "namespace": "specs", "content": "the plan", "expected_version": 0})
    assert written.status_code in (200, 201), written.text

    key = f"CTX-{uuid.uuid4().hex[:6].upper()}"
    task = await client.post("/v1/tasks", headers=author["headers"], json={
        "task_key": key, "title": "follow the plan", "assigned_role": "EXECUTOR",
        "assignee": f"agent:{assignee['signature']}",
        "context_docs": [{"namespace": "specs", "doc_key": "plan"}]})
    assert task.status_code == 201, task.text
    detail = await client.get(f"/v1/tasks/{key}", headers=author["headers"])
    assert detail.json()["context_docs"][0]["doc_key"] == "specs/plan"

    read = await client.get("/v1/documents/specs/plan", headers=assignee["headers"])
    assert read.status_code == 200, read.text
    listed = await client.get("/v1/documents", headers=assignee["headers"],
                              params={"namespace": "specs"})
    assert "specs/plan" in [item["doc_key"] for item in listed.json()["items"]]
    hidden = await client.get("/v1/documents/specs/plan", headers=stranger["headers"])
    assert hidden.status_code == 404

    claimed = await client.post(f"/v1/tasks/{key}/claim", headers=assignee["headers"],
                                json={})
    assert claimed.status_code == 200, claimed.text
    context = claimed.json()["context"]
    assert context[0]["content"] == "the plan" and not context[0].get("missing")
