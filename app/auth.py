"""Authentication, scoped agent tokens, and the v3 capability matrix."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.errors import (
    ApiError,
    Forbidden,
    HumanSovereigntyLocked,
    NamespaceForbidden,
)
from app.models import AgentToken, HumanSession, HumanUser, Tenant, Workspace
from app.tenant import activate_tenant

settings = get_settings()


class RoleScope(str, Enum):
    COMMANDER = "COMMANDER"
    VIEWER = "VIEWER"
    SCOUT = "SCOUT"
    ANALYST = "ANALYST"
    AUDITOR = "AUDITOR"
    EXECUTOR = "EXECUTOR"


class AuthKind(str, Enum):
    WORKSPACE = "workspace"
    AGENT = "agent"
    HUMAN = "human"
    HUMAN_ADMIN = "human_admin"
    # The workspace's root token (AG-ID-01): token management only.
    ROOT = "root"


@dataclass
class Principal:
    workspace: Workspace
    role: RoleScope
    agent_signature: str
    auth_kind: AuthKind
    allowed_namespaces: list[str]
    token_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    human_role: str | None = None
    tenant: Tenant | None = None
    # Token tree (AG-ID-01/02): canonical id, effective grant along the chain.
    token_cid: str | None = None
    scopes: frozenset[str] | None = None  # None: limited by the role only
    write_namespaces: list[str] | None = None  # None: same as allowed_namespaces
    lineage: list[str] | None = None
    parent_token_id: str | None = None
    expires_at: datetime | None = None
    can_issue: bool = False
    token_depth: int = 0  # stored depth: root 0, branch 1, child 2...
    max_descendants: int = 0

    @property
    def tenant_id(self) -> str:
        return self.workspace.tenant_id

    @property
    def tenant_scope(self) -> list[str]:
        """Tenant ids this principal may touch (one node in Phase 1)."""
        return [self.workspace.tenant_id]

    @property
    def is_commander(self) -> bool:
        return self.role is RoleScope.COMMANDER

    @property
    def is_human(self) -> bool:
        # The workspace key is a commander/automation credential, not a human
        # session. Human-only sovereignty actions require an actual account or
        # the admin console token.
        return self.auth_kind in (AuthKind.HUMAN, AuthKind.HUMAN_ADMIN)


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    return f"{settings.api_key_prefix}{secrets.token_urlsafe(32)}"


def generate_agent_key() -> str:
    return f"{settings.agent_key_prefix}{secrets.token_urlsafe(32)}"


def generate_human_key() -> str:
    return f"{settings.human_key_prefix}{secrets.token_urlsafe(32)}"


def extract_bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("Authorization")
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
    api_key_header = request.headers.get("X-API-Key")
    if api_key_header and api_key_header.strip():
        return api_key_header.strip()
    return None


def extract_admin_token(request: Request) -> str | None:
    token = extract_bearer_token(request)
    if token:
        return token
    header = request.headers.get("X-Admin-Token")
    return header.strip() if header and header.strip() else None


async def _resolve_principal(
    session: AsyncSession,
    raw_key: str,
    *,
    agent_hint: str | None = None,
) -> Principal | None:
    key_hash = hash_api_key(raw_key)

    workspace = (
        await session.execute(select(Workspace).where(Workspace.api_key_hash == key_hash))
    ).scalar_one_or_none()
    if workspace is not None:
        # The workspace key: a branch of the root with commander rights.
        signature = (agent_hint or "human_commander").strip() or "human_commander"
        return Principal(
            workspace=workspace,
            role=RoleScope.COMMANDER,
            agent_signature=signature[:128],
            auth_kind=AuthKind.WORKSPACE,
            allowed_namespaces=["*"],
            token_cid=workspace.key_token_id,
            lineage=[workspace.root_token_id, workspace.key_token_id],
            parent_token_id=workspace.root_token_id,
            can_issue=True,
            token_depth=1,
        )
    workspace = (
        await session.execute(select(Workspace).where(Workspace.root_key_hash == key_hash))
    ).scalar_one_or_none()
    if workspace is not None:
        return Principal(
            workspace=workspace,
            role=RoleScope.COMMANDER,
            agent_signature="workspace_root",
            auth_kind=AuthKind.ROOT,
            allowed_namespaces=["*"],
            token_cid=workspace.root_token_id,
            lineage=[workspace.root_token_id],
            can_issue=True,
            token_depth=0,
        )

    token = (
        await session.execute(select(AgentToken).where(AgentToken.api_key_hash == key_hash))
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if token is not None:
        if token.revoked_at is not None:
            raise _revoked(token, token.revoked_ancestor or token.canonical_id)
        if not token.is_active:
            return None
        workspace = await session.get(Workspace, token.workspace_id)
        if workspace is None:
            return None
        return await _agent_principal(session, workspace, token, now)

    human = (
        await session.execute(
            select(HumanSession, HumanUser)
            .join(HumanUser, HumanUser.id == HumanSession.user_id)
            .where(
                HumanSession.token_hash == key_hash,
                HumanSession.revoked_at.is_(None),
                HumanSession.expires_at > now,
                HumanUser.is_active.is_(True),
            )
        )
    ).one_or_none()
    if human is None:
        return None
    human_session, user = human
    workspace = await session.get(Workspace, human_session.workspace_id)
    if workspace is None:
        return None
    role = (
        RoleScope.VIEWER
        if user.role == "VIEWER"
        else RoleScope.COMMANDER
    )
    return Principal(
        workspace=workspace,
        role=role,
        agent_signature=user.email,
        auth_kind=AuthKind.HUMAN,
        allowed_namespaces=["*"],
        token_id=human_session.id,
        user_id=user.id,
        human_role=user.role,
    )


def _revoked(token: AgentToken, ancestor: str) -> ApiError:
    return ApiError(
        401, "token_revoked", "this token was revoked",
        details={"revoked_at": token.revoked_at.isoformat() if token.revoked_at else None,
                 "revoked_ancestor": ancestor},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _expired(token: AgentToken, expired: AgentToken) -> ApiError:
    return ApiError(
        401, "token_expired", "this token or one of its ancestors expired",
        details={"expired_at": expired.expires_at.isoformat(),
                 "expired_token": expired.canonical_id,
                 "parent_token_id": token.parent_token_id},
        headers={"WWW-Authenticate": "Bearer"},
    )


def meet(first: list[str], second: list[str]) -> list[str]:
    """Intersect two namespace lists where "*" means every namespace."""
    if "*" in first:
        return list(second)
    if "*" in second:
        return list(first)
    return [item for item in first if item in second]


async def _agent_principal(
    session: AsyncSession, workspace: Workspace, token: AgentToken, now: datetime
) -> Principal:
    """The effective grant is the intersection along the chain (AG-ID-02 point 1)."""
    lineage = list(token.lineage or [workspace.root_token_id, token.canonical_id])
    ancestors = []
    others = [item for item in lineage if item not in (
        token.canonical_id, workspace.root_token_id, workspace.key_token_id)]
    if others:
        rows = (
            await session.execute(
                select(AgentToken).where(AgentToken.workspace_id == workspace.id,
                                         AgentToken.canonical_id.in_(others))
            )
        ).scalars().all()
        by_id = {row.canonical_id: row for row in rows}
        ancestors = [by_id[item] for item in others if item in by_id]
    for ancestor in ancestors:
        if ancestor.revoked_at is not None:
            raise _revoked(token, ancestor.canonical_id)
    for item in [*ancestors, token]:
        if item.expires_at is not None and item.expires_at <= now:
            raise _expired(token, item)
    scopes: frozenset[str] | None = None
    read = ["*"]
    write = ["*"]
    for item in [*ancestors, token]:
        if item.scopes is not None:
            scopes = frozenset(item.scopes) if scopes is None else scopes & set(item.scopes)
        read = meet(read, list(item.read_namespaces or item.allowed_namespaces or ["*"]))
        write = meet(write, list(item.write_namespaces or item.allowed_namespaces or ["*"]))
    expires = [item.expires_at for item in [*ancestors, token] if item.expires_at]
    return Principal(
        workspace=workspace,
        role=RoleScope(token.role_scope.value),
        agent_signature=token.agent_signature,
        auth_kind=AuthKind.AGENT,
        allowed_namespaces=read,
        token_id=token.id,
        token_cid=token.canonical_id,
        scopes=scopes,
        write_namespaces=write,
        lineage=lineage,
        parent_token_id=token.parent_token_id,
        expires_at=min(expires) if expires else None,
        can_issue=bool(token.can_issue) and (scopes is None or "token:issue" in scopes),
        token_depth=token.depth,
        max_descendants=token.max_descendants,
    )


async def resolve_principal(
    session: AsyncSession,
    raw_key: str,
    *,
    agent_hint: str | None = None,
) -> Principal | None:
    principal = await _resolve_principal(session, raw_key, agent_hint=agent_hint)
    if principal is None:
        return None
    tenant = await session.get(Tenant, principal.workspace.tenant_id)
    if tenant is None or tenant.status == "deleted":
        # A purged tenant's credentials behave like unknown keys.
        return None
    principal.tenant = tenant
    return principal


# POST endpoints that only read; a suspended tenant may still call them.
_READ_ONLY_POSTS = frozenset({"/v1/query", "/v1/dereference", "/v1/dereference-many"})
# Reachable while a tenant is being deleted: owners cancel deletion or fetch
# the final export, and anyone may sign out.
_DELETING_ALLOWED = frozenset(
    {
        "/v1/human/tenant",
        "/v1/human/tenant/restore",
        "/v1/human/tenant/final-export",
        "/v1/auth/me",
        "/v1/auth/logout",
    }
)
# A suspended tenant keeps reading and exporting (AG-ID-07 point 8).
_SUSPENDED_ALLOWED_WRITES = frozenset(
    {"/v1/auth/logout", "/v1/auth/password", "/v1/exports"}
)


def is_write_request(method: str, path: str) -> bool:
    return method.upper() in ("POST", "PUT", "PATCH", "DELETE") and (
        path not in _READ_ONLY_POSTS
    )


def ensure_tenant_usable(principal: Principal, *, write: bool, path: str = "") -> None:
    """Apply tenant lifecycle rules (PRD AG-ID-07 points 8 and 10)."""
    tenant = principal.tenant
    if tenant is None or principal.auth_kind is AuthKind.HUMAN_ADMIN:
        return
    if tenant.status == "deleting" and path not in _DELETING_ALLOWED:
        raise ApiError(
            403,
            "tenant_deleting",
            "this tenant is scheduled for deletion; only an owner can cancel it "
            "or download the final export",
            details={
                "purge_after": tenant.purge_after.isoformat() if tenant.purge_after else None,
                "restorable": True,
                "tenant": tenant.id,
            },
        )
    if tenant.status == "suspended" and write and path not in _SUSPENDED_ALLOWED_WRITES:
        raise ApiError(
            403,
            "tenant_suspended",
            "this tenant is suspended: reads and exports still work, writes do not",
            details={
                "reason_code": tenant.status_reason or "suspended",
                "since": (
                    tenant.status_changed_at.isoformat() if tenant.status_changed_at else None
                ),
                "contact": "the workspace owner or the AgentGraph operator",
            },
        )


async def get_principal(
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> Principal:
    raw_key = extract_bearer_token(request)
    if not raw_key:
        raise ApiError(
            401,
            "unauthorized",
            "missing bearer API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    principal = await resolve_principal(
        session, raw_key, agent_hint=request.headers.get("X-Agent-Signature")
    )
    if principal is None:
        raise ApiError(
            401,
            "unauthorized",
            "invalid or inactive API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    ensure_tenant_usable(
        principal,
        write=is_write_request(request.method, request.url.path),
        path=request.url.path,
    )
    from app.services.lineage import stamp_for
    from app.stamp_context import set_current_stamp

    set_current_stamp(await stamp_for(principal))
    from app.services.access_log import note_request
    from app.stamp_context import current_stamp

    write = is_write_request(request.method, request.url.path)
    note_request(principal.workspace.id, current_stamp(), write=write)
    from app.services import directory, token_tree

    await token_tree.after_authentication(principal, path=request.url.path)
    await directory.touch(principal, write=write)
    await directory.load_actor_map(principal)
    await activate_tenant(session, principal.workspace.id, principal.tenant_scope)
    from app.services.common import set_audit_actor
    from app.services.usage import enforce_rate_limit

    set_audit_actor(principal)

    await enforce_rate_limit(session, principal)
    return principal


async def get_human_principal(
    principal: Principal = Depends(get_principal),
) -> Principal:
    if not principal.is_human:
        raise Forbidden("a human account or workspace commander key is required")
    return principal


async def get_human_writer(
    principal: Principal = Depends(get_principal),
) -> Principal:
    if not principal.is_human or not principal.is_commander:
        raise Forbidden("a human owner/reviewer is required for this action")
    return principal


async def get_human_account(
    principal: Principal = Depends(get_principal),
) -> Principal:
    """Require a human account session (not a workspace/commander API key)."""
    if principal.auth_kind is not AuthKind.HUMAN or principal.user_id is None:
        raise ApiError(
            401,
            "human_session_required",
            "sign in with a human account to use this endpoint",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


async def get_current_workspace(
    principal: Principal = Depends(get_principal),
) -> Workspace:
    return principal.workspace


def principal_for_admin(workspace: Workspace) -> Principal:
    return Principal(
        workspace=workspace,
        role=RoleScope.COMMANDER,
        agent_signature="human_commander",
        auth_kind=AuthKind.HUMAN_ADMIN,
        allowed_namespaces=["*"],
    )


def principal_for_human(user: HumanUser, workspace: Workspace) -> Principal:
    role = RoleScope.VIEWER if user.role == "VIEWER" else RoleScope.COMMANDER
    return Principal(
        workspace=workspace,
        role=role,
        agent_signature=user.email,
        auth_kind=AuthKind.HUMAN,
        allowed_namespaces=["*"],
        user_id=user.id,
        human_role=user.role,
    )


async def require_admin(request: Request) -> None:
    from app.services.common import set_audit_actor_values

    set_audit_actor_values("human_admin", None, None)
    from app.services.lineage import ADMIN_STAMP
    from app.stamp_context import set_current_stamp

    set_current_stamp(dict(ADMIN_STAMP))
    if not settings.admin_token:
        raise ApiError(
            503,
            "admin_disabled",
            "ADMIN_TOKEN is not configured on this server",
        )
    supplied = extract_admin_token(request)
    if not supplied or not hmac.compare_digest(supplied, settings.admin_token):
        raise ApiError(
            401,
            "unauthorized",
            "invalid admin token",
            headers={"WWW-Authenticate": "Bearer"},
        )


AdminGuard = Depends(require_admin)
TenantGuard = Depends(get_principal)


# ---------------------------------------------------------------- capabilities

PERMISSIONS: dict[RoleScope, set[str]] = {
    RoleScope.COMMANDER: {"*"},
    RoleScope.VIEWER: {
        "workspace:read",
        "events:read",
        "state:read",
        "task:read",
        "doc:read",
        "timeline:read",
        "link:read",
        "query",
        "proposal:read",
    },
    RoleScope.SCOUT: {
        "workspace:read",
        "events:read",
        "task:read",
        "doc:read",
        "doc:create",
        "timeline:append",
        "link:read",
        "proposal:read",
        "proposal:vote",
    },
    RoleScope.ANALYST: {
        "workspace:read",
        "events:read",
        "state:read",
        "task:read",
        "task:create",
        "task:claim",
        "task:transition",
        "doc:read",
        "doc:write",
        "doc:create",
        "doc:propose",
        "timeline:append",
        "timeline:read",
        "link:read",
        "link:write",
        "query",
        "proposal:read",
        "proposal:vote",
        "action:read",
        "budget:read",
    },
    RoleScope.AUDITOR: {
        "workspace:read",
        "events:read",
        "state:read",
        "task:read",
        "task:claim",
        "task:transition",
        "task:review",
        "doc:read",
        "doc:write",
        "doc:verify",
        "doc:propose",
        "timeline:append",
        "timeline:read",
        "link:read",
        "query",
        "audit:read",
        "proposal:read",
        "proposal:vote",
        "action:read",
        "budget:read",
    },
    RoleScope.EXECUTOR: {
        "workspace:read",
        "events:read",
        "state:read",
        "state:write",
        "task:read",
        "task:create",
        "task:claim",
        "task:transition",
        "doc:read",
        "doc:write",
        "doc:create",
        "doc:propose",
        "timeline:append",
        "timeline:read",
        "link:read",
        "link:write",
        "query",
        "proposal:read",
        "proposal:vote",
        "action:read",
        "action:propose",
        "budget:read",
    },
}


# PRD AG-ID-02 point 7: the scope vocabulary and the permissions each grants.
SCOPE_PERMISSIONS: dict[str, frozenset[str]] = {
    "docs:read": frozenset({"workspace:read", "doc:read", "query", "link:read",
                            "timeline:read", "proposal:read", "state:read"}),
    "docs:write": frozenset({"doc:write", "doc:create", "link:write", "state:write",
                             "timeline:append"}),
    "docs:archive": frozenset({"doc:archive"}),
    "tasks:read": frozenset({"workspace:read", "task:read"}),
    "tasks:write": frozenset({"task:create", "task:claim", "task:transition"}),
    "tasks:review": frozenset({"task:review", "doc:verify"}),
    "proposals:write": frozenset({"doc:propose", "proposal:vote"}),
    "events:read": frozenset({"workspace:read", "events:read", "audit:read", "budget:read"}),
    "external_action:draft": frozenset({"action:read", "action:propose"}),
    "export": frozenset({"export"}),
    "webhooks:manage": frozenset({"webhook:manage"}),
    "token:issue": frozenset({"token:issue"}),
}
SCOPES = tuple(sorted(SCOPE_PERMISSIONS))


def scopes_for_role(role: RoleScope) -> list[str]:
    """The scopes a role's permissions cover (what a legacy token may grant on)."""
    granted = PERMISSIONS.get(role, set())
    if "*" in granted:
        return list(SCOPES)
    return [scope for scope, permissions in SCOPE_PERMISSIONS.items()
            if permissions & granted]


def effective_scopes(principal: Principal) -> list[str]:
    """What this credential could grant on: role ∩ chain scopes; token:issue by can_issue."""
    base = [scope for scope in scopes_for_role(principal.role) if scope != "token:issue"]
    if principal.scopes is not None:
        base = [scope for scope in base if scope in principal.scopes]
    if principal.can_issue:
        base.append("token:issue")
    return base


def can(principal: Principal, permission: str) -> bool:
    granted = PERMISSIONS.get(principal.role, set())
    if not ("*" in granted or permission in granted):
        return False
    if principal.scopes is None:
        return True
    return any(permission in SCOPE_PERMISSIONS.get(scope, ()) for scope in principal.scopes)


def require_permission(principal: Principal, permission: str) -> None:
    if not can(principal, permission):
        raise Forbidden(
            f"role {principal.role.value} lacks permission '{permission}'"
        )


def effective_agent(principal: Principal, provided: str | None) -> str:
    """Agent/human sessions pin identity; workspace commander keys may name the actor."""
    if principal.auth_kind in (
        AuthKind.AGENT,
        AuthKind.HUMAN,
        AuthKind.HUMAN_ADMIN,
    ):
        return principal.agent_signature
    if provided:
        provided = provided.strip()
        if provided:
            return provided[:128]
    return principal.agent_signature


def ensure_namespace(principal: Principal, namespace: str, *, write: bool = False) -> None:
    if principal.auth_kind is not AuthKind.AGENT:
        return
    names = (principal.write_namespaces
             if write and principal.write_namespaces is not None
             else principal.allowed_namespaces)
    if "*" in names:
        return
    if namespace not in names:
        raise NamespaceForbidden(namespace)


def ensure_not_human_locked(
    principal: Principal,
    resource: str,
    locked: bool,
    note: str | None = None,
) -> None:
    if locked and not principal.is_human:
        raise HumanSovereigntyLocked(resource, note)


def ensure_task_role(principal: Principal, assigned_role: str | None) -> None:
    if principal.is_commander:
        return
    if assigned_role and assigned_role != principal.role.value:
        raise Forbidden(
            f"task is assigned to role {assigned_role}, not {principal.role.value}"
        )


def session_batch_id(value: uuid.UUID | None) -> uuid.UUID:
    return value or uuid.uuid4()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
