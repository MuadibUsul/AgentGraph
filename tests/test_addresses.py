"""Address specification acceptance (PRD AG-ST-17)."""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

from app.addresses import doc_key_violation, is_canonical, namespace_violation

V2 = {"AgentGraph-Version": "2026-11"}


def test_grammar_rules():
    assert doc_key_violation("goals/q4-plan") is None
    assert doc_key_violation("Goals/Q4 Plan").rule == "lowercase_only"
    assert doc_key_violation("goals/q4 plan").rule == "segment_charset"
    assert doc_key_violation("a/../b").rule == "no_dot_segments"
    assert doc_key_violation("a/_private").rule == "reserved_underscore_segment"
    assert doc_key_violation("/".join(["a"] * 9)).rule == "max_8_segments"
    assert namespace_violation("research") is None
    assert namespace_violation("system").rule == "reserved_namespace"
    assert namespace_violation("teamA").rule == "lowercase_only"
    assert namespace_violation("raw_inbox").rule == "namespace_charset"
    assert is_canonical("doc_01JAN0000000000000000000AB", "doc")
    assert not is_canonical("doc_short")


pytestmark_integration = pytest.mark.integration


def _db() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def _put(client, headers, key, content="x", version=0, **extra):
    return await client.put(
        f"/v1/documents/{key}",
        headers=headers,
        json={"content": content, "expected_version": version, **extra},
    )


async def _owner(client, admin_headers, workspace):
    email = f"addr-{uuid.uuid4().hex[:8]}@example.test"
    await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/human-users",
        headers=admin_headers,
        json={"email": email, "display_name": "O", "role": "OWNER",
              "password": "correct-horse-battery"},
    )
    login = await client.post(
        "/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    return {"Authorization": f"Bearer {login.json()['token']}"}


@pytest.mark.integration
async def test_documents_and_tasks_have_canonical_ids(client, workspace):
    created = await _put(client, workspace["headers"], f"goals/{uuid.uuid4().hex[:6]}")
    document = created.json()["document"]
    assert is_canonical(document["canonical_id"], "doc")
    assert document["alias"] == document["doc_key"]
    by_id = await client.get(
        f"/v1/documents/{document['canonical_id']}", headers=workspace["headers"]
    )
    assert by_id.status_code == 200
    assert by_id.json()["doc_key"] == document["doc_key"]
    deref = await client.post(
        "/v1/dereference",
        headers=workspace["headers"],
        json={"uri": document["canonical_id"]},
    )
    assert deref.status_code == 200, deref.text
    task = await client.post(
        "/v1/tasks", headers=workspace["headers"], json={"task_key": "ADDR-1", "title": "t"}
    )
    task_id = task.json()["canonical_id"]
    assert is_canonical(task_id, "task")
    assert (await client.get(f"/v1/tasks/{task_id}", headers=workspace["headers"])).json()[
        "task_key"
    ] == "ADDR-1"
    whoami = await client.get("/v1/whoami", headers=workspace["headers"])
    assert is_canonical(whoami.json()["workspace"]["canonical_id"], "ws")


@pytest.mark.integration
async def test_rename_keeps_id_and_redirects_old_alias(client, workspace):
    old = f"goals/q4-plan-{uuid.uuid4().hex[:6]}"
    new = f"goals/2026-q4-{uuid.uuid4().hex[:6]}"
    document = (await _put(client, workspace["headers"], old)).json()["document"]
    task = await client.post(
        "/v1/tasks",
        headers=workspace["headers"],
        json={"task_key": f"CTX-{uuid.uuid4().hex[:6]}", "title": "ctx",
              "input_context": {"doc_key": old}},
    )
    renamed = await client.post(
        f"/v1/documents/{old}/rename",
        headers=workspace["headers"],
        json={"new_alias": new, "expected_version": 1},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["document"]["canonical_id"] == document["canonical_id"]
    assert renamed.json()["document"]["alias"] == new
    via_old = await client.get(f"/v1/documents/{old}", headers=workspace["headers"])
    assert via_old.status_code == 200
    body = via_old.json()
    assert body["canonical_id"] == document["canonical_id"] and body["doc_key"] == new
    assert any(w["code"] == "alias_redirected" for w in body["warnings"])
    # The task's stored pointer still resolves without any change.
    claimed = await client.post(
        f"/v1/tasks/{task.json()['task_key']}/claim", headers=workspace["headers"], json={}
    )
    assert claimed.status_code == 200, claimed.text
    assert [d["canonical_id"] for d in claimed.json()["documents"]] == [document["canonical_id"]]
    # The old alias is never assigned again.
    reuse = await _put(client, workspace["headers"], old)
    assert reuse.status_code in (200, 409)
    assert reuse.json().get("document", {}).get("canonical_id", document["canonical_id"]) == \
        document["canonical_id"]


@pytest.mark.integration
async def test_purge_leaves_tombstone(client, admin_headers, workspace):
    owner = await _owner(client, admin_headers, workspace)
    key = f"goals/purged-{uuid.uuid4().hex[:6]}"
    document = (await _put(client, workspace["headers"], key)).json()["document"]
    early = await client.post(
        f"/v1/human/documents/{key}/purge",
        headers=owner,
        json={"reason": "test", "confirm": key},
    )
    assert early.status_code == 409 and early.json()["error"]["code"] == "retention_not_met"
    archived = await client.post(
        f"/v1/documents/{key}/archive", headers=workspace["headers"], json={}
    )
    assert archived.status_code == 200, archived.text
    purged = await client.post(
        f"/v1/human/documents/{key}/purge",
        headers=owner,
        json={"reason": "test residue", "confirm": key},
    )
    assert purged.status_code == 200, purged.text
    assert purged.json()["id"] == document["canonical_id"]
    recreate = await _put(client, workspace["headers"], key)
    assert recreate.status_code == 409
    assert recreate.json()["error"]["code"] == "alias_tombstoned"
    gone = await client.post(
        "/v1/dereference",
        headers=workspace["headers"],
        json={"uri": f"doc://{document['canonical_id']}"},
    )
    assert gone.status_code == 410
    assert gone.json()["error"]["code"] == "gone"
    assert gone.json()["error"]["details"]["alias"] == key


@pytest.mark.integration
async def test_frontmatter_refs_resolve_to_canonical_ids(client, admin_headers, workspace):
    target = (await _put(client, workspace["headers"], f"goals/t-{uuid.uuid4().hex[:6]}")).json()[
        "document"
    ]
    task = (
        await client.post(
            "/v1/tasks",
            headers=workspace["headers"],
            json={"task_key": f"REF-{uuid.uuid4().hex[:6]}", "title": "ref"},
        )
    ).json()
    token = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        headers=admin_headers,
        json={"agent_signature": f"ref_{uuid.uuid4().hex[:6]}", "role_scope": "SCOUT"},
    )
    whoami = (await client.get("/v1/whoami", headers=workspace["headers"])).json()
    source = await _put(
        client,
        workspace["headers"],
        f"goals/s-{uuid.uuid4().hex[:6]}",
        frontmatter={
            "depends_on": {"$ref": target["doc_key"]},
            "task": {"$ref": task["task_key"]},
            "token": {"$ref": token.json()["token"]["canonical_id"]},
            "workspace": {"$ref": whoami["workspace"]["canonical_id"]},
            "missing": {"$ref": "goals/does-not-exist"},
        },
    )
    assert source.status_code == 200, source.text
    frontmatter = source.json()["document"]["frontmatter"]
    assert frontmatter["depends_on"]["$ref"] == target["canonical_id"]
    assert frontmatter["task"]["$ref"] == task["canonical_id"]
    assert frontmatter["token"]["$ref"] == token.json()["token"]["canonical_id"]
    assert frontmatter["workspace"]["$ref"] == whoami["workspace"]["canonical_id"]
    assert frontmatter["missing"]["$ref"] == "goals/does-not-exist"
    assert any(w["code"] == "dangling_ref" for w in source.json()["warnings"])
    conn = await asyncpg.connect(_db())
    try:
        edges = await conn.fetch(
            "SELECT target_canonical_id FROM document_refs r JOIN shared_documents d "
            "ON d.id = r.source_document_id WHERE d.canonical_id = $1",
            source.json()["document"]["canonical_id"],
        )
    finally:
        await conn.close()
    assert {row["target_canonical_id"] for row in edges} >= {
        target["canonical_id"], task["canonical_id"]
    }


@pytest.mark.integration
async def test_address_grammar_is_versioned(client, workspace):
    strict = await client.put(
        "/v1/documents/Goals/Q4 Plan",
        headers={**workspace["headers"], **V2},
        json={"content": "x", "expected_version": 0},
    )
    assert strict.status_code == 422
    error = strict.json()["error"]
    assert error["code"] == "invalid_address" and error["details"]["rule"] == "lowercase_only"
    legacy = await client.put(
        "/v1/documents/Goals/Legacy-Key",
        headers=workspace["headers"],
        json={"content": "x", "expected_version": 0},
    )
    assert legacy.status_code == 200, legacy.text
    assert any(w["code"] == "legacy_alias" for w in legacy.json()["warnings"])
    reserved = await client.put(
        "/v1/documents/sys/x",
        headers=workspace["headers"],
        json={"content": "x", "expected_version": 0, "namespace": "system"},
    )
    assert reserved.status_code == 422


@pytest.mark.integration
async def test_namespace_register_before_use(client, workspace):
    namespace = f"research-{uuid.uuid4().hex[:6]}"
    strict = await _put(
        client, {**workspace["headers"], **V2}, f"r/{uuid.uuid4().hex[:6]}", namespace=namespace
    )
    assert strict.status_code == 422
    error = strict.json()["error"]
    assert error["code"] == "namespace_not_registered"
    assert error["details"]["action"] == "register_namespace"
    registered = await client.post(
        "/v1/namespaces",
        headers=workspace["headers"],
        json={"namespace": namespace, "purpose": "research notes"},
    )
    assert registered.status_code == 201, registered.text
    retry = await _put(
        client, {**workspace["headers"], **V2}, f"r/{uuid.uuid4().hex[:6]}", namespace=namespace
    )
    assert retry.status_code == 200, retry.text
    auto_ns = f"auto-{uuid.uuid4().hex[:6]}"
    baseline = await _put(
        client, workspace["headers"], f"a/{uuid.uuid4().hex[:6]}", namespace=auto_ns
    )
    assert baseline.status_code == 200
    assert any(w["code"] == "namespace_auto_registered" for w in baseline.json()["warnings"])
    listed = await client.get("/v1/namespaces", headers=workspace["headers"])
    names = {item["namespace"] for item in listed.json()["items"]}
    assert {namespace, auto_ns, "default"} <= names


@pytest.mark.integration
async def test_create_over_existing_alias_is_refused_in_new_version(client, workspace):
    key = f"goals/dup-{uuid.uuid4().hex[:6]}"
    first = await _put(client, {**workspace["headers"], **V2}, key)
    assert first.status_code == 200
    again = await _put(client, {**workspace["headers"], **V2}, key)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "alias_taken"
    assert again.json()["error"]["details"]["id"] == first.json()["document"]["canonical_id"]
    baseline = await _put(client, workspace["headers"], key)
    assert baseline.json()["error"]["code"] == "version_conflict"


@pytest.mark.integration
async def test_parts_expand_in_order(client, workspace):
    stem = uuid.uuid4().hex[:6]
    parts = []
    for index in range(3):
        part = await _put(
            client, workspace["headers"], f"book/{stem}/part-{index}", content=f"PART-{index}"
        )
        parts.append(part.json()["document"])
    parent = await _put(
        client,
        workspace["headers"],
        f"book/{stem}",
        content="Contents",
        frontmatter={
            "parts": [{"$ref": parts[i]["doc_key"], "index": i} for i in (2, 0, 1)]
        },
    )
    assert parent.status_code == 200, parent.text
    expanded = await client.post(
        "/v1/dereference",
        headers=workspace["headers"],
        json={"uri": f"doc://book/{stem}", "expand_parts": True},
    )
    assert expanded.status_code == 200, expanded.text
    body = expanded.json()
    assert body["expanded_content"] == "Contents\n\nPART-0\n\nPART-1\n\nPART-2"
    assert [p["id"] for p in body["parts"]] == [p["canonical_id"] for p in parts]


@pytest.mark.integration
async def test_every_existing_object_has_a_canonical_id(client, workspace):
    await _put(client, workspace["headers"], f"goals/m-{uuid.uuid4().hex[:6]}")
    conn = await asyncpg.connect(_db())
    try:
        for table in ("workspaces", "shared_documents", "collab_tasks", "context_slots",
                      "agent_tokens", "human_users"):
            missing = await conn.fetchval(
                f"SELECT count(*) FROM {table} WHERE canonical_id IS NULL"  # noqa: S608
            )
            assert missing == 0, table
        duplicates = await conn.fetchval(
            "SELECT count(*) FROM (SELECT workspace_id, doc_key FROM shared_documents "
            "GROUP BY 1, 2 HAVING count(*) > 1) d"
        )
        assert duplicates == 0
    finally:
        await conn.close()
