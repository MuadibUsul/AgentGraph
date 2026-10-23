"""Growth instrumentation and `npx veynod init` (v3.34)."""

from __future__ import annotations

import base64
import uuid

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.services.cli_setup import SEAL_INFO, seal


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _keypair() -> tuple[X25519PrivateKey, str]:
    private = X25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return private, base64.b64encode(public).decode()


def _open(sealed: str, private: X25519PrivateKey) -> str:
    """What the CLI does (cli/veynod.mjs openSealed), in Python."""
    version, eph, nonce, ciphertext = sealed.split(".")
    assert version == "v1"
    ephemeral_public = _b64url_decode(eph)
    own_public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    shared = private.exchange(X25519PublicKey.from_public_bytes(ephemeral_public))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=ephemeral_public + own_public,
               info=SEAL_INFO).derive(shared)
    return AESGCM(key).decrypt(_b64url_decode(nonce), _b64url_decode(ciphertext),
                               SEAL_INFO).decode()


def test_sealed_key_opens_only_with_the_cli_private_key():
    private, public_b64 = _keypair()
    sealed = seal("agt_secret_value", base64.b64decode(public_b64))
    assert "agt_secret_value" not in sealed
    assert _open(sealed, private) == "agt_secret_value"
    other, _ = _keypair()
    with pytest.raises(Exception):
        _open(sealed, other)


async def _signup(client) -> tuple[dict[str, str], str]:
    email = f"growth-{uuid.uuid4().hex[:8]}@example.test"
    registered = await client.post("/v1/auth/register", json={
        "email": email, "password": "correct-horse-battery-staple",
        "display_name": "Growth Owner", "workspace_name": "Growth Workspace",
    })
    assert registered.status_code == 201, registered.text
    body = registered.json()
    return {"Authorization": f"Bearer {body['token']}"}, email


async def test_cli_device_flow_hands_a_sealed_key_to_the_terminal(client, admin_headers):
    owner, email = await _signup(client)
    private, public_b64 = _keypair()

    bad = await client.post("/v1/auth/cli/sessions", json={"public_key": "A" * 44 + "AAAA"})
    assert bad.status_code == 422
    started = await client.post("/v1/auth/cli/sessions", json={
        "public_key": public_b64, "client_label": "laptop", "clients": ["claude-code"],
    })
    assert started.status_code == 201, started.text
    flow = started.json()
    assert set(flow) >= {"device_code", "user_code", "verification_uri_complete",
                         "expires_in", "interval"}
    assert flow["verification_uri_complete"].endswith(f"/dashboard/cli/?code={flow['user_code']}")

    pending = await client.post("/v1/auth/cli/poll", json={"device_code": flow["device_code"]})
    assert pending.json()["status"] == "pending"
    unknown = await client.post("/v1/auth/cli/poll", json={"device_code": "x" * 43})
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "invalid_device_code"

    # The console shows what is being approved; anonymous callers see nothing.
    assert (await client.get(f"/v1/auth/cli/sessions/{flow['user_code']}")).status_code == 401
    view = (await client.get(f"/v1/auth/cli/sessions/{flow['user_code']}",
                             headers=owner)).json()
    assert view["status"] == "pending" and view["client_label"] == "laptop"
    assert view["clients"] == ["claude-code"] and view["can_approve"] is True
    assert "device_code" not in view

    approved = await client.post(
        f"/v1/auth/cli/sessions/{flow['user_code'].lower().replace('-', '')}/approve",
        headers=owner, json={"agent_signature": "claude-code"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["agent_signature"] == "claude-code"
    again = await client.post(f"/v1/auth/cli/sessions/{flow['user_code']}/approve",
                              headers=owner, json={})
    assert again.status_code == 409

    collected = (await client.post("/v1/auth/cli/poll",
                                   json={"device_code": flow["device_code"]})).json()
    assert collected["status"] == "approved"
    assert collected["mcp_path"] == "/mcp/" and collected["agent_signature"] == "claude-code"
    api_key = _open(collected["sealed_key"], private)
    assert api_key.startswith("agt_")
    gone = await client.post("/v1/auth/cli/poll", json={"device_code": flow["device_code"]})
    assert gone.status_code == 410

    agent = {"Authorization": f"Bearer {api_key}"}
    assert (await client.get("/v1/orientation", headers=agent)).status_code == 200

    # A second setup in the same workspace gets a fresh name, not a 409.
    _, public2 = _keypair()
    flow2 = (await client.post("/v1/auth/cli/sessions", json={"public_key": public2})).json()
    second = await client.post(f"/v1/auth/cli/sessions/{flow2['user_code']}/approve",
                               headers=owner, json={"agent_signature": "claude-code"})
    assert second.json()["agent_signature"] == "claude-code-2"

    # Denied codes stay denied.
    _, public3 = _keypair()
    flow3 = (await client.post("/v1/auth/cli/sessions", json={"public_key": public3})).json()
    denied = await client.post(f"/v1/auth/cli/sessions/{flow3['user_code']}/deny",
                               headers=owner)
    assert denied.json()["status"] == "denied"
    assert (await client.post("/v1/auth/cli/poll", json={
        "device_code": flow3["device_code"]})).json()["status"] == "denied"


async def test_only_owners_approve_cli_codes(client, admin_headers, workspace):
    email = f"reviewer-{uuid.uuid4().hex[:8]}@example.test"
    await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/human-users",
        headers=admin_headers,
        json={"email": email, "display_name": "R", "role": "REVIEWER",
              "password": "correct-horse-battery"},
    )
    login = await client.post("/v1/auth/login",
                              json={"email": email, "password": "correct-horse-battery"})
    reviewer = {"Authorization": f"Bearer {login.json()['token']}"}
    _, public_b64 = _keypair()
    flow = (await client.post("/v1/auth/cli/sessions", json={"public_key": public_b64})).json()
    view = (await client.get(f"/v1/auth/cli/sessions/{flow['user_code']}",
                             headers=reviewer)).json()
    assert view["can_approve"] is False
    refused = await client.post(f"/v1/auth/cli/sessions/{flow['user_code']}/approve",
                                headers=reviewer, json={})
    assert refused.status_code == 403


async def test_growth_kpis_follow_a_signup_through_the_funnel(client, admin_headers):
    owner, email = await _signup(client)
    minted = await client.post("/v1/human/agent-tokens", headers=owner, json={
        "agent_signature": "builder", "role_scope": "EXECUTOR", "allowed_namespaces": ["*"],
    })
    agent = {"Authorization": f"Bearer {minted.json()['api_key']}"}
    assert (await client.get("/v1/orientation", headers=agent)).status_code == 200
    written = await client.put("/v1/documents/notes/growth", headers=agent,
                               json={"content": "# Growth", "expected_version": 0})
    assert written.status_code == 200, written.text
    verified = await client.post("/v1/human/documents/notes/growth/verify", headers=owner,
                                 json={"expected_version": 1})
    assert verified.status_code == 200, verified.text
    for name in ("upgrade_viewed", "upgrade_intent"):
        recorded = await client.post("/v1/human/product-events", headers=owner,
                                     json={"name": name, "source": "sidebar", "plan": "pro"})
        assert recorded.status_code == 204, recorded.text
    rejected = await client.post("/v1/human/product-events", headers=owner,
                                 json={"name": "signup"})
    assert rejected.status_code == 422
    assert (await client.post("/v1/human/product-events", headers=agent,
                              json={"name": "upgrade_intent"})).status_code == 403

    assert (await client.get("/v1/admin/growth")).status_code == 401
    growth = await client.get("/v1/admin/growth", headers=admin_headers)
    assert growth.status_code == 200, growth.text
    data = growth.json()
    assert set(data["kpis"]) == {"activation_rate", "week2_retention",
                                 "decisions_per_active_workspace",
                                 "upgrade_intent_workspaces"}
    assert data["kpis"]["activation_rate"]["target"] == 0.4
    assert data["kpis"]["upgrade_intent_workspaces"]["value"] >= 1
    assert [step["step"] for step in data["funnel"]] == [
        "signed_up", "token_created", "agent_oriented", "human_decision", "retained_week2"]
    mine = next(row for row in data["recent"] if row["email"] == email)
    assert mine["activated"] is True and mine["agents"] == 1
    assert mine["decisions_7d"] == 1 and mine["upgrade_intent"] is True
    assert mine["first_oriented_at"] is not None
    assert data["cohorts"] and data["cohorts"][0]["signups"] >= 1
