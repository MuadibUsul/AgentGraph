"""Verifiable lineage across tenants (PRD AG-ID-08).

Each tenant has an Ed25519 key pair (``tenant_signing_keys``, platform-only);
the public keys are published as a JWKS and a rotated key stays published
for ``OVERLAP_DAYS``. ``attest`` signs a compact JWS for one audience: the
chain of grants from the root down to a token (or to the token that wrote a
document version), each link named by a pairwise pseudonym
(HMAC of the audience and the token id under a per-tenant secret), so the
audience learns what was granted but never the real ids, names, siblings or
subtree. ``scripts/verify_lineage.py`` verifies one with the public key only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, ensure_namespace, require_permission
from app.database import async_session_factory
from app.errors import ApiError, Forbidden, NotFound

OVERLAP_DAYS = 7
TTL_MAX = 900


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _new_key() -> tuple[str, str]:
    private = Ed25519PrivateKey.generate()
    raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
    public = private.public_key().public_bytes(serialization.Encoding.Raw,
                                               serialization.PublicFormat.Raw)
    return _b64(raw), _b64(public)


async def _current(platform: AsyncSession, tenant_id: str) -> Any:
    """The tenant's live key; the first one is created on demand (kid = signing_key_id)."""
    row = (await platform.execute(text(
        "SELECT * FROM tenant_signing_keys WHERE tenant_id = :t AND retired_at IS NULL "
        "ORDER BY created_at DESC LIMIT 1"), {"t": tenant_id})).mappings().one_or_none()
    if row is not None:
        return row
    kid = await platform.scalar(text("SELECT signing_key_id FROM tenants WHERE id = :t"),
                                {"t": tenant_id})
    private, public = _new_key()
    await platform.execute(text(
        "INSERT INTO tenant_signing_keys (kid, tenant_id, private_key, public_x, "
        "pseudonym_secret) VALUES (:k, :t, :p, :x, :s) ON CONFLICT (kid) DO NOTHING"),
        {"k": kid, "t": tenant_id, "p": private, "x": public,
         "s": secrets.token_urlsafe(32)})
    await platform.commit()
    return (await platform.execute(text(
        "SELECT * FROM tenant_signing_keys WHERE kid = :k"), {"k": kid})).mappings().one()


async def rotate(principal: Principal | None, tenant_ref: str) -> dict[str, Any]:
    """New signing key; the old one stays published for OVERLAP_DAYS.

    Owners of the node rotate it; ``principal=None`` is the platform admin.
    """
    from app.services import tree

    async with async_session_factory() as platform:
        tenant_id = (await tree.node(platform, principal, tenant_ref))["id"]
        if principal is not None:
            await tree._require(platform, principal, tenant_id, ("owner",))
        old = await _current(platform, tenant_id)
        kid = await platform.scalar(text("SELECT agentgraph_new_id('key')"))
        private, public = _new_key()
        await platform.execute(text(
            "UPDATE tenant_signing_keys SET retired_at = NOW() WHERE kid = :k"),
            {"k": old["kid"]})
        await platform.execute(text(
            "INSERT INTO tenant_signing_keys (kid, tenant_id, private_key, public_x, "
            "pseudonym_secret) VALUES (:k, :t, :p, :x, :s)"),
            {"k": kid, "t": tenant_id, "p": private, "x": public,
             "s": old["pseudonym_secret"]})
        await platform.execute(text("UPDATE tenants SET signing_key_id = :k WHERE id = :t"),
                               {"k": kid, "t": tenant_id})
        await platform.commit()
    return {"kid": kid, "previous_kid": old["kid"],
            "previous_published_until": (datetime.now(timezone.utc)
                                         + timedelta(days=OVERLAP_DAYS)).isoformat()}


async def _tenant_by_alias(platform: AsyncSession, alias: str) -> str:
    tenant_id = await platform.scalar(text(
        "SELECT id FROM tenants WHERE alias = :a AND status <> 'deleted'"), {"a": alias})
    if tenant_id is None:
        raise NotFound("tenant", f"tenant '{alias}' not found")
    return tenant_id


async def jwks(alias: str) -> dict[str, Any]:
    async with async_session_factory() as platform:
        tenant_id = await _tenant_by_alias(platform, alias)
        await _current(platform, tenant_id)
        rows = (await platform.execute(text(
            "SELECT kid, public_x FROM tenant_signing_keys WHERE tenant_id = :t AND "
            "(retired_at IS NULL OR retired_at > NOW() - make_interval(days => :d)) "
            "ORDER BY created_at DESC"), {"t": tenant_id, "d": OVERLAP_DAYS})).all()
    return {"keys": [{"kty": "OKP", "crv": "Ed25519", "alg": "EdDSA", "use": "sig",
                      "kid": kid, "x": x} for kid, x in rows]}


def pseudonym(secret: str, audience: str, token_id: str) -> str:
    digest = hmac.new(secret.encode(), f"{audience}\n{token_id}".encode(), hashlib.sha256)
    return "ref_" + _b64(digest.digest()[:18])


async def _link(platform: AsyncSession, token_id: str, secret: str,
                audience: str) -> dict[str, Any]:
    grant = (await platform.execute(text(
        "SELECT g.scopes, g.namespaces, g.depth, g.purpose, g.issued_at, g.ttl_seconds, "
        "t.expires_at FROM grant_snapshots g LEFT JOIN agent_tokens t "
        "ON t.canonical_id = g.token_id WHERE g.token_id = :t "
        "ORDER BY g.issued_at DESC LIMIT 1"), {"t": token_id})).mappings().one_or_none()
    if grant is None:
        raise ApiError(409, "lineage_unavailable", "a token on the chain has no grant snapshot",
                       details={"ref": pseudonym(secret, audience, token_id)})
    expires = grant["expires_at"] or (
        grant["issued_at"] + timedelta(seconds=grant["ttl_seconds"])
        if grant["ttl_seconds"] else None)
    return {"ref": pseudonym(secret, audience, token_id),
            "grant": {"scopes": sorted(grant["scopes"] or []),
                      "namespaces": sorted(grant["namespaces"] or []),
                      "depth": grant["depth"], "purpose": grant["purpose"]},
            "issued_at": grant["issued_at"].isoformat(),
            "expires_at": expires.isoformat() if expires else None}


async def _chain_for_token(platform: AsyncSession, principal: Principal,
                           token_id: str) -> list[str]:
    row = (await platform.execute(text(
        "SELECT canonical_id, lineage, workspace_id FROM agent_tokens WHERE canonical_id = :t"),
        {"t": token_id})).mappings().one_or_none()
    if row is None or row["workspace_id"] != principal.workspace.id:
        raise NotFound("agent_token", f"token '{token_id}' not found")
    lineage = list(row["lineage"] or [row["canonical_id"]])
    own = principal.token_cid is not None and principal.token_cid in lineage
    if not (own or principal.is_human or principal.is_commander):
        raise Forbidden("you may attest your own token or your subtree only")
    return lineage


def _sign(private_b64: str, kid: str, payload: dict[str, Any]) -> str:
    header = _b64(json.dumps({"alg": "EdDSA", "kid": kid, "typ": "JWT"},
                             separators=(",", ":")).encode())
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    raw = base64.urlsafe_b64decode(private_b64 + "=" * (-len(private_b64) % 4))
    signature = Ed25519PrivateKey.from_private_bytes(raw).sign(f"{header}.{body}".encode())
    return f"{header}.{body}.{_b64(signature)}"


async def attest(session: AsyncSession, principal: Principal, *, audience: str,
                 token_id: str | None = None, doc_id: str | None = None,
                 version: int | None = None, ttl_seconds: int = TTL_MAX) -> dict[str, Any]:
    if not audience or len(audience) > 256:
        raise ApiError(422, "validation_error", "audience is 1-256 characters",
                       details={"field": "audience"})
    if not 1 <= ttl_seconds <= TTL_MAX:
        raise ApiError(422, "validation_error", f"ttl_seconds is 1-{TTL_MAX}",
                       details={"field": "ttl_seconds", "limit": TTL_MAX})
    if (token_id is None) == (doc_id is None):
        raise ApiError(422, "validation_error", "name either a token or a document version",
                       details={"field": "subject"})
    subject: dict[str, Any] = {}
    if doc_id is not None:
        from app.services.documents import _get_document

        require_permission(principal, "doc:read")
        document = await _get_document(session, principal.workspace.id, doc_id)
        ensure_namespace(principal, document.namespace)
        row = (await session.execute(text(
            "SELECT version, content_hash, stamp FROM document_versions WHERE "
            "document_id = :d AND version = :v"),
            {"d": document.id, "v": version or document.version})).mappings().one_or_none()
        if row is None:
            raise NotFound("document_version", f"version {version} not found")
        chain_ids = list((row["stamp"] or {}).get("chain") or [])
        if not chain_ids:
            raise ApiError(409, "lineage_unavailable",
                           "this version was written without a lineage stamp",
                           details={"version": row["version"]})
        subject = {"doc_hash": row["content_hash"], "version": row["version"]}
    tenant_id = principal.workspace.tenant_id
    async with async_session_factory() as platform:
        if token_id is not None:
            chain_ids = await _chain_for_token(platform, principal, token_id)
        key = await _current(platform, tenant_id)
        alias = await platform.scalar(text("SELECT alias FROM tenants WHERE id = :t"),
                                      {"t": tenant_id})
        chain = [await _link(platform, tid, key["pseudonym_secret"], audience)
                 for tid in chain_ids]
    now = int(datetime.now(timezone.utc).timestamp())
    payload = {"iss": f"agentgraph:tenant:{alias}", "aud": audience, "iat": now,
               "exp": now + ttl_seconds, "chain": chain, "subject": subject}
    return {"attestation": _sign(key["private_key"], key["kid"], payload),
            "kid": key["kid"], "expires_at": datetime.fromtimestamp(
                now + ttl_seconds, timezone.utc).isoformat(),
            "jwks_url": f"/.well-known/agentgraph/tenants/{alias}/jwks.json"}


async def revoked(alias: str, ref: str, audience: str) -> dict[str, Any]:
    """Live revocation check for one pseudonymous link."""
    async with async_session_factory() as platform:
        tenant_id = await _tenant_by_alias(platform, alias)
        key = await _current(platform, tenant_id)
        rows = (await platform.execute(text(
            "SELECT canonical_id, revoked_at, revoked_ancestor, expires_at FROM agent_tokens "
            "WHERE tenant_id = :t"), {"t": tenant_id})).mappings().all()
    for row in rows:
        if hmac.compare_digest(pseudonym(key["pseudonym_secret"], audience,
                                         row["canonical_id"]), ref):
            expired = row["expires_at"] is not None and \
                row["expires_at"] <= datetime.now(timezone.utc)
            return {"ref": ref, "known": True,
                    "revoked": bool(row["revoked_at"] or row["revoked_ancestor"]),
                    "expired": expired}
    return {"ref": ref, "known": False, "revoked": False, "expired": False}
