"""`npx veynod init`: a device flow that hands an agent key to a terminal.

1. The CLI makes an X25519 key pair and asks for a session. It gets a secret
   device code (stored here only as a SHA-256) and a short user code.
2. A signed-in owner opens /dashboard/cli/?code=XXXX-XXXX and approves. The
   agent key is minted through the normal console path, then sealed to the
   CLI's public key (ephemeral X25519 + HKDF-SHA256 + AES-256-GCM). The raw
   key is never stored; the server cannot read the sealed copy back.
3. The CLI polls with the device code, receives the sealed key once (the row's
   copy is cleared on pickup) and opens it locally.

cli_sessions is operator-only under RLS, so every read and write here uses an
unscoped session; the key itself is minted in the approver's tenant session.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import ApiError, Forbidden, NotFound
from app.schemas import (
    AgentTokenCreateRequest,
    CliSessionApproveRequest,
    CliSessionCreateRequest,
)
from app.services import product_events

settings = get_settings()

SEAL_INFO = b"veynod-cli-v1"
POLL_INTERVAL_SECONDS = 3
_USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"  # no vowels: no accidental words
_USER_CODE_LENGTH = 8


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(device_code: str) -> str:
    return hashlib.sha256(device_code.encode()).hexdigest()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def normalize_user_code(value: str) -> str:
    return "".join(ch for ch in value.upper() if ch.isalnum())


def format_user_code(value: str) -> str:
    return f"{value[:4]}-{value[4:]}"


def _decode_public_key(value: str) -> bytes:
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_")
    except (ValueError, TypeError):
        raw = b""
    if len(raw) != 32:
        raise ApiError(422, "invalid_public_key",
                       "public_key must be a base64 X25519 public key (32 bytes)")
    return raw


def seal(raw_key: str, client_public_key: bytes) -> str:
    """Encrypt the agent key so only the CLI's private key opens it."""
    ephemeral = X25519PrivateKey.generate()
    shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(client_public_key))
    ephemeral_public = ephemeral.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=ephemeral_public + client_public_key,
        info=SEAL_INFO,
    ).derive(shared)
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, raw_key.encode(), SEAL_INFO)
    return ".".join(("v1", _b64url(ephemeral_public), _b64url(nonce), _b64url(ciphertext)))


async def create_session(
    session: AsyncSession, request: CliSessionCreateRequest, *, base_url: str
) -> dict[str, Any]:
    public_key = _decode_public_key(request.public_key)
    device_code = secrets.token_urlsafe(32)
    expires_at = _now() + timedelta(seconds=settings.cli_session_ttl_seconds)
    for _ in range(5):
        user_code = "".join(
            secrets.choice(_USER_CODE_ALPHABET) for _ in range(_USER_CODE_LENGTH)
        )
        inserted = await session.scalar(
            text(
                "INSERT INTO cli_sessions (device_code_hash, user_code, client_public_key, "
                "client_label, client_kinds, expires_at) "
                "VALUES (:h, :u, :k, :l, CAST(:c AS jsonb), :x) "
                "ON CONFLICT (user_code) DO NOTHING RETURNING id"
            ),
            {
                "h": _hash(device_code),
                "u": user_code,
                "k": base64.b64encode(public_key).decode(),
                "l": (request.client_label or "")[:64],
                "c": json.dumps(sorted(set(request.clients))),
                "x": expires_at,
            },
        )
        if inserted is not None:
            break
    else:  # pragma: no cover - 20^8 codes; five collisions in a row is a bug
        raise ApiError(503, "cli_code_unavailable", "could not allocate a user code")
    # Old rows go away opportunistically; nothing else reads them.
    await session.execute(
        text("DELETE FROM cli_sessions WHERE expires_at < NOW() - INTERVAL '1 day'")
    )
    await session.commit()
    await product_events.record(
        "cli_init_started", clients=",".join(sorted(set(request.clients))) or None
    )
    path = f"/dashboard/cli/?code={format_user_code(user_code)}"
    return {
        "device_code": device_code,
        "user_code": format_user_code(user_code),
        "verification_uri": base_url.rstrip("/") + "/dashboard/cli/",
        "verification_uri_complete": base_url.rstrip("/") + path,
        "expires_in": settings.cli_session_ttl_seconds,
        "interval": POLL_INTERVAL_SECONDS,
    }


async def _row_by_user_code(session: AsyncSession, user_code: str, *, lock: bool = False):
    code = normalize_user_code(user_code)
    if len(code) != _USER_CODE_LENGTH:
        raise NotFound("cli_session", "unknown CLI sign-in code")
    row = (
        await session.execute(
            text(
                "SELECT * FROM cli_sessions WHERE user_code = :u"
                + (" FOR UPDATE" if lock else "")
            ),
            {"u": code},
        )
    ).mappings().one_or_none()
    if row is None:
        raise NotFound("cli_session", "unknown CLI sign-in code")
    return row


def _public_view(row: Any) -> dict[str, Any]:
    status = row["status"]
    if status == "pending" and row["expires_at"] <= _now():
        status = "expired"
    return {
        "user_code": format_user_code(row["user_code"]),
        "status": status,
        "client_label": row["client_label"],
        "clients": list(row["client_kinds"] or []),
        "created_at": row["created_at"].isoformat(),
        "expires_at": row["expires_at"].isoformat(),
        "agent_signature": row["agent_signature"],
    }


async def describe(unscoped: AsyncSession, user_code: str) -> dict[str, Any]:
    return _public_view(await _row_by_user_code(unscoped, user_code))


def _require_owner(principal: Any) -> None:
    if principal.human_role not in ("OWNER", "ADMIN"):
        raise Forbidden("an owner or admin of this workspace approves CLI sign-ins")


async def approve(
    scoped: AsyncSession,
    unscoped: AsyncSession,
    principal: Any,
    user_code: str,
    request: CliSessionApproveRequest,
) -> dict[str, Any]:
    from app import services

    _require_owner(principal)
    row = await _row_by_user_code(unscoped, user_code, lock=True)
    if row["status"] != "pending":
        raise ApiError(409, "cli_session_decided", f"this code was already {row['status']}")
    if row["expires_at"] <= _now():
        raise ApiError(410, "cli_session_expired",
                       "this code expired; run `npx veynod init` again")
    base = request.agent_signature.strip()
    created = None
    for attempt in range(1, 21):
        name = base if attempt == 1 else f"{base}-{attempt}"
        try:
            created = await services.create_agent_token(
                scoped,
                principal.workspace,
                AgentTokenCreateRequest(
                    agent_signature=name,
                    role_scope=request.role_scope,
                    allowed_namespaces=request.allowed_namespaces,
                    purpose=f"npx veynod init ({row['client_label'] or 'terminal'})"[:200],
                ),
            )
            break
        except ApiError as exc:
            if exc.code != "agent_token_exists":
                raise
    if created is None:
        raise ApiError(409, "agent_token_exists", f"pick another name than '{base}'")
    sealed = seal(created.api_key, base64.b64decode(row["client_public_key"]))
    await unscoped.execute(
        text(
            "UPDATE cli_sessions SET status = 'approved', workspace_id = :w, user_id = :u, "
            "agent_signature = :a, token_id = :t, sealed_key = :s, decided_at = NOW() "
            "WHERE id = :id"
        ),
        {
            "w": principal.workspace.id,
            "u": principal.user_id,
            "a": created.token.agent_signature,
            "t": uuid.UUID(str(created.token.id)),
            "s": sealed,
            "id": row["id"],
        },
    )
    await unscoped.commit()
    await product_events.record(
        "cli_init_approved", workspace_id=principal.workspace.id,
        user_id=principal.user_id, role=request.role_scope,
    )
    return {
        "status": "approved",
        "agent_signature": created.token.agent_signature,
        "workspace": {"id": str(principal.workspace.id), "name": principal.workspace.name},
    }


async def deny(unscoped: AsyncSession, principal: Any, user_code: str) -> dict[str, Any]:
    _require_owner(principal)
    row = await _row_by_user_code(unscoped, user_code, lock=True)
    if row["status"] != "pending":
        raise ApiError(409, "cli_session_decided", f"this code was already {row['status']}")
    await unscoped.execute(
        text("UPDATE cli_sessions SET status = 'denied', decided_at = NOW(), "
             "user_id = :u WHERE id = :id"),
        {"u": principal.user_id, "id": row["id"]},
    )
    await unscoped.commit()
    return {"status": "denied"}


async def poll(session: AsyncSession, device_code: str) -> dict[str, Any]:
    row = (
        await session.execute(
            text("SELECT * FROM cli_sessions WHERE device_code_hash = :h FOR UPDATE"),
            {"h": _hash(device_code)},
        )
    ).mappings().one_or_none()
    if row is None:
        raise ApiError(404, "invalid_device_code", "unknown device code")
    status = row["status"]
    now = _now()
    if status == "pending":
        if row["expires_at"] <= now:
            await session.rollback()
            return {"status": "expired"}
        last = row["last_polled_at"]
        await session.execute(
            text("UPDATE cli_sessions SET last_polled_at = NOW() WHERE id = :id"),
            {"id": row["id"]},
        )
        await session.commit()
        if last is not None and (now - last).total_seconds() < POLL_INTERVAL_SECONDS - 1:
            return {"status": "slow_down", "interval": POLL_INTERVAL_SECONDS + 2}
        return {"status": "pending", "interval": POLL_INTERVAL_SECONDS}
    if status == "denied":
        await session.rollback()
        return {"status": "denied"}
    if status == "consumed":
        await session.rollback()
        raise ApiError(410, "cli_session_consumed", "this key was already collected")
    workspace = (
        await session.execute(
            text("SELECT id, name FROM workspaces WHERE id = :w"), {"w": row["workspace_id"]}
        )
    ).mappings().one()
    sealed = row["sealed_key"]
    await session.execute(
        text("UPDATE cli_sessions SET status = 'consumed', sealed_key = NULL, "
             "consumed_at = NOW() WHERE id = :id"),
        {"id": row["id"]},
    )
    await session.commit()
    await product_events.record(
        "cli_init_completed", workspace_id=row["workspace_id"], user_id=row["user_id"]
    )
    return {
        "status": "approved",
        "sealed_key": sealed,
        "agent_signature": row["agent_signature"],
        "workspace": {"id": str(workspace["id"]), "name": workspace["name"]},
        "mcp_path": "/mcp/" if settings.mcp_enabled else None,
        "rest_path": "/v1",
    }
