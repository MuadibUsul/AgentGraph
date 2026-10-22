"""The token tree: root token, issuance, revocation, renewal (PRD AG-ID-01/02/04/05).

* The root token (``kind=root``) only manages tokens. Every use emits
  ``token.root_used`` to the workspace's humans, so a leaked root is noticed.
* ``issue_child_token`` only ever narrows: scopes, read and write namespaces,
  expiry and budget are subsets of the issuer's effective grant (the
  intersection along the whole chain, recomputed on every request). Depth,
  ``can_issue`` and tree quotas bound the shape of the tree.
* ``revoke_token`` always cascades to the whole subtree, releases leases and
  can taint everything the subtree wrote since a moment.
* ``renew_token`` needs an ancestor (or a human); a token never renews itself.

Depth is stored as root 0, branch 1, child 2...; the API reports the PRD
numbering (branch 0, child >= 1) as ``depth``.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    PERMISSIONS,
    SCOPE_PERMISSIONS,
    SCOPES,
    AuthKind,
    Principal,
    RoleScope,
    effective_scopes,
    generate_agent_key,
    hash_api_key,
    meet,
)
from app.config import get_settings
from app.database import async_session_factory
from app.errors import ApiError, Forbidden, NotFound
from app.models import AgentRole, AgentToken, HumanUser, SharedDocument, Workspace
from app.services.common import append_event
from app.warnings_context import add_warning

settings = get_settings()

ROOT_KEY_PREFIX = "agr_"
CHILD_TTL_DEFAULT = 3_600
CHILD_TTL_MAX = 86_400
BRANCH_TTL_DEFAULT = 30 * 86_400
BRANCH_TTL_MAX = 90 * 86_400
EXPIRING_SECONDS = 600
AGENT_ID = re.compile(r"^[a-z][a-z0-9-]{2,39}$")
# What a root token may call (AG-ID-01 point 1).
ROOT_PATH_PREFIXES = ("/v1/tokens",)
ROOT_TOOLS = frozenset({"issue_child_token", "revoke_token", "renew_token", "get_token",
                        "get_lineage", "list_token_tree"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def generate_root_key() -> str:
    import secrets

    return f"{ROOT_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def prd_depth(stored: int) -> int:
    """Branch 0, child >= 1 (root reports -1)."""
    return stored - 1


# ----------------------------------------------------------- per request


async def after_authentication(principal: Principal, *, path: str | None = None,
                               tool: str | None = None) -> None:
    """Root restrictions and notices, expiry warnings; called once per request."""
    if principal.auth_kind is AuthKind.ROOT:
        allowed = (tool in ROOT_TOOLS) if tool is not None else any(
            (path or "").startswith(prefix) for prefix in ROOT_PATH_PREFIXES)
        if not allowed:
            raise ApiError(
                403, "root_token_data_access_denied",
                "the root token only manages tokens; issue a branch token for data access",
                details={"allowed": sorted(ROOT_TOOLS) if tool is not None
                         else list(ROOT_PATH_PREFIXES)},
            )
        await _notify_humans(principal.workspace, "TOKEN_ROOT_USED",
                             {"token_id": principal.token_cid, "path": path or f"mcp:{tool}"})
        return
    if principal.auth_kind is AuthKind.AGENT and principal.expires_at is not None:
        remaining = (principal.expires_at - _now()).total_seconds()
        if remaining < EXPIRING_SECONDS:
            add_warning("token_expiring", "this token expires soon; ask its parent to renew it",
                        expires_in_s=max(0, int(remaining)),
                        renew_with=principal.parent_token_id)
            await _notify_expiring(principal)


async def _humans(session: AsyncSession, workspace_id: Any) -> list[str]:
    rows = (
        await session.execute(
            select(HumanUser.id).where(HumanUser.workspace_id == workspace_id,
                                       HumanUser.is_active.is_(True))
        )
    ).scalars().all()
    return [f"human:{row}" for row in rows]


async def _notify_humans(workspace: Workspace, event_type: str, payload: dict[str, Any],
                         extra_targets: list[str] | None = None) -> None:
    async with async_session_factory() as session:
        targets = await _humans(session, workspace.id) + list(extra_targets or [])
        await append_event(session, workspace_id=workspace.id, agent="tokens",
                           event_type=event_type,
                           payload={**payload, "targets": sorted(set(targets))})
        await session.commit()


async def _notify_expiring(principal: Principal) -> None:
    async with async_session_factory() as session:
        claimed = await session.execute(
            text("UPDATE agent_tokens SET expiring_notified_at = NOW() WHERE id = :id "
                 "AND expiring_notified_at IS NULL RETURNING parent_token_id"),
            {"id": principal.token_id},
        )
        row = claimed.one_or_none()
        if row is None:
            await session.rollback()
            return
        parent = await session.scalar(
            select(AgentToken.agent_signature).where(AgentToken.canonical_id == row[0])
        )
        targets = [f"agent:{parent}"] if parent else await _humans(session, principal.workspace.id)
        await append_event(session, workspace_id=principal.workspace.id, agent="tokens",
                           event_type="TOKEN_EXPIRING",
                           payload={"token_id": principal.token_cid,
                                    "expires_at": principal.expires_at.isoformat(),
                                    "targets": targets})
        await session.commit()


# ----------------------------------------------------------------- views


def token_scopes(token: AgentToken) -> list[str]:
    from app.auth import scopes_for_role

    base = [scope for scope in scopes_for_role(RoleScope(token.role_scope.value))
            if scope != "token:issue"]
    if token.scopes is not None:
        base = [scope for scope in base if scope in token.scopes]
    if token.can_issue:
        base.append("token:issue")
    return base


def token_kind(token: AgentToken) -> str:
    return "branch" if token.depth <= 1 else "child"


# ----------------------------------------------------------------- issue


def _check_subset(requested: list[str], allowed: list[str]) -> list[str]:
    return sorted(set(requested) - set(allowed))


def _namespaces_extra(requested: list[str], allowed: list[str]) -> list[str]:
    if "*" in allowed:
        return []
    if "*" in requested:
        return ["*"]
    return sorted(set(requested) - set(allowed))


def _role_for(scopes: list[str], parent: Principal) -> AgentRole:
    if parent.auth_kind is AuthKind.AGENT:
        return AgentRole(parent.role.value)
    needed: set[str] = set()
    for scope in scopes:
        needed |= SCOPE_PERMISSIONS[scope]
    needed -= {"token:issue", "export", "webhook:manage", "doc:archive"}
    for role in (AgentRole.SCOUT, AgentRole.ANALYST, AgentRole.AUDITOR, AgentRole.EXECUTOR):
        if needed <= PERMISSIONS[RoleScope(role.value)]:
            return role
    return AgentRole.EXECUTOR


async def _active_count(session: AsyncSession, workspace_id: Any, *,
                        under: str | None = None) -> int:
    query = select(func.count()).select_from(AgentToken).where(
        AgentToken.workspace_id == workspace_id,
        AgentToken.is_active.is_(True),
        AgentToken.revoked_at.is_(None),
        (AgentToken.expires_at.is_(None)) | (AgentToken.expires_at > func.now()),
    )
    if under is not None:
        query = query.where(AgentToken.lineage.any(under))
    return int(await session.scalar(query) or 0)


def _issuer_kind_ok(principal: Principal) -> None:
    if principal.auth_kind is AuthKind.HUMAN and principal.human_role in ("OWNER", "REVIEWER"):
        return
    if principal.auth_kind in (AuthKind.ROOT, AuthKind.WORKSPACE, AuthKind.HUMAN_ADMIN):
        return
    if principal.auth_kind is AuthKind.AGENT and principal.can_issue:
        return
    raise ApiError(403, "issue_not_permitted", "this credential cannot issue tokens",
                   details={"needs": "token:issue"})


def narrow(parent: dict[str, Any], request: dict[str, Any], *, child_depth: int,
           max_depth: int, now: datetime) -> dict[str, Any]:
    """The child's grant, or ApiError when the request would widen anything.

    ``parent`` = {scopes, read, write, expires_at}; pure, so the subset rule
    can be property-tested without a database (AG-ID-02 acceptance 8).
    """
    scopes = list(dict.fromkeys(request.get("scopes") or []))
    unknown = sorted(set(scopes) - set(SCOPES))
    if unknown:
        raise ApiError(422, "validation_error", "unknown scopes",
                       details={"field": "scopes", "unknown": unknown, "allowed": list(SCOPES)})
    can_issue = bool(request.get("can_issue")) or "token:issue" in scopes
    if can_issue and "token:issue" not in scopes:
        scopes.append("token:issue")
    if can_issue and not prd_depth(child_depth) < max_depth:
        raise ApiError(403, "depth_limit_exceeded",
                       "a token at this depth cannot be allowed to issue",
                       details={"max_depth": max_depth, "child_depth": prd_depth(child_depth)})
    parent_read, parent_write = list(parent["read"]), list(parent["write"])
    namespaces = request.get("namespaces") or {}
    read = list(namespaces.get("read") or parent_read)
    write = list(namespaces.get("write") if namespaces.get("write") is not None
                 else parent_write)
    extra_scopes = _check_subset(scopes, list(parent["scopes"]))
    extra_read = _namespaces_extra(read, parent_read)
    extra_write = _namespaces_extra(write, parent_write)
    if extra_scopes or extra_read or extra_write:
        details: dict[str, Any] = {"requested_extra": extra_scopes}
        if extra_read or extra_write:
            details["namespaces_extra"] = {
                key: value for key, value in (("read", extra_read), ("write", extra_write))
                if value
            }
        raise ApiError(403, "scope_escalation",
                       "a child token can only receive a subset of its issuer's grant",
                       details=details)
    # Writing needs reading: a write namespace outside the read set is dropped.
    if "*" not in read:
        write = meet(write, read)
    branch = child_depth == 1
    ttl = request.get("ttl_seconds") or (BRANCH_TTL_DEFAULT if branch else CHILD_TTL_DEFAULT)
    ceiling = BRANCH_TTL_MAX if branch else CHILD_TTL_MAX
    if ttl < 60 or ttl > ceiling:
        raise ApiError(422, "validation_error", "ttl_seconds is out of range",
                       details={"field": "ttl_seconds", "min": 60, "max": ceiling})
    expires_at = now + timedelta(seconds=int(ttl))
    if parent.get("expires_at") is not None and expires_at > parent["expires_at"]:
        if request.get("ttl_seconds"):
            raise ApiError(422, "ttl_exceeds_parent", "a child token cannot outlive its parent",
                           details={"max_expires_at": parent["expires_at"].isoformat()})
        # The default lifetime never outlives the parent: it is clamped.
        expires_at = parent["expires_at"]
    return {"scopes": sorted(set(scopes)), "read": read, "write": write,
            "can_issue": can_issue, "expires_at": expires_at}


async def issue_child_token(
    session: AsyncSession, principal: Principal, request: dict[str, Any]
) -> dict[str, Any]:
    if not settings.child_token_issuance:
        raise ApiError(403, "issue_not_permitted", "child token issuance is switched off",
                       details={"reason": "child_token_issuance_disabled"})
    workspace = await session.get(Workspace, principal.workspace.id)
    issued_by_human = principal.user_id if principal.auth_kind is AuthKind.HUMAN else None
    # Humans and the admin console issue on behalf of the root.
    via_root = principal.auth_kind in (AuthKind.ROOT, AuthKind.HUMAN, AuthKind.HUMAN_ADMIN)
    parent_id = workspace.root_token_id if via_root else principal.token_cid
    parent_lineage = [workspace.root_token_id] if via_root else list(principal.lineage or [])
    parent_depth = 0 if via_root else principal.token_depth
    child_depth = parent_depth + 1  # stored numbering
    max_depth = workspace.max_token_depth
    if prd_depth(child_depth) > max_depth:
        raise ApiError(403, "depth_limit_exceeded", "the token tree is at its maximum depth",
                       details={"max_depth": max_depth, "parent_depth": prd_depth(parent_depth)})
    _issuer_kind_ok(principal)
    if via_root:
        parent_grant = {"scopes": list(SCOPES), "read": ["*"], "write": ["*"],
                        "expires_at": None}
    else:
        parent_grant = {"scopes": effective_scopes(principal),
                        "read": list(principal.allowed_namespaces),
                        "write": list(principal.write_namespaces
                                      or principal.allowed_namespaces),
                        "expires_at": principal.expires_at}
    now = _now()
    grant = narrow(parent_grant, request, child_depth=child_depth, max_depth=max_depth,
                   now=now)
    scopes, read, write = grant["scopes"], grant["read"], grant["write"]
    can_issue, expires_at = grant["can_issue"], grant["expires_at"]
    branch = child_depth == 1

    agent = request.get("agent") or {}
    agent_id = (agent.get("agent_id") or "").strip()
    if not AGENT_ID.fullmatch(agent_id):
        raise ApiError(422, "validation_error",
                       "agent.agent_id must match ^[a-z][a-z0-9-]{2,39}$",
                       details={"field": "agent.agent_id"})
    purpose = (request.get("purpose") or "").strip()
    if not purpose or len(purpose) > 200:
        raise ApiError(422, "validation_error", "purpose is required (at most 200 characters)",
                       details={"field": "purpose"})
    taken = await session.scalar(
        select(func.count()).select_from(AgentToken).where(
            AgentToken.workspace_id == workspace.id, AgentToken.agent_signature == agent_id)
    )
    if not taken:
        taken = await session.scalar(
            text("SELECT count(*) FROM agent_tokens t JOIN workspaces w ON w.id = "
                 "t.workspace_id WHERE w.tenant_id = :tenant AND t.agent_signature = :agent"),
            {"tenant": workspace.tenant_id, "agent": agent_id},
        )
    if taken:
        raise ApiError(409, "agent_id_taken", "this agent_id is already registered",
                       details={"agent_id": agent_id})

    # Tree quotas count whole trees, not children of one parent (AG-ID-02 point 4).
    workspace_active = await _active_count(session, workspace.id)
    if workspace_active >= workspace.workspace_token_quota:
        raise ApiError(409, "tree_quota_exceeded", "the workspace has too many active tokens",
                       details={"scope": "workspace", "limit": workspace.workspace_token_quota,
                                "active": workspace_active})
    branch_id = None if branch else (parent_lineage[1] if len(parent_lineage) > 1 else None)
    if branch_id and branch_id != workspace.key_token_id:
        branch_active = await _active_count(session, workspace.id, under=branch_id)
        if branch_active >= workspace.branch_token_quota:
            raise ApiError(409, "tree_quota_exceeded", "this branch has too many active tokens",
                           details={"scope": "branch", "limit": workspace.branch_token_quota,
                                    "active": branch_active})
    max_descendants = int(request.get("max_descendants") or 0)
    if principal.auth_kind is AuthKind.AGENT:
        used = await _active_count(session, workspace.id, under=principal.token_cid) - 1
        remaining = principal.max_descendants - used
        if remaining <= 0:
            raise ApiError(409, "tree_quota_exceeded",
                           "this token may not issue more tokens",
                           details={"scope": "token", "limit": principal.max_descendants,
                                    "active": used})
        if max_descendants > remaining - 1:
            raise ApiError(403, "scope_escalation",
                           "max_descendants exceeds what the issuer has left",
                           details={"requested_extra": [], "max_descendants": remaining - 1})
    elif max_descendants > workspace.branch_token_quota - 1:
        raise ApiError(422, "validation_error", "max_descendants exceeds the branch quota",
                       details={"field": "max_descendants",
                                "max": workspace.branch_token_quota - 1})
    if not can_issue:
        max_descendants = 0

    budget = request.get("budget") or {}
    parent_limits = {}
    if principal.auth_kind is AuthKind.AGENT:
        parent_row = await session.get(AgentToken, principal.token_id)
        parent_limits = {"per_minute_limit": parent_row.per_minute_limit,
                         "monthly_token_budget": parent_row.monthly_token_budget}
    limits = {}
    for key in ("per_minute_limit", "monthly_token_budget"):
        wanted, ceiling_value = budget.get(key), parent_limits.get(key)
        if wanted is not None and ceiling_value is not None and wanted > ceiling_value:
            raise ApiError(403, "scope_escalation", "a budget cannot exceed the issuer's",
                           details={"requested_extra": [], "budget": {key: ceiling_value}})
        limits[key] = wanted if wanted is not None else ceiling_value

    role = AgentRole(request["role_scope"]) if request.get("role_scope") else _role_for(
        scopes, principal)
    if principal.auth_kind is AuthKind.AGENT and role.value != principal.role.value:
        raise ApiError(403, "scope_escalation", "a child token keeps its issuer's role",
                       details={"requested_extra": [], "role_scope": principal.role.value})
    raw_key = generate_agent_key()
    token = AgentToken(
        workspace_id=workspace.id,
        agent_signature=agent_id,
        api_key_hash=hash_api_key(raw_key),
        role_scope=role,
        allowed_namespaces=read,
        read_namespaces=read,
        write_namespaces=write,
        scopes=sorted(set(scopes)),
        can_issue=can_issue,
        max_descendants=max_descendants,
        expires_at=expires_at,
        parent_token_id=parent_id,
        root_token_id=workspace.root_token_id,
        depth=child_depth,
        purpose=purpose,
        issued_by_agent_id=principal.agent_signature,
        issued_by_token_id=principal.token_cid,
        issued_by_human=issued_by_human,
        per_minute_limit=limits.get("per_minute_limit"),
        monthly_token_budget=limits.get("monthly_token_budget"),
        agent_profile={
            "agent_id": agent_id,
            "display_name": (agent.get("display_name") or agent_id)[:64],
            "role": (agent.get("role") or "")[:32] or None,
            "capabilities": [str(item)[:64] for item in (agent.get("capabilities") or [])][:20],
            "description": (agent.get("description") or "")[:500] or None,
        },
    )
    session.add(token)
    await session.flush()
    token.lineage = [*parent_lineage, token.canonical_id]
    from app.services.lineage import snapshot_token

    await snapshot_token(session, workspace, token)
    await append_event(session, workspace_id=workspace.id, agent="tokens",
                       event_type="TOKEN_ISSUED",
                       payload={"token_id": token.canonical_id, "parent_token_id": parent_id,
                                "agent_id": agent_id, "purpose": purpose,
                                "targets": [f"agent:{principal.agent_signature}"]})
    from app.services import directory

    await directory.register_issued(session, workspace, token, principal)
    await session.commit()
    return {
        "token_id": token.canonical_id,
        "token": raw_key,
        "kind": token_kind(token),
        "depth": prd_depth(token.depth),
        "parent_token_id": parent_id,
        "root_token_id": workspace.root_token_id,
        "lineage": token.lineage,
        "scopes": token_scopes(token),
        "namespaces": {"read": read, "write": write},
        "expires_at": expires_at,
        "can_issue": can_issue,
        "max_descendants": max_descendants,
        "agent_id": agent_id,
        "role_scope": role.value,
    }


# ---------------------------------------------------------------- revoke


async def _target(session: AsyncSession, workspace: Workspace, token_id: str) -> AgentToken:
    if token_id in (workspace.root_token_id, workspace.key_token_id):
        raise ApiError(409, "invalid_transition",
                       "the root token and the workspace key are rotated, not revoked",
                       details={"rotate_with": "POST /v1/admin/workspaces/{id}/root-token:rotate"
                                if token_id == workspace.root_token_id
                                else "POST /v1/admin/workspaces/{id}/rotate-key"})
    token = await session.scalar(
        select(AgentToken).where(AgentToken.workspace_id == workspace.id,
                                 AgentToken.canonical_id == token_id)
    )
    if token is None:
        raise NotFound("token", f"token '{token_id}' not found")
    return token


def _may_manage(principal: Principal, token: AgentToken, *, allow_self: bool) -> bool:
    if principal.auth_kind in (AuthKind.ROOT, AuthKind.HUMAN_ADMIN, AuthKind.WORKSPACE):
        return True
    if principal.auth_kind is AuthKind.HUMAN:
        return principal.human_role == "OWNER"
    if principal.token_cid == token.canonical_id:
        return allow_self
    return principal.token_cid in (token.lineage or [])


async def revoke_token(
    session: AsyncSession, principal: Principal, token_id: str, *, reason: str,
    taint_writes_since: datetime | None = None,
) -> dict[str, Any]:
    if not reason or not reason.strip() or len(reason) > 500:
        raise ApiError(422, "validation_error", "a reason (at most 500 characters) is required",
                       details={"field": "reason"})
    workspace = await session.get(Workspace, principal.workspace.id)
    target = await _target(session, workspace, token_id)
    if not _may_manage(principal, target, allow_self=True):
        raise Forbidden("only the token itself, an ancestor or a human may revoke a token")
    now = _now()
    subtree = (
        await session.execute(
            select(AgentToken).where(AgentToken.workspace_id == workspace.id,
                                     AgentToken.lineage.any(target.canonical_id),
                                     AgentToken.revoked_at.is_(None))
            .order_by(AgentToken.depth)
        )
    ).scalars().all()
    if target not in subtree and target.revoked_at is None:
        subtree = [target, *subtree]
    from app.services.leases import revoke_holder

    released = 0
    for token in subtree:
        token.revoked_at = now
        token.is_active = False
        token.revoke_reason = reason.strip()
        token.revoked_ancestor = target.canonical_id
        await session.flush()
        released += len(await revoke_holder(session, workspace.id, token,
                                            reason="holder_revoked"))
    ids = [token.canonical_id for token in subtree]
    tainted = 0
    if taint_writes_since is not None and ids:
        tainted = await _taint_writes(session, workspace.id, ids, taint_writes_since)
    issuer = target.issued_by_agent_id
    await append_event(session, workspace_id=workspace.id, agent="tokens",
                       event_type="TOKEN_REVOKED",
                       payload={"token_id": target.canonical_id, "count": len(ids),
                                "reason": reason.strip(),
                                "targets": sorted(set(await _humans(session, workspace.id)
                                                      + ([f"agent:{issuer}"] if issuer else [])))})
    from app.services import directory

    await directory.retire_if_unused(session, workspace, [token.agent_signature
                                                          for token in subtree])
    await session.commit()
    return {"revoked": ids, "count": len(ids), "revoked_at": now,
            "released_leases": released, "tainted_documents": tainted}


async def _taint_writes(session: AsyncSession, workspace_id: Any, token_ids: list[str],
                        since: datetime) -> int:
    """Taint every document the subtree wrote since ``since``, then downstream."""
    from app.services.taint import mark_document_taint, propagate_from_document

    rows = (
        await session.execute(
            text("SELECT DISTINCT document_id, stamp->>'token_id' AS token FROM "
                 "document_versions WHERE workspace_id = :ws AND stamp->>'token_id' = "
                 "ANY(:ids) AND created_at >= :since"),
            {"ws": workspace_id, "ids": token_ids, "since": since},
        )
    ).all()
    sources: dict[Any, str] = {}
    for document_id, token in rows:
        sources.setdefault(document_id, token)
    before = set()
    for document_id, token in sources.items():
        document = await session.get(SharedDocument, document_id)
        if document is None:
            continue
        before.add(document.id)
        await mark_document_taint(session, document, tainted=True, actor="token_revocation",
                                  sources=[{"kind": "revoked_principal", "ref": token}])
    for document_id in list(before):
        document = await session.get(SharedDocument, document_id)
        await propagate_from_document(session, document)
    await session.flush()
    downstream = await session.scalar(
        text("SELECT count(DISTINCT e.target_id) FROM taint_edges e JOIN shared_documents d "
             "ON d.id = e.target_id WHERE e.workspace_id = :ws AND e.target_type = 'document' "
             "AND e.source_doc_id = ANY(:ids) AND d.tainted AND NOT (e.target_id = ANY(:ids))"),
        {"ws": workspace_id, "ids": list(before)},
    )
    return len(before) + int(downstream or 0)


# ----------------------------------------------------------------- renew


async def renew_token(session: AsyncSession, principal: Principal, token_id: str,
                      ttl_seconds: int) -> dict[str, Any]:
    workspace = await session.get(Workspace, principal.workspace.id)
    target = await _target(session, workspace, token_id)
    if principal.token_cid == target.canonical_id:
        raise ApiError(403, "renewal_requires_parent",
                       "a token cannot renew itself; ask its parent",
                       details={"renew_with": target.parent_token_id})
    if not _may_manage(principal, target, allow_self=False):
        raise Forbidden("only an ancestor or a human may renew a token")
    now = _now()
    if target.revoked_at is not None:
        raise ApiError(409, "token_already_expired", "a revoked token cannot be renewed",
                       details={"revoked_at": target.revoked_at.isoformat()})
    if target.expires_at is not None and target.expires_at <= now:
        raise ApiError(409, "token_already_expired",
                       "an expired token cannot be renewed; issue a new one",
                       details={"expired_at": target.expires_at.isoformat()})
    ceiling = BRANCH_TTL_MAX if target.depth <= 1 else CHILD_TTL_MAX
    if ttl_seconds < 60 or ttl_seconds > ceiling:
        raise ApiError(422, "validation_error", "ttl_seconds is out of range",
                       details={"field": "ttl_seconds", "min": 60, "max": ceiling})
    expires_at = now + timedelta(seconds=ttl_seconds)
    if principal.auth_kind is AuthKind.AGENT and principal.expires_at is not None:
        expires_at = min(expires_at, principal.expires_at)
    target.expires_at = expires_at
    target.expiring_notified_at = None
    await session.commit()
    return {"token_id": target.canonical_id, "expires_at": expires_at}


# ------------------------------------------------------------------ root


async def rotate_root(session: AsyncSession, workspace_id: uuid.UUID) -> dict[str, Any]:
    """Replace the root secret; the token id (and the whole tree) stays."""
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise NotFound("workspace")
    raw = generate_root_key()
    workspace.root_key_hash = hash_api_key(raw)
    workspace.root_key_rotated_at = _now()
    await session.commit()
    return {"workspace_id": str(workspace.id), "root_token_id": workspace.root_token_id,
            "root_key": raw, "rotated_at": workspace.root_key_rotated_at}


async def still_valid(principal: Principal) -> None:
    """Re-check a long-running request's token (revocation within seconds)."""
    if principal.auth_kind is not AuthKind.AGENT or principal.token_id is None:
        return
    async with async_session_factory() as session:
        row = (
            await session.execute(
                text("SELECT t.revoked_at, t.revoked_ancestor, t.canonical_id, t.is_active "
                     "FROM agent_tokens t WHERE t.id = :id"),
                {"id": principal.token_id},
            )
        ).one_or_none()
    if row is None or row[0] is not None or not row[3]:
        raise ApiError(401, "token_revoked", "this token was revoked",
                       details={"revoked_at": row[0].isoformat() if row and row[0] else None,
                                "revoked_ancestor": (row[1] or row[2]) if row else None},
                       headers={"WWW-Authenticate": "Bearer"})
