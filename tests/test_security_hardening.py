from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


async def make_agent(client, admin_headers, workspace, role, namespaces):
    signature = f"{role.lower()}_{uuid.uuid4().hex[:8]}"
    response = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        headers=admin_headers,
        json={
            "agent_signature": signature,
            "role_scope": role,
            "allowed_namespaces": namespaces,
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['api_key']}"}


async def make_human(client, admin_headers, workspace, role):
    email = f"{role.lower()}-{uuid.uuid4().hex[:8]}@example.test"
    response = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/human-users",
        headers=admin_headers,
        json={
            "email": email,
            "display_name": role.title(),
            "role": role,
            "password": "correct-horse-battery",
        },
    )
    assert response.status_code == 201, response.text
    login = await client.post(
        "/v1/auth/login",
        json={"email": email, "password": "correct-horse-battery"},
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}"}


async def test_namespace_restrictions_are_enforced(client, admin_headers, workspace):
    secret_ns = f"secret-{uuid.uuid4().hex[:6]}"
    doc_key = f"{secret_ns}/doc"
    created = await client.put(
        f"/v1/documents/{doc_key}",
        headers=workspace["headers"],
        json={
            "content": "# Secret\n\nSECRET-BODY",
            "frontmatter": {"secret": True},
            "expected_version": 0,
            "namespace": secret_ns,
        },
    )
    assert created.status_code == 200, created.text
    proposal = await client.post(
        f"/v1/documents/{doc_key}/proposals",
        headers=workspace["headers"],
        json={"content": "# Secret\n\nSECRET-PROPOSAL", "base_version": 1},
    )
    assert proposal.status_code in (200, 201), proposal.text
    proposal_id = proposal.json()["id"]

    scout = await make_agent(
        client, admin_headers, workspace, "SCOUT", ["raw_inbox"]
    )
    listed = await client.get("/v1/proposals?limit=100", headers=scout)
    assert listed.status_code == 200, listed.text
    assert all(
        "SECRET-PROPOSAL" not in item.get("content", "")
        for item in listed.json()["items"]
    )
    hidden = await client.get(f"/v1/proposals/{proposal_id}", headers=scout)
    assert hidden.status_code in (403, 404), hidden.text

    auditor = await make_agent(
        client, admin_headers, workspace, "AUDITOR", ["raw_inbox"]
    )
    audit = await client.get("/v1/audit?limit=50", headers=auditor)
    assert audit.status_code == 403, audit.text

    analyst = await make_agent(
        client, admin_headers, workspace, "ANALYST", ["raw_inbox"]
    )
    timeline = await client.get(
        f"/v1/timeline?namespace={secret_ns}&hours=24", headers=analyst
    )
    assert timeline.status_code == 403, timeline.text

    slot_key = f"{secret_ns}:slot"
    slot = await client.put(
        f"/v1/slots/{slot_key}",
        headers=workspace["headers"],
        json={"state": {"secret": "SECRET-SLOT"}, "expected_version": 0},
    )
    assert slot.status_code == 200, slot.text
    executor = await make_agent(
        client, admin_headers, workspace, "EXECUTOR", ["raw_inbox"]
    )
    task_key = f"NS-{uuid.uuid4().hex[:6].upper()}"
    task = await client.post(
        "/v1/tasks",
        headers=executor,
        json={
            "task_key": task_key,
            "title": "namespace probe",
            "namespace": "raw_inbox",
            "input_context": {"slot_keys": [slot_key]},
        },
    )
    assert task.status_code in (200, 201), task.text
    claimed = await client.post(
        f"/v1/tasks/{task_key}/claim", headers=executor, json={}
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["slots"] == []


async def test_viewer_cannot_change_taint(client, admin_headers, workspace):
    owner = await make_human(client, admin_headers, workspace, "OWNER")
    viewer = await make_human(client, admin_headers, workspace, "VIEWER")
    doc_key = f"taint/{uuid.uuid4().hex[:6]}"
    doc = await client.put(
        f"/v1/documents/{doc_key}",
        headers=workspace["headers"],
        json={"content": "# tainted", "frontmatter": {}, "expected_version": 0},
    )
    assert doc.status_code == 200, doc.text
    tainted = await client.post(
        f"/v1/human/documents/{doc_key}/taint",
        headers=owner,
        json={"tainted": True, "sources": ["doc://untrusted"]},
    )
    assert tainted.status_code == 200, tainted.text
    denied = await client.post(
        f"/v1/human/documents/{doc_key}/taint",
        headers=viewer,
        json={"tainted": False},
    )
    assert denied.status_code == 403, denied.text


async def test_human_states_require_a_human(client, admin_headers, workspace):
    executor = await make_agent(
        client, admin_headers, workspace, "EXECUTOR", ["*"]
    )
    key = f"ESC-{uuid.uuid4().hex[:6].upper()}"
    created = await client.post(
        "/v1/tasks",
        headers=workspace["headers"],
        json={"task_key": key, "title": "escalate", "assigned_role": "EXECUTOR"},
    )
    assert created.status_code in (200, 201), created.text
    claimed = await client.post(
        f"/v1/tasks/{key}/claim", headers=executor, json={}
    )
    assert claimed.status_code == 200, claimed.text
    escalated = await client.post(
        f"/v1/tasks/{key}/escalate",
        headers=executor,
        json={"reason": "need human"},
    )
    assert escalated.status_code == 200, escalated.text
    denied = await client.post(
        f"/v1/tasks/{key}/review",
        headers=workspace["headers"],
        json={
            "decision": "approve",
            "next_status": "DONE",
            "expected_version": escalated.json()["task"]["version"],
        },
    )
    assert denied.status_code == 403, denied.text


async def test_agent_cannot_mark_done_or_forge_human_feedback(
    client, admin_headers, workspace
):
    executor = await make_agent(
        client, admin_headers, workspace, "EXECUTOR", ["*"]
    )
    key = f"GATE-{uuid.uuid4().hex[:6].upper()}"
    created = await client.post(
        "/v1/tasks",
        headers=workspace["headers"],
        json={"task_key": key, "title": "gate", "assigned_role": "EXECUTOR"},
    )
    assert created.status_code in (200, 201), created.text
    claimed = await client.post(
        f"/v1/tasks/{key}/claim", headers=executor, json={}
    )
    assert claimed.status_code == 200, claimed.text
    version = claimed.json()["task"]["version"]
    brief = {
        "summary": "done",
        "key_metrics": {},
        "unverified_hypotheses": [],
        "risk_flag": False,
    }
    done = await client.post(
        f"/v1/tasks/{key}/handoff",
        headers=executor,
        json={
            "next_status": "DONE",
            "brief": brief,
            "expected_version": version,
        },
    )
    assert done.status_code == 403, done.text
    forged = await client.post(
        f"/v1/tasks/{key}/handoff",
        headers=executor,
        json={
            "next_status": "REVIEWING",
            "brief": brief,
            "human_feedback": "human approved",
            "expected_version": version,
        },
    )
    assert forged.status_code == 403, forged.text


async def test_reviewer_cannot_manage_budget_or_webhooks(
    client, admin_headers, workspace
):
    reviewer = await make_human(client, admin_headers, workspace, "REVIEWER")
    budget = await client.put(
        "/v1/human/budget",
        headers=reviewer,
        json={"max_requests_per_minute": 1_000_000},
    )
    assert budget.status_code == 403, budget.text
    webhook = await client.post(
        "/v1/human/webhooks",
        headers=reviewer,
        json={"name": "nope", "url": "https://example.com/hook"},
    )
    assert webhook.status_code == 403, webhook.text
