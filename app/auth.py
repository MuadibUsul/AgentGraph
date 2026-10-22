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
        signature = (agent_hint or "human_commander").strip() or "human_commander"
        return Principal(
            workspace=workspace,
            role=RoleScope.COMMANDER,
            agent_signature=signature[:128],
            auth_kind=AuthKind.WORKSPACE,
            allowed_namespaces=["*"],
        )

    token = (
        await session.execute(
            select(AgentToken).where(
                AgentToken.api_key_hash == key_hash,
                AgentToken.is_active.is_(True),
                AgentToken.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if token is not None:
        if token.expires_at is not None and token.expires_at <= now:
            return None
        workspace = await session.get(Workspace, token.workspace_id)
        if workspace is None:
            return None
        return Principal(
            workspace=workspace,
            role=RoleScope(token.role_scope.value),
            agent_signature=token.agent_signature,
            auth_kind=AuthKind.AGENT,
            allowed_namespaces=list(token.allowed_namespaces or ["*"]),
            token_id=token.id,
        )

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


def can(principal: Principal, permission: str) -> bool:
    granted = PERMISSIONS.get(principal.role, set())
    return "*" in granted or permission in granted


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
    if principal.is_commander or "*" in principal.allowed_namespaces:
        return
    if namespace not in principal.allowed_namespaces:
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
