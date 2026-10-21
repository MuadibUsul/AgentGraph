from __future__ import annotations

import asyncio
import uuid

import httpx
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


async def test_audit_cursor_is_complete(client, admin_headers, workspace):
    wid = workspace["workspace"]["id"]
    for index in range(3):
        created = await client.put(
            f"/v1/documents/audit-{uuid.uuid4().hex[:6]}-{index}",
            headers=workspace["headers"],
            json={"content": f"# audit {index}", "frontmatter": {}, "expected_version": 0},
        )
        assert created.status_code == 200, created.text
    seen: list[str] = []
    cursor = None
    total = None
    while True:
        params = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        page = await client.get(
            f"/v1/admin/workspaces/{wid}/audit",
            headers=admin_headers,
            params=params,
        )
        assert page.status_code == 200, page.text
        payload = page.json()
        total = payload["total_batches"]
        seen.extend(batch["session_batch_id"] for batch in payload["batches"])
        cursor = payload.get("next_cursor")
        if cursor is None:
            break
    assert total is not None and total >= 3
    assert len(seen) == len(set(seen))
    assert len(seen) == total


# ------------------------------------------------- 2026-10-21 audit follow-up

BRIEF = {
    "summary": "ready",
    "key_metrics": {},
    "unverified_hypotheses": [],
    "risk_flag": False,
}


async def _agent_with_id(client, admin_headers, workspace, role, namespaces):
    response = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        headers=admin_headers,
        json={
            "agent_signature": f"{role.lower()}_{uuid.uuid4().hex[:8]}",
            "role_scope": role,
            "allowed_namespaces": namespaces,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return {"Authorization": f"Bearer {body['api_key']}"}, body["token"]["id"]


async def _put_doc(client, headers, doc_key, namespace, content, expected_version=0):
    response = await client.put(
        f"/v1/documents/{doc_key}",
        headers=headers,
        json={
            "content": content,
            "namespace": namespace,
            "expected_version": expected_version,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["document"]


async def _claimed_task(client, workspace, agent, role, namespace="default"):
    key = f"SEC-{uuid.uuid4().hex[:6].upper()}"
    created = await client.post(
        "/v1/tasks",
        headers=workspace["headers"],
        json={
            "task_key": key,
            "title": "security follow-up",
            "assigned_role": role,
            "namespace": namespace,
        },
    )
    assert created.status_code == 201, created.text
    claimed = await client.post(f"/v1/tasks/{key}/claim", headers=agent, json={})
    assert claimed.status_code == 200, claimed.text
    return key


async def test_handoff_artifact_must_stay_in_task_namespace(
    client, admin_headers, workspace
):
    other = await make_agent(client, admin_headers, workspace, "EXECUTOR", ["teamB"])
    foreign = await _put_doc(
        client, other, f"teamb/{uuid.uuid4().hex[:6]}", "teamB", "other team draft"
    )
    agent = await make_agent(client, admin_headers, workspace, "EXECUTOR", ["teamA"])
    key = await _claimed_task(client, workspace, agent, "EXECUTOR", "teamA")
    handed = await client.post(
        f"/v1/tasks/{key}/handoff",
        headers=agent,
        json={
            "next_status": "REVIEWING",
            "brief": BRIEF,
            "artifact_uri": f"doc://{foreign['doc_key']}",
        },
    )
    assert handed.status_code == 403, handed.text
    current = await client.get(
        f"/v1/documents/{foreign['doc_key']}", headers=workspace["headers"]
    )
    assert current.json()["verification_status"] == "DRAFT"


async def test_review_verifies_only_the_handed_off_artifact_version(
    client, admin_headers, workspace
):
    agent = await make_agent(client, admin_headers, workspace, "EXECUTOR", ["*"])
    document = await _put_doc(
        client, agent, f"art/{uuid.uuid4().hex[:6]}", "default", "benign v1"
    )
    key = await _claimed_task(client, workspace, agent, "EXECUTOR")
    handed = await client.post(
        f"/v1/tasks/{key}/handoff",
        headers=agent,
        json={
            "next_status": "REVIEWING",
            "brief": BRIEF,
            "artifact_uri": f"doc://{document['doc_key']}",
        },
    )
    assert handed.status_code == 200, handed.text
    await _put_doc(
        client, agent, document["doc_key"], "default", "changed after handoff", 1
    )
    owner = await make_human(client, admin_headers, workspace, "OWNER")
    reviewed = await client.post(
        f"/v1/human/tasks/{key}/review",
        headers=owner,
        json={"decision": "approve", "expected_version": handed.json()["task"]["version"]},
    )
    assert reviewed.status_code == 200, reviewed.text
    current = await client.get(
        f"/v1/documents/{document['doc_key']}", headers=workspace["headers"]
    )
    assert current.json()["verification_status"] == "DRAFT"
    assert current.json()["verified_version"] is None


async def test_agent_cannot_review_its_own_handoff(client, admin_headers, workspace):
    auditor = await make_agent(client, admin_headers, workspace, "AUDITOR", ["*"])
    other_auditor = await make_agent(client, admin_headers, workspace, "AUDITOR", ["*"])
    key = await _claimed_task(client, workspace, auditor, "AUDITOR")
    # AUDITOR lacks doc:create, so it cannot mint artifacts through handoff.
    minted = await client.post(
        f"/v1/tasks/{key}/handoff",
        headers=auditor,
        json={"next_status": "REVIEWING", "brief": BRIEF, "artifact_text": "self-made"},
    )
    assert minted.status_code == 403, minted.text
    handed = await client.post(
        f"/v1/tasks/{key}/handoff",
        headers=auditor,
        json={"next_status": "REVIEWING", "brief": BRIEF},
    )
    assert handed.status_code == 200, handed.text
    version = handed.json()["task"]["version"]
    own = await client.post(
        f"/v1/tasks/{key}/review",
        headers=auditor,
        json={"decision": "approve", "expected_version": version},
    )
    assert own.status_code == 403, own.text
    peer = await client.post(
        f"/v1/tasks/{key}/review",
        headers=other_auditor,
        json={"decision": "approve", "expected_version": version},
    )
    assert peer.status_code == 200, peer.text


async def test_agent_status_changes_respect_human_lock(client, admin_headers, workspace):
    owner = await make_human(client, admin_headers, workspace, "OWNER")
    doc_key = f"locked/{uuid.uuid4().hex[:6]}"
    document = await _put_doc(client, owner, doc_key, "default", "human truth")
    locked = await client.put(
        f"/v1/human/documents/{doc_key}",
        headers=owner,
        json={
            "expected_version": document["version"],
            "content": "human truth",
            "lock": True,
            "human_note": "do not touch",
        },
    )
    assert locked.status_code == 200, locked.text
    version = locked.json()["document"]["version"]
    executor = await make_agent(client, admin_headers, workspace, "EXECUTOR", ["*"])
    auditor = await make_agent(client, admin_headers, workspace, "AUDITOR", ["*"])
    for headers, action in (
        (executor, "submit-review"),
        (auditor, "deprecate"),
        (auditor, "verify"),
    ):
        response = await client.post(
            f"/v1/documents/{doc_key}/{action}",
            headers=headers,
            json={"expected_version": version},
        )
        assert response.status_code == 403, (action, response.text)
        assert response.json()["error"]["code"] == "HUMAN_SOVEREIGNTY_LOCKED"


async def test_stream_closes_after_token_revocation(
    client, admin_headers, workspace, api_base_url
):
    agent, token_id = await _agent_with_id(
        client, admin_headers, workspace, "EXECUTOR", ["*"]
    )
    frames: list[str] = []
    connected = asyncio.Event()

    async def read_stream() -> None:
        async with httpx.AsyncClient(base_url=api_base_url, timeout=None) as http:
            async with http.stream("GET", "/v1/stream", headers=agent) as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if line.startswith("event: connected"):
                        connected.set()
                    if line.startswith(("event: closed", "data:")):
                        frames.append(line)

    reader = asyncio.create_task(read_stream())
    await asyncio.wait_for(connected.wait(), 10)
    revoked = await client.patch(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens/{token_id}",
        headers=admin_headers,
        json={"is_active": False},
    )
    assert revoked.status_code == 200, revoked.text
    await asyncio.wait_for(reader, 20)
    assert any("credential_revoked" in frame for frame in frames), frames


async def test_stats_hide_other_namespaces(client, admin_headers, workspace):
    other = await make_agent(client, admin_headers, workspace, "EXECUTOR", ["hidden-ns"])
    await _put_doc(client, other, f"h/{uuid.uuid4().hex[:6]}", "hidden-ns", "x")
    scout = await make_agent(client, admin_headers, workspace, "SCOUT", ["raw_inbox"])
    stats = await client.get("/v1/stats", headers=scout)
    assert stats.status_code == 200, stats.text
    assert "hidden-ns" not in stats.json()["document_namespaces"]
    full = await client.get("/v1/stats", headers=workspace["headers"])
    assert "hidden-ns" in full.json()["document_namespaces"]
