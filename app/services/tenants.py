"""Tenant lifecycle and membership sync (PRD AG-ID-07, AG-ID-10 groundwork).

Each customer is a single-node root tenant in Phase 1; the tree fields exist
from day zero so hierarchy (AG-ID-09..13) needs no migration later.

Deletion is a two-party decision: the operator (admin) and a tenant owner both
confirm. The tenant then enters ``deleting`` for the grace period (30 days by
default): unusable except for the owner cancelling or fetching the final
export. After the grace period the reaper purges every workspace and keeps a
content-free tombstone row so the id and alias are never reused.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.config import get_settings
from app.errors import ApiError, Forbidden, NotFound
from app.models import HumanUser, Membership, Tenant, Workspace
from app.schemas import TenantInfo, TenantListResponse, TenantOut
from app.services.common import utcnow

logger = logging.getLogger("agentgraph.tenants")
settings = get_settings()

# Account roles of 3.x map onto PRD membership roles.
_MEMBERSHIP_ROLE = {"OWNER": "owner", "ADMIN": "admin", "REVIEWER": "member", "VIEWER": "guest"}


def tenant_info(tenant: Tenant) -> TenantInfo:
    return TenantInfo(
        id=tenant.id,
        alias=tenant.alias,
        display_name=tenant.display_name,
        tenant_type=tenant.tenant_type,
        parent_tenant_id=tenant.parent_tenant_id,
        root_tenant_id=tenant.root_tenant_id,
        path=list(tenant.path or []),
        depth=tenant.depth,
        status=tenant.status,
    )


def _pending(tenant: Tenant) -> list[str]:
    if tenant.status != "active":
        return []
    pending: list[str] = []
    started = tenant.deletion_admin_confirmed_at or tenant.deletion_owner_confirmed_at
    if started is None:
        return pending
    if tenant.deletion_admin_confirmed_at is None:
        pending.append("admin")
    if tenant.deletion_owner_confirmed_at is None:
        pending.append("owner")
    return pending


def tenant_out(tenant: Tenant, workspaces: int = 0) -> TenantOut:
    return TenantOut(
        **tenant_info(tenant).model_dump(),
        billing_owner_tenant_id=tenant.billing_owner_tenant_id,
        status_reason=tenant.status_reason,
        status_changed_at=tenant.status_changed_at,
        deletion_admin_confirmed_at=tenant.deletion_admin_confirmed_at,
        deletion_owner_confirmed_at=tenant.deletion_owner_confirmed_at,
        purge_after=tenant.purge_after,
        deleted_at=tenant.deleted_at,
        final_export_id=tenant.final_export_id,
        pending_confirmations=_pending(tenant),
        workspaces=workspaces,
        created_at=tenant.created_at,
    )


async def _workspace_count(session: AsyncSession, tenant_id: str) -> int:
    return int(
        await session.scalar(
            select(func.count(Workspace.id)).where(Workspace.tenant_id == tenant_id)
        )
        or 0
    )


async def get_tenant(session: AsyncSession, tenant_id: str) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFound("tenant")
    return tenant


async def describe_tenant(session: AsyncSession, tenant_id: str) -> TenantOut:
    tenant = await get_tenant(session, tenant_id)
    return tenant_out(tenant, await _workspace_count(session, tenant.id))


async def list_tenants(session: AsyncSession) -> TenantListResponse:
    rows = (
        await session.execute(
            select(Tenant, func.count(Workspace.id))
            .outerjoin(Workspace, Workspace.tenant_id == Tenant.id)
            .group_by(Tenant.id)
            .order_by(Tenant.created_at.desc())
        )
    ).all()
    return TenantListResponse(items=[tenant_out(tenant, count) for tenant, count in rows])


def _set_status(tenant: Tenant, status: str, reason: str | None = None) -> None:
    tenant.status = status
    tenant.status_reason = reason
    tenant.status_changed_at = utcnow()


async def suspend_tenant(session: AsyncSession, tenant_id: str, reason: str) -> TenantOut:
    tenant = await get_tenant(session, tenant_id)
    if tenant.status in ("deleting", "deleted"):
        raise ApiError(
            409, "invalid_transition", f"cannot suspend a tenant that is {tenant.status}"
        )
    _set_status(tenant, "suspended", reason)
    await session.commit()
    return await describe_tenant(session, tenant_id)


async def resume_tenant(session: AsyncSession, tenant_id: str) -> TenantOut:
    tenant = await get_tenant(session, tenant_id)
    if tenant.status != "suspended":
        raise ApiError(409, "invalid_transition", f"tenant is {tenant.status}, not suspended")
    _set_status(tenant, "active")
    await session.commit()
    return await describe_tenant(session, tenant_id)


def _check_confirmation(tenant: Tenant, confirm: str) -> None:
    if confirm != tenant.alias:
        raise ApiError(
            409,
            "workspace_confirmation_mismatch",
            "confirm must exactly match the tenant alias",
            details={"tenant": tenant.alias},
        )
    if tenant.status in ("deleting", "deleted"):
        raise ApiError(409, "invalid_transition", f"tenant is already {tenant.status}")


def _maybe_start_deletion(tenant: Tenant) -> None:
    if tenant.deletion_admin_confirmed_at and tenant.deletion_owner_confirmed_at:
        _set_status(tenant, "deleting", "deletion confirmed by operator and owner")
        tenant.purge_after = utcnow() + timedelta(days=settings.tenant_deletion_grace_days)


async def _final_export(tenant: Tenant) -> None:
    """Once deletion starts, take the final full export the owner can download."""
    if tenant.status == "deleting" and not tenant.final_export_id:
        from app.services.exports import start_final_export

        await start_final_export(tenant.id)


async def admin_confirm_deletion(
    session: AsyncSession, tenant_id: str, confirm: str
) -> TenantOut:
    tenant = await get_tenant(session, tenant_id)
    _check_confirmation(tenant, confirm)
    tenant.deletion_admin_confirmed_at = utcnow()
    _maybe_start_deletion(tenant)
    await session.commit()
    await _final_export(tenant)
    session.expire_all()
    return await describe_tenant(session, tenant_id)


def _require_owner(principal: Principal) -> None:
    if principal.user_id is None or principal.human_role != "OWNER":
        raise Forbidden("a tenant owner is required for this action")


async def owner_confirm_deletion(
    session: AsyncSession, principal: Principal, confirm: str
) -> TenantOut:
    _require_owner(principal)
    tenant = await get_tenant(session, principal.tenant_id)
    _check_confirmation(tenant, confirm)
    tenant.deletion_owner_confirmed_at = utcnow()
    tenant.deletion_owner_confirmed_by = principal.agent_signature
    _maybe_start_deletion(tenant)
    await session.commit()
    tenant_id = tenant.id
    await _final_export(tenant)
    session.expire_all()
    return await describe_tenant(session, tenant_id)


def _clear_deletion(tenant: Tenant) -> None:
    tenant.deletion_admin_confirmed_at = None
    tenant.deletion_owner_confirmed_at = None
    tenant.deletion_owner_confirmed_by = None
    tenant.purge_after = None


async def restore_tenant(session: AsyncSession, tenant_id: str) -> TenantOut:
    """Cancel a pending or in-grace deletion (operator or owner)."""
    tenant = await get_tenant(session, tenant_id)
    if tenant.status == "deleted":
        raise ApiError(410, "gone", "the tenant was purged and cannot be restored",
                       details={"tenant": tenant.id, "deleted_at": tenant.deleted_at.isoformat()
                                if tenant.deleted_at else None})
    if tenant.status == "deleting":
        _set_status(tenant, "active", "deletion cancelled")
    _clear_deletion(tenant)
    await session.commit()
    return await describe_tenant(session, tenant_id)


async def owner_restore_tenant(session: AsyncSession, principal: Principal) -> TenantOut:
    _require_owner(principal)
    return await restore_tenant(session, principal.tenant_id)


_TREE_ERRORS = ("tenant_type_invalid_parent", "tenant_cycle", "tenant_depth_exceeded")


async def create_child_tenant(
    session: AsyncSession,
    parent_id: str,
    *,
    display_name: str,
    tenant_type: str = "organization",
    alias: str | None = None,
    workspace_name: str | None = None,
    tier: str = "free",
    operator_managed: bool = False,
) -> dict:
    """A tenant under ``parent_id`` with its first workspace (AG-ID-07 tree, ID-09 groundwork).

    The tree trigger derives root, path and depth and enforces the type rules.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.services.workspace import create_workspace

    parent = await get_tenant(session, parent_id)
    if parent.status != "active":
        raise ApiError(409, "invalid_state_transition", "the parent tenant is not active",
                       details={"status": parent.status})
    if parent.tenant_type == "personal":
        raise ApiError(422, "tenant_type_invalid_parent", "personal tenants have no children",
                       details={"child_type": tenant_type, "parent_type": "personal",
                                "allowed": []})
    if parent.depth >= 4:
        # depth counts the root as 1: three levels below the root (AG-ID-09).
        raise ApiError(422, "tenant_depth_exceeded", "a tenant tree is at most 3 levels deep",
                       details={"max_depth": 3})
    if tenant_type not in ("organization", "personal"):
        raise ApiError(422, "tenant_type_invalid_parent",
                       "a child tenant is an organization or a personal tenant",
                       details={"allowed": ["organization", "personal"]})
    try:
        tenant_id = await session.scalar(
            text("INSERT INTO tenants (id, alias, display_name, tenant_type, parent_tenant_id) "
                 "SELECT n.id, COALESCE(:alias, agentgraph_tenant_alias(:name, n.id)), :name, "
                 ":type, :parent FROM (SELECT agentgraph_new_id('ten') AS id) n RETURNING id"),
            {"alias": alias, "name": display_name, "type": tenant_type, "parent": parent_id},
        )
    except DBAPIError as exc:
        message = str(getattr(exc, "orig", exc))
        code = next((item for item in _TREE_ERRORS if item in message), "validation_error")
        raise ApiError(422, code, "the tenant tree refused this node",
                       details={"reason": message.splitlines()[0][:200]}) from exc
    created = await create_workspace(
        session, name=workspace_name or display_name, tier=tier, tenant_id=tenant_id,
        operator_managed=operator_managed,
    )
    return {"tenant": await describe_tenant(session, tenant_id), **created.model_dump()}


async def purge_due_tenants(session: AsyncSession) -> int:
    """Physically delete tenants whose grace period ended; keep a tombstone."""
    due = (
        await session.execute(
            select(Tenant).where(
                Tenant.status == "deleting",
                Tenant.purge_after.is_not(None),
                Tenant.purge_after <= utcnow(),
            )
        )
    ).scalars().all()
    for tenant in due:
        workspaces = (
            await session.execute(select(Workspace).where(Workspace.tenant_id == tenant.id))
        ).scalars().all()
        for workspace in workspaces:
            await session.delete(workspace)
        await session.execute(delete(Membership).where(Membership.tenant_id == tenant.id))
        _set_status(tenant, "deleted", "purged after the deletion grace period")
        tenant.deleted_at = utcnow()
        # Tombstone: the row (id, alias) stays so neither is ever reused, but
        # it carries no customer content.
        tenant.display_name = "[deleted]"
        _clear_deletion(tenant)
        logger.info("purged tenant %s (%s workspace(s))", tenant.id, len(workspaces))
    await session.commit()
    return len(due)


async def sync_membership(session: AsyncSession, user: HumanUser) -> None:
    """Mirror a 3.x account role onto the tenant membership (no commit)."""
    workspace = await session.get(Workspace, user.workspace_id)
    if workspace is None:
        return
    role = _MEMBERSHIP_ROLE.get(user.role, "guest")
    existing = await session.scalar(
        select(Membership).where(
            Membership.tenant_id == workspace.tenant_id,
            Membership.principal_id == user.id,
        )
    )
    if existing is None:
        session.add(
            Membership(
                id=uuid.uuid4(),
                tenant_id=workspace.tenant_id,
                principal_id=user.id,
                role=role,
            )
        )
    elif existing.role != role:
        existing.role = role
        existing.updated_at = utcnow()
