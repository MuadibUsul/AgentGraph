"""Collaborator directory: who the agents are (PRD AG-ID-06).

Identity comes from the token tree: an entry binds tokens and the signatures
they write under. Tokens issued with ``issue_child_token`` are registered at
birth; older tokens call ``register_agent`` once. Under API version
``2026-11`` responses show writers as ``agent:<agent_id>``,
``human:<user_id>``, ``token:<token_id>`` (unregistered) or
``legacy:<label>``, with the stored label in ``updated_by_label``.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.actors import set_map
from app.auth import AuthKind, Principal
from app.database import async_session_factory
from app.errors import ApiError, Forbidden, NotFound
from app.models import Workspace
from app.services.common import append_event
from app.versioning import LATEST_VERSION, at_least
from app.warnings_context import add_warning

AGENT_ID = re.compile(r"^[a-z][a-z0-9-]{2,39}$")
STATUSES = ("active", "idle", "offline", "retired")
OFFLINE_AFTER = timedelta(hours=24)
SEEN_THROTTLE = timedelta(minutes=1)
# Fields each kind of caller may change (AG-ID-06 point 1).
AGENT_FIELDS = frozenset({"display_name", "capabilities", "description", "status"})
HUMAN_FIELDS = frozenset({"display_name", "role", "capabilities", "description", "status",
                          "owner_human", "trust_level", "agent_id"})

def _now() -> datetime:
    return datetime.now(timezone.utc)


def entry_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "principal_id": row["principal_id"],
        "agent_id": row["agent_id"],
        "display_name": row["display_name"],
        "role": row["role"],
        "capabilities": list(row["capabilities"] or []),
        "description": row["description"],
        "owner_human": str(row["owner_human"]) if row["owner_human"] else None,
        "status": row["status"],
        "status_note": row["status_note"],
        "trust_level": row["trust_level"],
        "token_ids": list(row["token_ids"] or []),
        "registered_by": row["registered_by"],
        "version": row["version"],
        "last_seen_at": row["last_seen_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def _owner(session: AsyncSession, workspace_id: Any) -> Any:
    return await session.scalar(
        text("SELECT id FROM human_users WHERE workspace_id = :ws AND role = 'OWNER' "
             "AND is_active ORDER BY created_at LIMIT 1"),
        {"ws": workspace_id},
    )


async def _taken(session: AsyncSession, tenant_id: str, agent_id: str) -> bool:
    return bool(await session.scalar(
        text("SELECT 1 FROM agent_directory WHERE tenant_id = :t AND agent_id = :a "
             "UNION ALL SELECT 1 FROM agent_aliases WHERE tenant_id = :t AND alias = :a "
             "LIMIT 1"),
        {"t": tenant_id, "a": agent_id},
    ))


async def _by_token(session: AsyncSession, token_cid: str | None) -> dict[str, Any] | None:
    if not token_cid:
        return None
    row = (
        await session.execute(
            text("SELECT * FROM agent_directory WHERE :token = ANY(token_ids) LIMIT 1"),
            {"token": token_cid},
        )
    ).mappings().one_or_none()
    return dict(row) if row else None


async def _resolve(session: AsyncSession, workspace: Workspace, ref: str) -> dict[str, Any]:
    ref = ref.removeprefix("agent:")
    row = (
        await session.execute(
            text("SELECT d.* FROM agent_directory d WHERE d.tenant_id = :t AND "
                 "(d.agent_id = :r OR d.principal_id = :r OR d.principal_id = "
                 "(SELECT principal_id FROM agent_aliases WHERE tenant_id = :t AND alias = :r))"),
            {"t": workspace.tenant_id, "r": ref},
        )
    ).mappings().one_or_none()
    if row is None:
        raise NotFound("agent", f"agent '{ref}' is not in the directory")
    return dict(row)


async def _announce(session: AsyncSession, workspace_id: Any, event_type: str,
                    payload: dict[str, Any]) -> None:
    humans = (
        await session.execute(
            text("SELECT id FROM human_users WHERE workspace_id = :ws AND is_active"),
            {"ws": workspace_id},
        )
    ).scalars().all()
    await append_event(session, workspace_id=workspace_id, agent="directory",
                       event_type=event_type,
                       payload={**payload, "targets": [f"human:{item}" for item in humans]})


# ---------------------------------------------------------- registration


async def register_issued(session: AsyncSession, workspace: Workspace, token: Any,
                          issuer: Principal) -> None:
    """Birth registration for a token from issue_child_token (AG-ID-02 point 6)."""
    profile = token.agent_profile or {}
    owner = None
    trust = "internal"
    if issuer.auth_kind is AuthKind.HUMAN:
        owner = issuer.user_id
    elif issuer.auth_kind is AuthKind.AGENT:
        parent = await _by_token(session, issuer.token_cid)
        if parent is not None:
            owner, trust = parent["owner_human"], parent["trust_level"]
    if owner is None:
        owner = await _owner(session, workspace.id)
    await session.execute(
        text("INSERT INTO agent_directory (workspace_id, agent_id, display_name, role, "
             "capabilities, description, owner_human, trust_level, token_ids, signatures, "
             "registered_by) VALUES (:ws, :agent, :name, :role, CAST(:caps AS text[]), :desc, "
             ":owner, :trust, ARRAY[:token], ARRAY[:sig], :by)"),
        {"ws": workspace.id, "agent": profile.get("agent_id") or token.agent_signature,
         "name": profile.get("display_name") or token.agent_signature,
         "role": profile.get("role"), "caps": profile.get("capabilities") or [],
         "desc": profile.get("description"), "owner": owner, "trust": trust,
         "token": token.canonical_id, "sig": token.agent_signature,
         "by": f"human:{issuer.user_id}" if issuer.user_id else (issuer.token_cid or "admin")},
    )
    await _announce(session, workspace.id, "AGENT_REGISTERED",
                    {"agent_id": profile.get("agent_id"), "token_id": token.canonical_id})


async def register_agent(session: AsyncSession, principal: Principal,
                         data: dict[str, Any]) -> dict[str, Any]:
    if principal.auth_kind is not AuthKind.AGENT:
        raise Forbidden("register_agent is called by the agent's own token")
    existing = await _by_token(session, principal.token_cid)
    if existing is not None:
        raise ApiError(409, "already_registered", "this token is already registered",
                       details={"agent_id": existing["agent_id"]})
    agent_id = (data.get("agent_id") or "").strip()
    if not AGENT_ID.fullmatch(agent_id):
        raise ApiError(422, "validation_error", "agent_id must match ^[a-z][a-z0-9-]{2,39}$",
                       details={"field": "agent_id"})
    workspace = principal.workspace
    if await _taken(session, workspace.tenant_id, agent_id):
        raise ApiError(409, "agent_id_taken", "this agent_id is already registered",
                       details={"agent_id": agent_id})
    issued_by_human = await session.scalar(
        text("SELECT issued_by_human FROM agent_tokens WHERE id = :id"),
        {"id": principal.token_id},
    )
    owner = issued_by_human or await _owner(session, workspace.id)
    row = (
        await session.execute(
            text("INSERT INTO agent_directory (workspace_id, agent_id, display_name, role, "
                 "capabilities, description, owner_human, token_ids, signatures, registered_by, "
                 "last_seen_at) VALUES (:ws, :agent, :name, :role, CAST(:caps AS text[]), :desc, "
                 ":owner, ARRAY[:token], ARRAY[:sig], :by, NOW()) RETURNING *"),
            {"ws": workspace.id, "agent": agent_id,
             "name": (data.get("display_name") or agent_id)[:64],
             "role": (data.get("role") or None) and data["role"][:32],
             "caps": [str(item)[:64] for item in (data.get("capabilities") or [])][:20],
             "desc": (data.get("description") or None) and data["description"][:500],
             "owner": owner, "token": principal.token_cid, "sig": principal.agent_signature,
             "by": principal.token_cid},
        )
    ).mappings().one()
    await _announce(session, workspace.id, "AGENT_REGISTERED",
                    {"agent_id": agent_id, "token_id": principal.token_cid})
    await session.commit()
    return entry_out(dict(row))


async def get_agent(session: AsyncSession, principal: Principal, ref: str) -> dict[str, Any]:
    return entry_out(await _resolve(session, principal.workspace, ref))


async def me(session: AsyncSession, principal: Principal) -> dict[str, Any] | None:
    row = await _by_token(session, principal.token_cid)
    return entry_out(row) if row else None


def _cursor(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(value.encode()).decode())["after"]
    except Exception as exc:  # noqa: BLE001 - any malformed cursor
        raise ApiError(400, "cursor_invalid", "the cursor is not valid",
                       details={"field": "cursor"}) from exc


async def list_agents(session: AsyncSession, principal: Principal, *, role: str | None = None,
                      capability: str | None = None, status: str | None = None,
                      cursor: str | None = None, limit: int = 100) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    where = ["workspace_id = :ws"]
    params: dict[str, Any] = {"ws": principal.workspace.id, "limit": limit + 1}
    if role:
        where.append("role = :role")
        params["role"] = role
    if capability:
        where.append(":cap = ANY(capabilities)")
        params["cap"] = capability
    if status:
        where.append("status = :status")
        params["status"] = status
    after = _cursor(cursor)
    if after:
        where.append("agent_id > :after")
        params["after"] = after
    rows = (
        await session.execute(
            text(f"SELECT * FROM agent_directory WHERE {' AND '.join(where)} "
                 "ORDER BY agent_id LIMIT :limit"),
            params,
        )
    ).mappings().all()
    items = [entry_out(dict(row)) for row in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = base64.urlsafe_b64encode(
            json.dumps({"after": items[-1]["agent_id"]}).encode()).decode()
    return {"items": items, "next_cursor": next_cursor}


# --------------------------------------------------------------- changes


async def update_agent(session: AsyncSession, principal: Principal, ref: str,
                       fields: dict[str, Any], expected_version: int | None) -> dict[str, Any]:
    row = await _resolve(session, principal.workspace, ref)
    own = principal.token_cid in (row["token_ids"] or [])
    if principal.is_human:
        writable = HUMAN_FIELDS
    elif own:
        writable = AGENT_FIELDS
    else:
        raise Forbidden("an agent may only change its own directory entry")
    for field in fields:
        if field not in writable:
            raise ApiError(403, "field_not_writable", f"'{field}' cannot be changed by you",
                           details={"field": field, "writable": sorted(writable)})
    if expected_version is not None and expected_version != row["version"]:
        raise ApiError(409, "version_conflict", "the entry changed since you read it",
                       details={"current_version": row["version"]})
    if "status" in fields:
        allowed = STATUSES if principal.is_human else ("active", "idle")
        if fields["status"] not in allowed:
            raise ApiError(403, "field_not_writable", "status value not allowed for you",
                           details={"field": "status", "allowed": list(allowed)})
    if "trust_level" in fields and fields["trust_level"] not in ("internal", "external"):
        raise ApiError(422, "validation_error", "trust_level is internal or external",
                       details={"field": "trust_level"})
    if "agent_id" in fields and fields["agent_id"] != row["agent_id"]:
        new_id = fields["agent_id"]
        if not AGENT_ID.fullmatch(new_id or ""):
            raise ApiError(422, "validation_error", "agent_id must match ^[a-z][a-z0-9-]{2,39}$",
                           details={"field": "agent_id"})
        if await _taken(session, principal.workspace.tenant_id, new_id):
            raise ApiError(409, "agent_id_taken", "this agent_id is already registered",
                           details={"agent_id": new_id})
        await session.execute(
            text("INSERT INTO agent_aliases (workspace_id, alias, principal_id) "
                 "VALUES (:ws, :alias, :p)"),
            {"ws": principal.workspace.id, "alias": row["agent_id"], "p": row["principal_id"]},
        )
    sets, params = [], {"p": row["principal_id"]}
    for key, value in fields.items():
        if key == "capabilities":
            sets.append("capabilities = CAST(:capabilities AS text[])")
            value = [str(item)[:64] for item in value or []][:20]
        else:
            sets.append(f"{key} = :{key}")
        params[key] = value
    if sets:
        updated = (
            await session.execute(
                text(f"UPDATE agent_directory SET {', '.join(sets)}, version = version + 1, "
                     "updated_at = NOW() WHERE principal_id = :p RETURNING *"),
                params,
            )
        ).mappings().one()
        if "status" in fields and fields["status"] != row["status"]:
            await _status_event(session, dict(updated), row["status"])
        await session.commit()
        return entry_out(dict(updated))
    return entry_out(row)


async def _status_event(session: AsyncSession, row: dict[str, Any], before: str) -> None:
    await append_event(session, workspace_id=row["workspace_id"], agent="directory",
                       event_type="AGENT_STATUS_CHANGED",
                       payload={"agent_id": row["agent_id"], "status": row["status"],
                                "previous": before, "note": row.get("status_note"),
                                "targets": [f"agent:{row['agent_id']}"]})


async def set_status(session: AsyncSession, principal: Principal, status: str,
                     note: str | None) -> dict[str, Any]:
    row = await _by_token(session, principal.token_cid)
    if row is None:
        raise ApiError(409, "invalid_transition", "register this token first",
                       details={"register_with": "register_agent"})
    if status not in ("active", "idle"):
        raise ApiError(403, "field_not_writable", "agents set themselves active or idle",
                       details={"field": "status", "allowed": ["active", "idle"]})
    updated = (
        await session.execute(
            text("UPDATE agent_directory SET status = :s, status_note = :n, "
                 "version = version + 1, updated_at = NOW(), last_seen_at = NOW() "
                 "WHERE principal_id = :p RETURNING *"),
            {"s": status, "n": (note or None) and note[:200], "p": row["principal_id"]},
        )
    ).mappings().one()
    await _status_event(session, dict(updated), row["status"])
    await session.commit()
    return entry_out(dict(updated))


async def merge_legacy_identity(session: AsyncSession, principal: Principal, legacy: str,
                                agent_ref: str) -> dict[str, Any]:
    """Claim a pre-directory updated_by label for an agent (humans only)."""
    if not principal.is_human:
        raise Forbidden("only a human can merge a legacy identity")
    legacy = legacy.removeprefix("legacy:").strip()
    if not legacy:
        raise ApiError(422, "validation_error", "name the legacy label",
                       details={"field": "legacy"})
    row = await _resolve(session, principal.workspace, agent_ref)
    documents = int(await session.scalar(
        text("SELECT count(*) FROM shared_documents WHERE workspace_id = :ws "
             "AND (updated_by = :l OR created_by = :l)"),
        {"ws": principal.workspace.id, "l": legacy},
    ) or 0)
    await session.execute(
        text("UPDATE agent_directory SET legacy_names = ARRAY(SELECT DISTINCT unnest("
             "legacy_names || ARRAY[:l])), version = version + 1, updated_at = NOW() "
             "WHERE principal_id = :p"),
        {"l": legacy, "p": row["principal_id"]},
    )
    # The stored labels never change; the audit keeps the mapping and its scope.
    await session.execute(
        text("INSERT INTO access_audit (workspace_id, action, details) VALUES "
             "(:ws, 'directory.merge_legacy', CAST(:d AS jsonb))"),
        {"ws": principal.workspace.id,
         "d": json.dumps({"legacy": legacy, "agent_id": row["agent_id"],
                          "principal_id": row["principal_id"], "documents": documents,
                          "by": principal.agent_signature})},
    )
    await session.commit()
    return {"legacy": legacy, "agent_id": row["agent_id"], "documents": documents}


async def retire_if_unused(session: AsyncSession, workspace: Workspace,
                           signatures: list[str]) -> None:
    """A directory entry whose every token is revoked becomes retired (AG-ID-04 7)."""
    await session.execute(
        text("UPDATE agent_directory d SET status = 'retired', version = version + 1, "
             "updated_at = NOW() WHERE d.workspace_id = :ws AND d.status <> 'retired' "
             "AND d.signatures && CAST(:sigs AS text[]) AND NOT EXISTS (SELECT 1 FROM "
             "agent_tokens t WHERE t.canonical_id = ANY(d.token_ids) AND t.revoked_at IS NULL "
             "AND t.is_active)"),
        {"ws": workspace.id, "sigs": signatures},
    )


# ------------------------------------------------------------ per request


async def touch(principal: Principal, *, write: bool) -> None:
    """last_seen_at (throttled), back to active, and the not_registered warning."""
    if principal.auth_kind is not AuthKind.AGENT:
        return
    async with async_session_factory() as session:
        row = (
            await session.execute(
                text("UPDATE agent_directory SET last_seen_at = NOW(), "
                     "status = CASE WHEN status = 'offline' THEN 'active' ELSE status END "
                     "WHERE :token = ANY(token_ids) AND (last_seen_at IS NULL OR last_seen_at < "
                     ":before OR status = 'offline') RETURNING principal_id"),
                {"token": principal.token_cid, "before": _now() - SEEN_THROTTLE},
            )
        ).one_or_none()
        registered = row is not None or await session.scalar(
            text("SELECT 1 FROM agent_directory WHERE :token = ANY(token_ids)"),
            {"token": principal.token_cid},
        )
        await session.commit()
    if write and not registered:
        add_warning("not_registered",
                    "this token has no directory entry; call register_agent so writes show "
                    "who you are", register_with="register_agent")


async def sweep_offline() -> int:
    """Agents silent for 24 hours become offline (AG-ID-06 point 1)."""
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                text("UPDATE agent_directory SET status = 'offline', version = version + 1, "
                     "updated_at = NOW() WHERE status IN ('active', 'idle') AND last_seen_at < "
                     ":cutoff RETURNING *, 'active' AS previous"),
                {"cutoff": _now() - OFFLINE_AFTER},
            )
        ).mappings().all()
        for row in rows:
            await _status_event(session, dict(row), "active")
        await session.commit()
    return len(rows)


async def load_actor_map(principal: Principal) -> None:
    """Labels -> typed actor refs for this request (2026-11 responses only)."""
    if not at_least(LATEST_VERSION):
        set_map(None)
        return
    mapping: dict[str, str] = {}
    async with async_session_factory() as session:
        params = {"ws": principal.workspace.id}
        for label, token in (
            await session.execute(
                text("SELECT agent_signature, canonical_id FROM agent_tokens "
                     "WHERE workspace_id = :ws"), params)
        ).all():
            mapping[label] = f"token:{token}"
        for email, user_id in (
            await session.execute(
                text("SELECT email, id FROM human_users WHERE workspace_id = :ws"), params)
        ).all():
            mapping[email] = f"human:{user_id}"
        for agent_id, signatures, legacy in (
            await session.execute(
                text("SELECT agent_id, signatures, legacy_names FROM agent_directory "
                     "WHERE workspace_id = :ws"), params)
        ).all():
            for label in [*(signatures or []), *(legacy or [])]:
                mapping[label] = f"agent:{agent_id}"
    set_map(mapping)
