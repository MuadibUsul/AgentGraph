"""Tenant row-security context for PostgreSQL RLS.

Authentication queries run as the connection role before a workspace is known.
Once the principal is resolved, the request session is switched (with SET LOCAL
ROLE) to a non-superuser application role and given ``app.workspace_id`` and
``app.tenant_scope`` (the tenant ids this request may touch; PRD AG-ID-07).
Policies require both, so a connection without tenant context reads nothing.
A new transaction on the same SQLAlchemy session re-applies the context.
"""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import ApiError
from app.stamp_context import stamp_json

logger = logging.getLogger("agentgraph.tenant")
settings = get_settings()

_SAFE_ROLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _role_identifier() -> str:
    role = settings.rls_role
    if not _SAFE_ROLE.fullmatch(role):
        raise RuntimeError("rls_role must be a simple SQL identifier")
    return role


_SCOPE_SQL = text("SELECT tenant_id FROM workspaces WHERE id = CAST(:workspace_id AS uuid)")
_SET_CONTEXT_SQL = text(
    "SELECT set_config('app.workspace_id', :workspace_id, true), "
    "set_config('app.tenant_scope', :tenant_scope, true), "
    "set_config('app.lineage_stamp', :stamp, true)"
)
_SET_STAMP_SQL = text("SELECT set_config('app.lineage_stamp', :stamp, true)")


def set_session_tenant(
    session: Session,
    workspace_id: uuid.UUID | str,
    tenant_scope: list[str] | None = None,
) -> None:
    """Attach tenant context to a session before its first query.

    Without ``tenant_scope`` the scope is derived from the workspace when the
    next transaction begins (before the role switch).
    """
    session.info["workspace_id"] = str(workspace_id)
    if tenant_scope:
        session.info["tenant_scope"] = ",".join(tenant_scope)
    else:
        session.info.pop("tenant_scope", None)


async def activate_tenant(
    session: Session,
    workspace_id: uuid.UUID | str,
    tenant_scope: list[str] | None = None,
) -> None:
    """Apply tenant context to the current transaction and future ones."""
    set_session_tenant(session, workspace_id, tenant_scope)
    if not settings.rls_enabled:
        return
    role = _role_identifier()
    try:
        if "tenant_scope" not in session.info:
            tenant_id = (
                await session.execute(_SCOPE_SQL, {"workspace_id": str(workspace_id)})
            ).scalar_one_or_none()
            session.info["tenant_scope"] = tenant_id or ""
        await session.execute(text(f'SET LOCAL ROLE "{role}"'))
        await session.execute(
            _SET_CONTEXT_SQL,
            {
                "workspace_id": str(workspace_id),
                "tenant_scope": session.info["tenant_scope"],
                "stamp": stamp_json(),
            },
        )
    except Exception as exc:  # noqa: BLE001 - fail closed on tenant isolation
        logger.error(
            "tenant RLS activation failed (role=%s): %s", role, exc
        )
        session.info.pop("workspace_id", None)
        session.info.pop("tenant_scope", None)
        raise ApiError(
            503,
            "tenant_context_unavailable",
            "tenant row-security context could not be established",
        ) from exc


async def apply_stamp(session: Session) -> None:
    """Publish the current lineage stamp to the session's open transaction."""
    await session.execute(_SET_STAMP_SQL, {"stamp": stamp_json()})


@event.listens_for(Session, "after_begin")
def _apply_tenant_context(
    session: Session, transaction: object, connection: object
) -> None:
    workspace_id = session.info.get("workspace_id")
    stamp = stamp_json()
    if not workspace_id or not settings.rls_enabled:
        if stamp:
            # Admin and other unscoped sessions still stamp their writes.
            connection.execute(_SET_STAMP_SQL, {"stamp": stamp})
        return
    role = _role_identifier()
    try:
        if "tenant_scope" not in session.info:
            tenant_id = connection.execute(
                _SCOPE_SQL, {"workspace_id": str(workspace_id)}
            ).scalar_one_or_none()
            session.info["tenant_scope"] = tenant_id or ""
        connection.exec_driver_sql(f'SET LOCAL ROLE "{role}"')
        connection.execute(
            _SET_CONTEXT_SQL,
            {
                "workspace_id": str(workspace_id),
                "tenant_scope": session.info["tenant_scope"],
                "stamp": stamp_json(),
            },
        )
    except Exception as exc:  # noqa: BLE001 - fail closed on tenant isolation
        logger.error("tenant context could not be applied: %s", exc)
        raise ApiError(
            503,
            "tenant_context_unavailable",
            "tenant row-security context could not be established",
        ) from exc


__all__ = ["activate_tenant", "set_session_tenant"]
