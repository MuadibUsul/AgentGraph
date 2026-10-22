from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


async def _owner(client, admin_headers: dict[str, str], workspace: dict) -> dict[str, str]:
    workspace_id = workspace["workspace"]["id"]
    email = f"picker-{uuid.uuid4().hex[:8]}@example.test"
    created = await client.post(
        f"/v1/admin/workspaces/{workspace_id}/human-users",
        headers=admin_headers,
        json={
            "email": email,
            "display_name": "Picker Owner",
            "role": "OWNER",
            "password": "correct-horse-battery",
        },
    )
    assert created.status_code == 201, created.text
    login = await client.post(
        "/v1/auth/login",
        json={"email": email, "password": "correct-horse-battery"},
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}"}


async def test_namespace_picker_sources_are_visible_to_admin_and_human(
    client, admin_headers, workspace
):
    workspace_id = workspace["workspace"]["id"]
    namespace = f"picker-{uuid.uuid4().hex[:8]}"

    registered = await client.post(
        "/v1/namespaces",
        headers=workspace["headers"],
        json={"namespace": namespace, "purpose": "Namespace picker test"},
    )
    assert registered.status_code == 201, registered.text

    document = await client.put(
        f"/v1/documents/{namespace}/item",
        headers=workspace["headers"],
        json={
            "content": "# Picker item",
            "namespace": namespace,
            "expected_version": 0,
        },
    )
    assert document.status_code in (200, 201), document.text

    admin = await client.get(
        f"/v1/admin/workspaces/{workspace_id}/namespaces",
        headers=admin_headers,
    )
    assert admin.status_code == 200, admin.text
    admin_item = next(
        (item for item in admin.json()["items"] if item["namespace"] == namespace),
        None,
    )
    assert admin_item is not None
    assert admin_item["last_updated_at"]
    assert admin_item["documents"] >= 1

    human_headers = await _owner(client, admin_headers, workspace)
    human = await client.get("/v1/human/namespaces", headers=human_headers)
    assert human.status_code == 200, human.text
    human_item = next(
        (item for item in human.json()["items"] if item["namespace"] == namespace),
        None,
    )
    assert human_item is not None
    assert human_item["last_updated_at"]
