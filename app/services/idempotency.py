"""Idempotency records for write requests (PRD AG-ST-18 point 1, §6.1 point 1).

Scope is ``(principal, tool, key)``: the principal is a SHA-256 of the
presenting credential, so only the same credential can replay a key. Within
the window the same key with the same body replays the first response with
``replayed: true``; a different body is ``idempotency_key_reused``; a request
still running is ``idempotency_in_progress``. Retryable outcomes (5xx, 429,
in-progress) are never stored, so a retry with the same key runs again.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from app.config import get_settings
from app.database import async_session_factory
from app.errors import ApiError

settings = get_settings()

MAX_KEY_LENGTH = 128


def credential_hash(raw_credential: str) -> str:
    return hashlib.sha256(raw_credential.encode("utf-8")).hexdigest()


def request_hash(*parts: Any) -> str:
    digest = hashlib.sha256()
    for part in parts:
        if isinstance(part, bytes):
            digest.update(part)
        else:
            digest.update(
                json.dumps(part, sort_keys=True, ensure_ascii=False, default=str).encode()
            )
        digest.update(b"\x00")
    return digest.hexdigest()


def validate_key(key: str) -> str:
    key = key.strip()
    if not key or len(key) > MAX_KEY_LENGTH:
        raise ApiError(
            422,
            "validation_error",
            f"idempotency_key must be 1-{MAX_KEY_LENGTH} characters",
            details={
                "field": "idempotency_key",
                "limit": MAX_KEY_LENGTH,
                "actual": len(key),
            },
        )
    return key


def should_store(status_code: int) -> bool:
    """Store deterministic outcomes only; retryable ones must run again."""
    return status_code < 500 and status_code != 429


class UnknownCredential(Exception):
    """The credential maps to no tenant; the route will reject the request."""


_TENANT_FOR_CREDENTIAL = text(
    """
SELECT tenant_id FROM workspaces WHERE api_key_hash = :hash
UNION ALL
SELECT w.tenant_id FROM agent_tokens t JOIN workspaces w ON w.id = t.workspace_id
WHERE t.api_key_hash = :hash
UNION ALL
SELECT w.tenant_id FROM human_sessions s JOIN workspaces w ON w.id = s.workspace_id
WHERE s.token_hash = :hash
LIMIT 1
"""
)


@dataclass
class Replay:
    http_status: int
    response: Any


async def begin(principal: str, tool: str, key: str, body_hash: str) -> Replay | None:
    """Claim the key. Returns a stored response to replay, or None to proceed.

    Records belong to the tenant of the presenting credential (PRD AG-ID-07:
    every persisted object carries a server-derived tenant_id).
    """
    async with async_session_factory() as session:
        tenant_id = await session.scalar(_TENANT_FOR_CREDENTIAL, {"hash": principal})
        if tenant_id is None:
            raise UnknownCredential()
        # Expired records (outside the window) and abandoned in-progress
        # claims no longer bind the key.
        await session.execute(
            text(
                """
DELETE FROM idempotency_records
WHERE tenant_id = :tenant AND principal_hash = :principal AND tool = :tool
  AND idempotency_key = :key
  AND (created_at < NOW() - make_interval(hours => :window)
       OR (status = 'in_progress'
           AND created_at < NOW() - make_interval(secs => :stale)))
"""
            ),
            {
                "tenant": tenant_id,
                "principal": principal,
                "tool": tool,
                "key": key,
                "window": settings.idempotency_window_hours,
                "stale": settings.idempotency_stale_seconds,
            },
        )
        inserted = await session.scalar(
            text(
                """
INSERT INTO idempotency_records
    (tenant_id, principal_hash, tool, idempotency_key, request_hash, status)
VALUES (:tenant, :principal, :tool, :key, :hash, 'in_progress')
ON CONFLICT (tenant_id, principal_hash, tool, idempotency_key) DO NOTHING
RETURNING id
"""
            ),
            {
                "tenant": tenant_id,
                "principal": principal,
                "tool": tool,
                "key": key,
                "hash": body_hash,
            },
        )
        if inserted is not None:
            await session.commit()
            return None
        row = (
            await session.execute(
                text(
                    """
SELECT request_hash, status, http_status, response, created_at
FROM idempotency_records
WHERE tenant_id = :tenant AND principal_hash = :principal AND tool = :tool
  AND idempotency_key = :key
"""
                ),
                {"tenant": tenant_id, "principal": principal, "tool": tool, "key": key},
            )
        ).mappings().one_or_none()
        await session.commit()
    if row is None:
        # Lost a race with a concurrent cleanup; the caller may retry.
        raise ApiError(
            409,
            "idempotency_in_progress",
            "the first request with this idempotency_key is still being processed",
            headers={"Retry-After": "1"},
            details={"retry_after_ms": 1000},
        )
    if row["request_hash"] != body_hash:
        raise ApiError(
            409,
            "idempotency_key_reused",
            "this idempotency_key was already used with a different request body",
            details={
                "original_request_hash": row["request_hash"],
                "first_seen_at": row["created_at"].isoformat(),
            },
        )
    if row["status"] != "completed":
        raise ApiError(
            409,
            "idempotency_in_progress",
            "the first request with this idempotency_key is still being processed",
            headers={"Retry-After": "1"},
            details={"retry_after_ms": 1000},
        )
    return Replay(http_status=int(row["http_status"] or 200), response=row["response"])


async def complete(
    principal: str, tool: str, key: str, http_status: int, response: Any
) -> None:
    async with async_session_factory() as session:
        if should_store(http_status):
            await session.execute(
                text(
                    """
UPDATE idempotency_records
SET status = 'completed', http_status = :status,
    response = CAST(:response AS jsonb), completed_at = NOW()
WHERE principal_hash = :principal AND tool = :tool AND idempotency_key = :key
"""
                ),
                {
                    "principal": principal,
                    "tool": tool,
                    "key": key,
                    "status": http_status,
                    "response": json.dumps(response, ensure_ascii=False, default=str),
                },
            )
        else:
            await _delete(session, principal, tool, key)
        await session.commit()


async def abort(principal: str, tool: str, key: str) -> None:
    async with async_session_factory() as session:
        await _delete(session, principal, tool, key)
        await session.commit()


async def _delete(session, principal: str, tool: str, key: str) -> None:
    await session.execute(
        text(
            """
DELETE FROM idempotency_records
WHERE principal_hash = :principal AND tool = :tool AND idempotency_key = :key
  AND status = 'in_progress'
"""
        ),
        {"principal": principal, "tool": tool, "key": key},
    )


async def purge_expired() -> int:
    async with async_session_factory() as session:
        result = await session.execute(
            text(
                "DELETE FROM idempotency_records "
                "WHERE created_at < NOW() - make_interval(hours => :window)"
            ),
            {"window": settings.idempotency_window_hours},
        )
        await session.commit()
        return int(result.rowcount or 0)
