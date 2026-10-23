"""Phase 0 R0.1 acceptance: AG-CO-01, AG-ST-03, AG-ST-04, AG-ST-05."""

from __future__ import annotations

import hashlib
import json
import uuid

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

pytestmark = pytest.mark.integration

BRIEF = {"summary": "ready", "key_metrics": {}, "unverified_hypotheses": [], "risk_flag": False}


async def _agent(client, admin_headers, workspace, role):
    response = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        headers=admin_headers,
        json={"agent_signature": f"{role.lower()}_{uuid.uuid4().hex[:6]}", "role_scope": role},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['api_key']}"}, response.json()["token"]


async def _in_review(client, workspace, executor):
    key = f"RW-{uuid.uuid4().hex[:6].upper()}"
    created = await client.post(
        "/v1/tasks",
        headers=workspace["headers"],
        json={"task_key": key, "title": "rework", "assigned_role": "EXECUTOR"},
    )
    assert created.status_code == 201, created.text
    claimed = await client.post(
        f"/v1/tasks/{key}/claim", headers=executor, json={"lease_seconds": 600}
    )
    assert claimed.status_code == 200, claimed.text
    handed = await client.post(
        f"/v1/tasks/{key}/handoff",
        headers=executor,
        json={"next_status": "REVIEWING", "brief": BRIEF},
    )
    assert handed.status_code == 200, handed.text
    return key, handed.json()["task"]["version"]


async def _mcp_call(api_base_url, headers, tool, arguments):
    async with httpx.AsyncClient(base_url=api_base_url, headers=headers, timeout=60) as http:
        async with streamable_http_client(f"{api_base_url}/mcp/", http_client=http) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                if tool == "__list__":
                    return await session.list_tools()
                return await session.call_tool(tool, arguments)


# ----------------------------------------------------------------- AG-CO-01


async def test_rework_returns_task_to_executor_on_rest_and_mcp(
    client, admin_headers, workspace, api_base_url
):
    executor, token = await _agent(client, admin_headers, workspace, "EXECUTOR")
    auditor, _ = await _agent(client, admin_headers, workspace, "AUDITOR")
    rest_key, version = await _in_review(client, workspace, executor)
    rest = await client.post(
        f"/v1/tasks/{rest_key}/review",
        headers=auditor,
        json={
            "decision": "reject",
            "next_status": "IN_PROGRESS",
            "feedback": "add section 3",
            "expected_version": version,
        },
    )
    assert rest.status_code == 200, rest.text
    body = rest.json()
    assert body["task"]["status"] == "IN_PROGRESS"
    assert body["effective_status"] == "IN_PROGRESS" and body["reassigned"] is False
    assert body["task"]["claimed_by"] == token["agent_signature"]

    mcp_key, mcp_version = await _in_review(client, workspace, executor)
    result = await _mcp_call(
        api_base_url,
        auditor,
        "review_task",
        {
            "task_key": mcp_key,
            "decision": "reject",
            "next_status": "IN_PROGRESS",
            "feedback": "add section 3",
            "expected_version": mcp_version,
        },
    )
    assert result.isError is False, result.content[0].text
    mcp_body = result.structuredContent
    for field in ("status", "claimed_by", "review_feedback", "review_round"):
        assert mcp_body["task"][field] == body["task"][field], field
    assert mcp_body["effective_status"] == body["effective_status"]

    # The executor keeps working under its lease and can hand off again.
    again = await client.post(
        f"/v1/tasks/{rest_key}/handoff",
        headers=executor,
        json={"next_status": "REVIEWING", "brief": BRIEF},
    )
    assert again.status_code == 200, again.text


@pytest.mark.parametrize(
    ("decision", "next_status", "accepted"),
    [
        ("approve", "DONE", True),
        ("approve", "HANDOFF", True),
        ("approve", "READY", True),
        ("approve", "IN_PROGRESS", False),
        ("reject", "READY", True),
        ("reject", "IN_PROGRESS", True),
        ("reject", "DONE", False),
        ("reject", "FOO", False),
    ],
)
async def test_review_targets_match_on_rest_and_mcp(
    client, admin_headers, workspace, api_base_url, decision, next_status, accepted
):
    executor, _ = await _agent(client, admin_headers, workspace, "EXECUTOR")
    auditor, _ = await _agent(client, admin_headers, workspace, "AUDITOR")
    rest_key, version = await _in_review(client, workspace, executor)
    rest = await client.post(
        f"/v1/tasks/{rest_key}/review",
        headers=auditor,
        json={"decision": decision, "next_status": next_status, "expected_version": version,
              "feedback": "needs another pass"},
    )
    mcp_key, mcp_version = await _in_review(client, workspace, executor)
    result = await _mcp_call(
        api_base_url,
        auditor,
        "review_task",
        {
            "task_key": mcp_key,
            "decision": decision,
            "next_status": next_status,
            "expected_version": mcp_version,
            "feedback": "needs another pass",
        },
    )
    assert (rest.status_code == 200) is accepted, rest.text
    assert (result.isError is False) is accepted, result.content[0].text
    if not accepted:
        assert rest.status_code == 422
        assert "IN_PROGRESS" in rest.json()["error"]["details"]["all"]
        assert result.structuredContent["code"] == "validation_error"


async def test_review_rounds_and_history(client, admin_headers, workspace):
    executor, _ = await _agent(client, admin_headers, workspace, "EXECUTOR")
    auditor, _ = await _agent(client, admin_headers, workspace, "AUDITOR")
    key, version = await _in_review(client, workspace, executor)
    first = await client.post(
        f"/v1/tasks/{key}/review",
        headers=auditor,
        json={"decision": "reject", "next_status": "IN_PROGRESS", "feedback": "X",
              "expected_version": version},
    )
    assert first.json()["task"]["review_feedback"] == "X"
    resubmitted = await client.post(
        f"/v1/tasks/{key}/handoff",
        headers=executor,
        json={"next_status": "REVIEWING", "brief": BRIEF},
    )
    task = resubmitted.json()["task"]
    assert task["review_feedback"] is None and task["review_round"] == 1
    assert task["review_history"][0]["review_feedback"] == "X"
    assert task["review_history"][0]["resolved_at"] is not None
    second = await client.post(
        f"/v1/tasks/{key}/review",
        headers=auditor,
        json={"decision": "reject", "next_status": "IN_PROGRESS", "feedback": "Y",
              "expected_version": task["version"]},
    )
    assert second.json()["task"]["review_feedback"] == "Y"
    assert second.json()["task"]["review_round"] == 2
    final = await client.post(
        f"/v1/tasks/{key}/handoff",
        headers=executor,
        json={"next_status": "REVIEWING", "brief": BRIEF},
    )
    done = await client.post(
        f"/v1/tasks/{key}/review",
        headers=auditor,
        json={"decision": "approve", "next_status": "DONE",
              "expected_version": final.json()["task"]["version"]},
    )
    assert done.status_code == 200, done.text
    assert done.json()["task"]["review_feedback"] is None
    assert len(done.json()["task"]["review_history"]) == 2


# ----------------------------------------------------------------- AG-ST-03


async def test_large_content_is_accepted_and_round_trips(client, workspace):
    headers = workspace["headers"]
    ascii_doc = await client.put(
        "/v1/documents/big/ascii", headers=headers,
        json={"content": "a" * 8_195, "expected_version": 0},
    )
    assert ascii_doc.status_code == 200, ascii_doc.text
    assert ascii_doc.json()["chunk_count"] >= 1
    assert ascii_doc.json()["content_bytes"] == 8_195
    chinese = "协作" * 1_366 + "末尾"
    assert len(chinese.encode("utf-8")) >= 8_195
    cjk = await client.put(
        "/v1/documents/big/cjk", headers=headers,
        json={"content": chinese, "expected_version": 0},
    )
    assert cjk.status_code == 200, cjk.text

    paragraphs = [f"Paragraph {index}: " + ("lorem ipsum " * 24) for index in range(700)]
    paragraphs[650] = "Paragraph 650: the quokka-ledger reconciliation finished on time."
    body = "# Long report\n\n" + "\n\n".join(paragraphs)
    assert len(body) >= 200_000
    long_doc = await client.put(
        "/v1/documents/big/long", headers=headers,
        json={"content": body, "expected_version": 0},
    )
    assert long_doc.status_code == 200, long_doc.text
    deref = await client.post("/v1/dereference", headers=headers, json={"uri": "doc://big/long"})
    assert deref.json()["document"]["content"] == body
    assert deref.json()["content_total_bytes"] == len(body.encode("utf-8"))
    hits = await client.post(
        "/v1/query", headers=headers,
        json={"query": "quokka-ledger reconciliation", "limit": 5},
    )
    hit = next(item for item in hits.json()["items"] if item["doc_key"] == "big/long")
    assert hit["matched_chunks"][0]["chunk_index"] > 0
    assert "quokka-ledger" in hit["matched_chunks"][0]["snippet"]

    window = await client.post(
        "/v1/dereference", headers=headers,
        json={"uri": "doc://big/cjk", "content_offset": 1, "content_limit": 10},
    )
    sliced = window.json()
    assert sliced["content_truncated"] is True
    assert sliced["document"]["content"] in chinese  # sliced on character boundaries

    too_big = await client.put(
        "/v1/documents/big/too-big", headers=headers,
        json={"content": "b" * 1_048_577, "expected_version": 0},
    )
    assert too_big.status_code == 413
    error = too_big.json()["error"]
    assert error["code"] == "content_too_large"
    assert error["details"]["limit_bytes"] == 1_048_576
    assert error["details"]["action"] == "split_into_parts"


async def test_register_parts_joins_existing_documents(client, workspace):
    headers = workspace["headers"]
    stem = uuid.uuid4().hex[:6]
    for index in range(2):
        await client.put(
            f"/v1/documents/manual/{stem}/p{index}", headers=headers,
            json={"content": f"BODY-{index}", "expected_version": 0},
        )
    await client.put(
        f"/v1/documents/manual/{stem}", headers=headers,
        json={"content": "Index", "expected_version": 0},
    )
    registered = await client.post(
        f"/v1/documents/manual/{stem}/parts", headers=headers,
        json={"part_refs": [f"manual/{stem}/p0", f"manual/{stem}/p1"]},
    )
    assert registered.status_code == 200, registered.text
    joined = await client.post(
        "/v1/dereference", headers=headers,
        json={"uri": f"doc://manual/{stem}", "expand_parts": True},
    )
    assert joined.json()["expanded_content"] == "Index\n\nBODY-0\n\nBODY-1"
    part = await client.get(f"/v1/documents/manual/{stem}/p1", headers=headers)
    assert part.json()["frontmatter"]["part_index"] == 1


# ----------------------------------------------------------------- AG-ST-04


async def test_own_drafts_visible_others_counted(client, admin_headers, workspace, api_base_url):
    writer, _ = await _agent(client, admin_headers, workspace, "EXECUTOR")
    reader, _ = await _agent(client, admin_headers, workspace, "EXECUTOR")
    word = f"zephyr-{uuid.uuid4().hex[:6]}"
    await client.put(
        f"/v1/documents/drafts/{word}", headers=writer,
        json={"content": f"notes about {word}", "expected_version": 0},
    )
    own = await client.post("/v1/query", headers=writer, json={"query": word})
    mine = [item for item in own.json()["items"] if item["doc_key"] == f"drafts/{word}"]
    assert mine and mine[0]["is_own"] is True and mine[0]["trust"] == "UNTRUSTED"
    other = await client.post("/v1/query", headers=reader, json={"query": word})
    assert all(item["doc_key"] != f"drafts/{word}" for item in other.json()["items"])
    assert other.json()["excluded"]["unverified_by_others"] >= 1
    opened = await client.post(
        "/v1/query", headers=reader, json={"query": word, "include_unverified": True}
    )
    assert any(item["doc_key"] == f"drafts/{word}" for item in opened.json()["items"])
    hidden = await client.post(
        "/v1/query", headers=writer, json={"query": word, "include_own_unverified": False}
    )
    assert all(item["doc_key"] != f"drafts/{word}" for item in hidden.json()["items"])

    tools = await _mcp_call(api_base_url, writer, "__list__", {})
    description = next(t for t in tools.tools if t.name == "query_workspace").description
    assert "list_documents" in description and "include_own_unverified" in description


# ----------------------------------------------------------------- AG-ST-05


async def test_conflicts_carry_current_state(client, admin_headers, workspace):
    headers = workspace["headers"]
    key = f"conflict/{uuid.uuid4().hex[:6]}"
    await client.put(f"/v1/documents/{key}", headers=headers,
                     json={"content": "v1", "expected_version": 0})
    for version in range(1, 5):
        await client.put(f"/v1/documents/{key}", headers=headers,
                         json={"content": f"v{version + 1}", "expected_version": version})
    stale = await client.put(
        f"/v1/documents/{key}", headers=headers,
        json={"content": "stale", "expected_version": 3, "return_current_on_conflict": True},
    )
    assert stale.status_code == 409
    details = stale.json()["error"]["details"]
    assert details["current_version"] == 5 and details["expected_version"] == 3
    current = await client.post("/v1/dereference", headers=headers, json={"uri": f"doc://{key}"})
    assert details["current_content_hash"] == current.json()["document"]["content_hash"]
    assert details["current_content"] == current.json()["document"]["content"]
    assert details["updated_by_type"] == "agent"
    retried = await client.put(
        f"/v1/documents/{key}", headers=headers,
        json={"content": "merged", "expected_version": details["current_version"]},
    )
    assert retried.status_code == 200 and retried.json()["document"]["version"] == 6

    email = f"conflict-{uuid.uuid4().hex[:6]}@example.test"
    await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/human-users",
        headers=admin_headers,
        json={"email": email, "display_name": "H", "role": "OWNER",
              "password": "correct-horse-battery"},
    )
    login = await client.post(
        "/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    human = {"Authorization": f"Bearer {login.json()['token']}"}
    await client.put(f"/v1/documents/{key}", headers=human,
                     json={"content": "human edit", "expected_version": 6})
    clash = await client.put(f"/v1/documents/{key}", headers=headers,
                             json={"content": "agent", "expected_version": 6})
    assert clash.json()["error"]["details"]["updated_by_type"] == "human"
    digest = hashlib.sha256(b"human edit").hexdigest()
    assert digest in json.dumps(clash.json()["error"]["details"]["current_content_hash"])
