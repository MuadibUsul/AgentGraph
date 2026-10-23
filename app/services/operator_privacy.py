"""Operator privacy (v3.35): the operator sees counts and changes, never content.

The ADMIN_TOKEN runs the platform: it lists workspaces with their counts and
usage, sets tiers and budgets, backs up, restores, suspends and deletes. For a
private workspace (every workspace the operator did not create itself) it does
not read documents, versions, tasks, slots, audit, timeline, proposals or
events, and it cannot mint keys, tokens or accounts that would read them.

The one way in is support access: the workspace owner opens a time-limited
window from the console (read-only unless they allow changes) and can close it
at any time. Every operator request inside the window is written to the
workspace's access_audit, which the owner reads in the same console card.
There is no operator-side override.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Request
from sqlalchemy import text

from app.database import async_session_factory
from app.errors import ApiError, Forbidden

# Workspace sub-resources the operator may reach on a private workspace:
# counts, usage, quotas and the index queue. Everything else is content.
CONTROL_PLANE_SUFFIXES = frozenset(
    {"", "/stats", "/usage", "/budget", "/index-status", "/metrics", "/reindex"}
)
_WORKSPACE_PATH = re.compile(
    r"^/v1/admin/workspaces/([0-9a-fA-F-]{36})(/.*)?$"
)
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MAX_GRANT_HOURS = 168
AUDIT_ACTION = "operator.support_access"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _denied(workspace_id: Any) -> ApiError:
    return ApiError(
        403,
        "operator_content_denied",
        "this workspace is private: the operator sees its counts and changes, not its content",
        details={
            "workspace_id": str(workspace_id),
            "how": "the workspace owner can open support access in the console "
                   "(Governance → Support access)",
        },
    )


async def _workspace_state(session, workspace_id: Any) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                "SELECT w.operator_managed, g.id AS grant_id, g.allow_write, g.expires_at "
                "FROM workspaces w LEFT JOIN LATERAL ("
                "  SELECT id, allow_write, expires_at FROM support_access_grants s "
                "  WHERE s.workspace_id = w.id AND s.revoked_at IS NULL "
                "    AND s.expires_at > NOW() ORDER BY s.expires_at DESC LIMIT 1"
                ") g ON TRUE WHERE w.id = CAST(:w AS uuid)"
            ),
            {"w": str(workspace_id)},
        )
    ).mappings().one_or_none()
    return dict(row) if row else None


async def require_content_access(
    workspace_id: Any, *, method: str, path: str
) -> None:
    """Let the operator through only for its own workspaces or an open grant."""
    async with async_session_factory() as session:
        state = await _workspace_state(session, workspace_id)
        if state is None or state["operator_managed"]:
            return  # unknown ids 404 in the route; managed ones are the operator's own
        if state["grant_id"] is None:
            raise _denied(workspace_id)
        if method.upper() not in _READ_METHODS and not state["allow_write"]:
            raise ApiError(
                403,
                "operator_write_denied",
                "support access to this workspace is read-only",
                details={"workspace_id": str(workspace_id),
                         "how": "the owner can reopen support access with changes allowed"},
            )
        await session.execute(
            text("INSERT INTO access_audit (workspace_id, action, details) "
                 "VALUES (CAST(:w AS uuid), :a, CAST(:d AS jsonb))"),
            {"w": str(workspace_id), "a": AUDIT_ACTION,
             "d": json.dumps({"method": method.upper(), "path": path[:300],
                              "grant_id": str(state["grant_id"])})},
        )
        await session.execute(
            text("UPDATE support_access_grants SET requests = requests + 1, "
                 "last_request_at = NOW() WHERE id = :g"),
            {"g": state["grant_id"]},
        )
        await session.commit()


async def admin_guard(request: Request) -> None:
    """Router dependency for /v1/admin: content paths need the owner's grant."""
    match = _WORKSPACE_PATH.match(request.url.path)
    if not match:
        return
    if (match.group(2) or "").rstrip("/") in CONTROL_PLANE_SUFFIXES:
        return
    await require_content_access(match.group(1), method=request.method, path=request.url.path)


async def require_tenant_content_access(tenant_id: str, *, method: str, path: str) -> None:
    """Tenant-wide data paths (exports, imports, personal-data search)."""
    async with async_session_factory() as session:
        private = (
            await session.execute(
                text("SELECT id FROM workspaces WHERE tenant_id = :t "
                     "AND NOT operator_managed"),
                {"t": tenant_id},
            )
        ).scalars().all()
    for workspace_id in private:
        await require_content_access(workspace_id, method=method, path=path)


async def require_export_access(export_id: str) -> None:
    """An export archive is the tenant's content."""
    async with async_session_factory() as session:
        tenant_id = await session.scalar(
            text("SELECT tenant_id FROM exports WHERE id = :id"), {"id": export_id})
    if tenant_id:
        await require_tenant_content_access(
            tenant_id, method="GET", path=f"/v1/admin/exports/{export_id}/download")


async def is_operator_managed(workspace_id: Any) -> bool:
    async with async_session_factory() as session:
        state = await _workspace_state(session, workspace_id)
    return bool(state and state["operator_managed"])


# ------------------------------------------------------------ owner side


def _require_owner(principal: Any) -> None:
    if principal.human_role not in ("OWNER", "ADMIN") or principal.user_id is None:
        raise Forbidden("only an owner or admin of this workspace manages support access")


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value or {}


def _grant_out(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "note": row["note"],
        "allow_write": row["allow_write"],
        "created_at": row["created_at"].isoformat(),
        "expires_at": row["expires_at"].isoformat(),
        "revoked_at": row["revoked_at"].isoformat() if row["revoked_at"] else None,
        "requests": row["requests"],
        "last_request_at": row["last_request_at"].isoformat() if row["last_request_at"] else None,
    }


async def status(workspace_id: Any) -> dict[str, Any]:
    async with async_session_factory() as session:
        managed = await session.scalar(
            text("SELECT operator_managed FROM workspaces WHERE id = CAST(:w AS uuid)"),
            {"w": str(workspace_id)},
        )
        grants = (
            await session.execute(
                text("SELECT * FROM support_access_grants WHERE workspace_id = CAST(:w AS uuid) "
                     "ORDER BY created_at DESC LIMIT 10"),
                {"w": str(workspace_id)},
            )
        ).mappings().all()
        log = (
            await session.execute(
                text("SELECT created_at, details FROM access_audit "
                     "WHERE workspace_id = CAST(:w AS uuid) AND action = :a "
                     "ORDER BY created_at DESC LIMIT 100"),
                {"w": str(workspace_id), "a": AUDIT_ACTION},
            )
        ).mappings().all()
    now = _now()
    active = next((g for g in grants if g["revoked_at"] is None and g["expires_at"] > now), None)
    return {
        "private": not managed,
        "operator_managed": bool(managed),
        "active": _grant_out(active) if active else None,
        "grants": [_grant_out(g) for g in grants],
        "operator_requests": [
            {"at": row["created_at"].isoformat(), "method": details.get("method"),
             "path": details.get("path")}
            for row in log
            for details in [_json(row["details"])]
        ],
    }


async def open_grant(principal: Any, *, hours: int, note: str, allow_write: bool) -> dict:
    _require_owner(principal)
    if not 1 <= hours <= MAX_GRANT_HOURS:
        raise ApiError(422, "validation_error",
                       f"support access lasts 1 to {MAX_GRANT_HOURS} hours",
                       details={"field": "hours", "max": MAX_GRANT_HOURS})
    async with async_session_factory() as session:
        await session.execute(
            text("UPDATE support_access_grants SET revoked_at = NOW(), revoked_by = :u "
                 "WHERE workspace_id = CAST(:w AS uuid) AND revoked_at IS NULL "
                 "AND expires_at > NOW()"),
            {"w": str(principal.workspace.id), "u": principal.user_id},
        )
        await session.execute(
            text("INSERT INTO support_access_grants (id, workspace_id, granted_by, note, "
                 "allow_write, expires_at) VALUES (:id, CAST(:w AS uuid), :u, :n, :aw, :x)"),
            {"id": uuid.uuid4(), "w": str(principal.workspace.id), "u": principal.user_id,
             "n": (note or "").strip()[:500], "aw": bool(allow_write),
             "x": _now() + timedelta(hours=hours)},
        )
        await session.execute(
            text("INSERT INTO access_audit (workspace_id, action, details) "
                 "VALUES (CAST(:w AS uuid), 'operator.support_access_opened', CAST(:d AS jsonb))"),
            {"w": str(principal.workspace.id),
             "d": json.dumps({"by": str(principal.user_id), "hours": hours,
                              "allow_write": bool(allow_write)})},
        )
        await session.commit()
    return await status(principal.workspace.id)


async def close_grant(principal: Any) -> dict:
    _require_owner(principal)
    async with async_session_factory() as session:
        await session.execute(
            text("UPDATE support_access_grants SET revoked_at = NOW(), revoked_by = :u "
                 "WHERE workspace_id = CAST(:w AS uuid) AND revoked_at IS NULL "
                 "AND expires_at > NOW()"),
            {"w": str(principal.workspace.id), "u": principal.user_id},
        )
        await session.commit()
    return await status(principal.workspace.id)


async def make_private(principal: Any) -> dict:
    """An owner takes an operator-created workspace private (one way)."""
    _require_owner(principal)
    async with async_session_factory() as session:
        await session.execute(
            text("UPDATE workspaces SET operator_managed = FALSE WHERE id = CAST(:w AS uuid)"),
            {"w": str(principal.workspace.id)},
        )
        await session.execute(
            text("INSERT INTO access_audit (workspace_id, action, details) "
                 "VALUES (CAST(:w AS uuid), 'operator.made_private', CAST(:d AS jsonb))"),
            {"w": str(principal.workspace.id), "d": json.dumps({"by": str(principal.user_id)})},
        )
        await session.commit()
    return await status(principal.workspace.id)
