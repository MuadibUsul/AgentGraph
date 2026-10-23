"""Human members and roles of a tenant node (PRD AG-ID-10).

Roles: owner > admin > member > guest. A membership is what lets a person use
a node's workspaces; ancestors' owners and admins act as owners and admins in
the whole subtree (``inherited``), members and guests do not inherit, and no
role inherits upwards. Guests only use workspaces shared with them (their
home workspace, until AG-ID-12 adds explicit shares). Every root keeps at
least one direct owner. Removing a member revokes the tokens they issued
unless they are transferred to another member.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthKind, Principal, generate_human_key, hash_api_key
from app.config import get_settings
from app.errors import ApiError, Forbidden, NotFound

settings = get_settings()

RANK = {"guest": 0, "member": 1, "admin": 2, "owner": 3}
ROLES = tuple(RANK)
# Account role (3.x) for a membership role, and back.
ACCOUNT_ROLE = {"owner": "OWNER", "admin": "ADMIN", "member": "REVIEWER", "guest": "VIEWER"}
GUEST_DAYS = 30
INVITE_DAYS = 7
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def effective_role(
    session: AsyncSession, user_id: Any, tenant_id: str
) -> dict[str, Any] | None:
    """The caller's role in a node: direct, or owner/admin inherited from an ancestor."""
    rows = (
        await session.execute(
            text("SELECT m.tenant_id, m.role FROM memberships m JOIN tenants t ON t.id = :t "
                 "WHERE m.principal_id = CAST(:u AS uuid) AND m.tenant_id = ANY(t.path) "
                 "AND (m.expires_at IS NULL OR m.expires_at > NOW())"),
            {"t": tenant_id, "u": str(user_id)},
        )
    ).all()
    best: dict[str, Any] | None = None
    for row_tenant, role in rows:
        inherited = row_tenant != tenant_id
        if inherited and role not in ("owner", "admin"):
            continue
        if best is None or RANK[role] > RANK[best["role"]] or (
                RANK[role] == RANK[best["role"]] and best["inherited"] and not inherited):
            best = {"role": role, "inherited": inherited, "via_tenant": row_tenant}
    return best


async def workspace_access(
    session: AsyncSession, user: Any, workspace_id: Any
) -> dict[str, Any] | None:
    """May this person use this workspace, and as what (None: no access)."""
    tenant_id = await session.scalar(
        text("SELECT tenant_id FROM workspaces WHERE id = CAST(:w AS uuid)"),
        {"w": str(workspace_id)},
    )
    if tenant_id is None:
        return None
    role = await effective_role(session, user.id, tenant_id)
    home = str(user.workspace_id) == str(workspace_id)
    if role is None:
        if not home:
            return None
        # Accounts from before memberships: the account role is the membership.
        legacy = {value: key for key, value in ACCOUNT_ROLE.items()}.get(user.role, "guest")
        return {"role": legacy, "inherited": False, "via_tenant": tenant_id}
    if role["role"] == "guest" and not home:
        return None  # guests only use workspaces shared with them
    return role


def _require_manager(principal: Principal) -> None:
    if principal.auth_kind is not AuthKind.HUMAN or principal.user_id is None:
        raise Forbidden("members are managed by signed-in owners and admins")


async def _caller_role(session: AsyncSession, principal: Principal, tenant_id: str) -> dict:
    _require_manager(principal)
    role = await effective_role(session, principal.user_id, tenant_id)
    if role is None or role["role"] not in ("owner", "admin"):
        raise Forbidden("only owners and admins of this node manage its members")
    return role


async def _node(session: AsyncSession, principal: Principal, tenant_ref: str) -> dict[str, Any]:
    tenant_id = principal.tenant_id if tenant_ref in ("current", "self") else tenant_ref
    row = (
        await session.execute(
            text("SELECT id, parent_tenant_id, root_tenant_id, path, status FROM tenants "
                 "WHERE id = :t"),
            {"t": tenant_id},
        )
    ).mappings().one_or_none()
    if row is None:
        raise NotFound("tenant", f"tenant '{tenant_id}' not found")
    return dict(row)


def member_out(row: dict[str, Any], *, inherited: bool = False) -> dict[str, Any]:
    return {
        "principal_id": str(row["principal_id"]),
        "email": row["email"],
        "display_name": row["display_name"],
        "role": row["role"],
        "inherited": inherited,
        "via_tenant": row["tenant_id"],
        "invited_by": row.get("invited_by"),
        "expires_at": row["expires_at"],
        "created_at": row["created_at"],
    }


async def list_members(session: AsyncSession, principal: Principal, tenant_ref: str, *,
                       include_inherited: bool = True) -> dict[str, Any]:
    node = await _node(session, principal, tenant_ref)
    if principal.auth_kind is AuthKind.HUMAN:
        role = await effective_role(session, principal.user_id, node["id"])
        if role is None or role["role"] == "guest":
            raise Forbidden("guests do not see the member directory")
    elif principal.auth_kind is not AuthKind.HUMAN_ADMIN:
        raise Forbidden("agent tokens cannot list members")
    rows = (
        await session.execute(
            text("SELECT m.tenant_id, m.principal_id, m.role, m.expires_at, m.created_at, "
                 "m.invited_by, u.email, u.display_name FROM memberships m JOIN human_users u "
                 "ON u.id = m.principal_id WHERE m.tenant_id = ANY(CAST(:path AS text[])) "
                 "AND (m.expires_at IS NULL OR m.expires_at > NOW()) ORDER BY u.email"),
            {"path": list(node["path"])},
        )
    ).mappings().all()
    direct = [member_out(dict(row)) for row in rows if row["tenant_id"] == node["id"]]
    seen = {item["principal_id"] for item in direct}
    items = list(direct)
    if include_inherited:
        for row in rows:
            if row["tenant_id"] == node["id"] or row["role"] not in ("owner", "admin"):
                continue
            if str(row["principal_id"]) in seen:
                continue
            items.append(member_out(dict(row), inherited=True))
            seen.add(str(row["principal_id"]))
    invitations = (
        await session.execute(
            text("SELECT email, role, expires_at, created_at FROM member_invitations "
                 "WHERE tenant_id = :t AND accepted_at IS NULL AND revoked_at IS NULL "
                 "AND expires_at > NOW() ORDER BY created_at"),
            {"t": node["id"]},
        )
    ).mappings().all()
    return {"tenant_id": node["id"], "items": items,
            "pending_invitations": [dict(row) for row in invitations]}


def _assignable(caller: dict, role: str) -> None:
    if RANK[role] > RANK[caller["role"]]:
        raise ApiError(403, "role_not_assignable", "you can only grant roles up to your own",
                       details={"your_role": caller["role"], "requested_role": role})


async def _audit(session: AsyncSession, tenant_id: str, action: str,
                 details: dict[str, Any]) -> None:
    workspace = await session.scalar(
        text("SELECT id FROM workspaces WHERE tenant_id = :t ORDER BY created_at LIMIT 1"),
        {"t": tenant_id},
    )
    if workspace is None:
        return
    await session.execute(
        text("INSERT INTO access_audit (workspace_id, action, details) "
             "VALUES (:w, :a, CAST(:d AS jsonb))"),
        {"w": workspace, "a": action, "d": json.dumps(details, default=str)},
    )


def _guest_expiry(role: str, expires_at: datetime | None) -> datetime | None:
    if role == "guest" and expires_at is None:
        return _now() + timedelta(days=GUEST_DAYS)
    return expires_at


async def invite_member(session: AsyncSession, principal: Principal, tenant_ref: str, *,
                        email: str, role: str, expires_at: datetime | None) -> dict[str, Any]:
    node = await _node(session, principal, tenant_ref)
    caller = await _caller_role(session, principal, node["id"])
    if role not in RANK:
        raise ApiError(422, "validation_error", "role is owner, admin, member or guest",
                       details={"field": "role", "allowed": list(ROLES)})
    _assignable(caller, role)
    if role == "guest":
        from app.services import tree

        if not await tree.policy_value(node["id"], "guests_allowed"):
            raise ApiError(403, "forbidden", "this organisation's policy allows no guests",
                           details={"policy": "guests_allowed"})
    email = email.strip()
    if not EMAIL.fullmatch(email):
        raise ApiError(422, "validation_error", "not an email address",
                       details={"field": "email"})
    expires_at = _guest_expiry(role, expires_at)
    user = (
        await session.execute(
            text("SELECT id, email, display_name FROM human_users WHERE lower(email) = "
                 "lower(:e)"),
            {"e": email},
        )
    ).mappings().one_or_none()
    by = f"human:{principal.user_id}"
    if user is not None:
        existing = await session.scalar(
            text("SELECT role FROM memberships WHERE tenant_id = :t AND principal_id = :u"),
            {"t": node["id"], "u": user["id"]},
        )
        if existing is not None:
            raise ApiError(409, "already_registered", "this person is already a member",
                           details={"role": existing})
        await session.execute(
            text("INSERT INTO memberships (tenant_id, principal_id, role, expires_at, "
                 "invited_by) VALUES (:t, :u, :r, :x, :by)"),
            {"t": node["id"], "u": user["id"], "r": role, "x": expires_at, "by": by},
        )
        await _audit(session, node["id"], "member.added",
                     {"principal_id": str(user["id"]), "role": role, "by": by})
        await session.commit()
        return {"status": "added", "tenant_id": node["id"], "principal_id": str(user["id"]),
                "role": role, "expires_at": expires_at}
    token = secrets.token_urlsafe(32)
    invitation = await session.scalar(
        text("INSERT INTO member_invitations (tenant_id, email, role, membership_expires_at, "
             "token_hash, invited_by, expires_at) VALUES (:t, :e, :r, :x, :h, :by, :until) "
             "RETURNING id"),
        {"t": node["id"], "e": email, "r": role, "x": expires_at,
         "h": hashlib.sha256(token.encode()).hexdigest(), "by": by,
         "until": _now() + timedelta(days=INVITE_DAYS)},
    )
    from app.services.mailer import queue_email

    await queue_email(
        session, to=email, subject="You are invited to an Veynod workspace",
        body=(f"You were invited as {role}. Accept within {INVITE_DAYS} days with "
              f"POST /v1/auth/accept-invite and this code:\n\n{token}\n"),
        ref=invitation,
    )
    await _audit(session, node["id"], "member.invited",
                 {"invitation_id": invitation, "role": role, "by": by})
    await session.commit()
    return {"status": "invited", "tenant_id": node["id"], "invitation_id": invitation,
            "role": role, "expires_at": expires_at}


async def _target(session: AsyncSession, tenant_id: str, principal_id: str) -> dict[str, Any]:
    row = (
        await session.execute(
            text("SELECT * FROM memberships WHERE tenant_id = :t AND principal_id = "
                 "CAST(:u AS uuid)"),
            {"t": tenant_id, "u": principal_id},
        )
    ).mappings().one_or_none()
    if row is None:
        raise NotFound("member", "no such direct member of this node")
    return dict(row)


async def _check_last_owner(session: AsyncSession, node: dict[str, Any],
                            target: dict[str, Any]) -> None:
    if target["role"] != "owner" or node["parent_tenant_id"] is not None:
        return
    owners = await session.scalar(
        text("SELECT count(*) FROM memberships WHERE tenant_id = :t AND role = 'owner' "
             "AND (expires_at IS NULL OR expires_at > NOW())"),
        {"t": node["id"]},
    )
    if int(owners or 0) <= 1:
        raise ApiError(409, "last_owner_required", "a root node keeps at least one owner",
                       details={"tenant_id": node["id"]})


async def set_member_role(session: AsyncSession, principal: Principal, tenant_ref: str,
                          member_id: str, *, role: str,
                          expires_at: datetime | None) -> dict[str, Any]:
    node = await _node(session, principal, tenant_ref)
    caller = await _caller_role(session, principal, node["id"])
    if role not in RANK:
        raise ApiError(422, "validation_error", "role is owner, admin, member or guest",
                       details={"field": "role", "allowed": list(ROLES)})
    target = await _target(session, node["id"], member_id)
    _assignable(caller, role)
    _assignable(caller, target["role"])
    if RANK[role] < RANK[target["role"]]:
        await _check_last_owner(session, node, target)
    await session.execute(
        text("UPDATE memberships SET role = :r, expires_at = :x, updated_at = NOW() "
             "WHERE id = :id"),
        {"r": role, "x": _guest_expiry(role, expires_at), "id": target["id"]},
    )
    await _audit(session, node["id"], "member.role_changed",
                 {"principal_id": member_id, "from": target["role"], "to": role,
                  "by": f"human:{principal.user_id}"})
    await session.commit()
    return {"tenant_id": node["id"], "principal_id": member_id, "role": role}


async def remove_member(session: AsyncSession, principal: Principal, tenant_ref: str,
                        member_id: str, *, transfer_tokens_to: str | None) -> dict[str, Any]:
    node = await _node(session, principal, tenant_ref)
    caller = await _caller_role(session, principal, node["id"])
    target = await _target(session, node["id"], member_id)
    _assignable(caller, target["role"])
    await _check_last_owner(session, node, target)
    tokens = (
        await session.execute(
            text("SELECT t.canonical_id FROM agent_tokens t JOIN workspaces w ON "
                 "w.id = t.workspace_id WHERE w.tenant_id = :t AND t.issued_by_human = "
                 "CAST(:u AS uuid) AND t.revoked_at IS NULL"),
            {"t": node["id"], "u": member_id},
        )
    ).scalars().all()
    transferred: list[str] = []
    revoked: list[str] = []
    if transfer_tokens_to:
        successor = await _target(session, node["id"], transfer_tokens_to)
        await session.execute(
            text("UPDATE agent_tokens t SET issued_by_human = CAST(:s AS uuid) FROM workspaces w "
                 "WHERE w.id = t.workspace_id AND w.tenant_id = :t "
                 "AND t.issued_by_human = CAST(:u AS uuid)"),
            {"s": str(successor["principal_id"]), "t": node["id"], "u": member_id},
        )
        transferred = list(tokens)
    else:
        from app.services.token_tree import revoke_subtrees

        revoked = await revoke_subtrees(session, node["id"], tokens,
                                        reason="issuer removed from the organisation")
    await session.execute(text("DELETE FROM memberships WHERE id = :id"), {"id": target["id"]})
    await session.execute(
        text("UPDATE human_sessions s SET revoked_at = NOW() FROM workspaces w "
             "WHERE w.id = s.workspace_id AND w.tenant_id = :t AND s.user_id = CAST(:u AS uuid) "
             "AND s.revoked_at IS NULL"),
        {"t": node["id"], "u": member_id},
    )
    spaces = await _hand_over_personal_spaces(session, node["id"], member_id)
    await _audit(session, node["id"], "member.removed",
                 {"principal_id": member_id, "role": target["role"],
                  "tokens_revoked": len(revoked), "tokens_transferred": len(transferred),
                  "transfer_tokens_to": transfer_tokens_to,
                  "personal_spaces_handed_over": spaces,
                  "by": f"human:{principal.user_id}"})
    await session.commit()
    return {"tenant_id": node["id"], "principal_id": member_id, "revoked_tokens": revoked,
            "transferred_tokens": transferred, "personal_spaces_handed_over": spaces}


async def _hand_over_personal_spaces(session: AsyncSession, node_id: str,
                                     member_id: str) -> list[str]:
    """AG-ID-13 point 5: a leaving member's managed personal spaces go to the
    owners of their parent node; the member loses access to them."""
    spaces = (await session.execute(
        text("SELECT t.id, t.parent_tenant_id FROM tenants t JOIN memberships m ON "
             "m.tenant_id = t.id WHERE t.tenant_type = 'personal' AND :n = ANY(t.path) AND "
             "t.parent_tenant_id IS NOT NULL AND m.principal_id = CAST(:u AS uuid)"),
        {"n": node_id, "u": member_id})).all()
    for space_id, parent_id in spaces:
        await session.execute(
            text("INSERT INTO memberships (tenant_id, principal_id, role, invited_by) "
                 "SELECT :space, m.principal_id, 'owner', 'handover' FROM memberships m "
                 "JOIN tenants p ON p.id = :parent WHERE m.tenant_id = ANY(p.path) AND "
                 "m.role = 'owner' AND m.principal_id <> CAST(:u AS uuid) "
                 "ON CONFLICT (tenant_id, principal_id) DO UPDATE SET role = 'owner'"),
            {"space": space_id, "parent": parent_id, "u": member_id})
        await session.execute(
            text("DELETE FROM memberships WHERE tenant_id = :space AND "
                 "principal_id = CAST(:u AS uuid)"), {"space": space_id, "u": member_id})
        await session.execute(
            text("UPDATE human_sessions s SET revoked_at = NOW() FROM workspaces w "
                 "WHERE w.id = s.workspace_id AND w.tenant_id = :t AND "
                 "s.user_id = CAST(:u AS uuid) AND s.revoked_at IS NULL"),
            {"t": space_id, "u": member_id})
    return [space_id for space_id, _ in spaces]


async def accept_invitation(session: AsyncSession, *, code: str, password: str,
                            display_name: str) -> dict[str, Any]:
    from app.services.accounts import hash_password

    row = (
        await session.execute(
            text("SELECT * FROM member_invitations WHERE token_hash = :h FOR UPDATE"),
            {"h": hashlib.sha256(code.encode()).hexdigest()},
        )
    ).mappings().one_or_none()
    if row is None or row["accepted_at"] or row["revoked_at"] or row["expires_at"] <= _now():
        raise NotFound("invitation", "the invitation is unknown, used or expired")
    workspace_id = await session.scalar(
        text("SELECT id FROM workspaces WHERE tenant_id = :t ORDER BY created_at LIMIT 1"),
        {"t": row["tenant_id"]},
    )
    if workspace_id is None:
        raise NotFound("workspace", "the inviting organisation has no workspace")
    user_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO human_users (id, workspace_id, email, password_hash, display_name, "
             "role) VALUES (:id, :w, :e, :p, :n, :r)"),
        {"id": user_id, "w": workspace_id, "e": row["email"], "p": hash_password(password),
         "n": display_name[:128], "r": ACCOUNT_ROLE[row["role"]]},
    )
    await session.execute(
        text("INSERT INTO memberships (tenant_id, principal_id, role, expires_at, invited_by) "
             "VALUES (:t, :u, :r, :x, :by)"),
        {"t": row["tenant_id"], "u": user_id, "r": row["role"],
         "x": row["membership_expires_at"], "by": row["invited_by"]},
    )
    await session.execute(
        text("UPDATE member_invitations SET accepted_at = NOW() WHERE id = :id"),
        {"id": row["id"]},
    )
    raw = generate_human_key()
    await session.execute(
        text("INSERT INTO human_sessions (id, user_id, workspace_id, token_hash, expires_at) "
             "VALUES (:id, :u, :w, :h, :x)"),
        {"id": uuid.uuid4(), "u": user_id, "w": workspace_id, "h": hash_api_key(raw),
         "x": _now() + timedelta(hours=settings.human_session_hours)},
    )
    await _audit(session, row["tenant_id"], "member.joined",
                 {"principal_id": str(user_id), "role": row["role"]})
    await session.commit()
    return {"token": raw, "user_id": str(user_id), "workspace_id": str(workspace_id),
            "tenant_id": row["tenant_id"], "role": row["role"]}


async def switch_workspace(session: AsyncSession, principal: Principal,
                           workspace_id: uuid.UUID) -> dict[str, Any]:
    """A session for another workspace the person may use (or 404, audited)."""
    from app.models import HumanUser

    if principal.auth_kind is not AuthKind.HUMAN or principal.user_id is None:
        raise Forbidden("only signed-in people switch workspaces")
    user = await session.get(HumanUser, principal.user_id)
    access = await workspace_access(session, user, workspace_id)
    if access is None:
        exists = await session.scalar(
            text("SELECT 1 FROM workspaces WHERE id = :w"), {"w": workspace_id})
        if exists:
            await session.execute(
                text("INSERT INTO access_audit (workspace_id, action, details) "
                     "VALUES (:w, 'access.denied', CAST(:d AS jsonb))"),
                {"w": workspace_id,
                 "d": json.dumps({"principal_id": str(user.id), "reason": "no_membership"})},
            )
            await session.commit()
        raise NotFound("workspace", f"workspace '{workspace_id}' not found")
    raw = generate_human_key()
    await session.execute(
        text("INSERT INTO human_sessions (id, user_id, workspace_id, token_hash, expires_at) "
             "VALUES (:id, :u, :w, :h, :x)"),
        {"id": uuid.uuid4(), "u": user.id, "w": workspace_id, "h": hash_api_key(raw),
         "x": _now() + timedelta(hours=settings.human_session_hours)},
    )
    await session.commit()
    return {"token": raw, "workspace_id": str(workspace_id), **access}
