"""Tenant row-security context for PostgreSQL RLS.

Authentication queries run as the connection role before a workspace is known.
Once the principal is resolved, the request session is switched (with SET LOCAL
ROLE) to a non-superuser application role and given ``app.workspace_id``. A new
transaction on the same SQLAlchemy session re-applies the context automatically.
"""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import ApiError

logger = logging.getLogger("agentgraph.tenant")
settings = get_settings()

_SAFE_ROLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _role_identifier() -> str:
    role = settings.rls_role
    if not _SAFE_ROLE.fullmatch(role):
        raise RuntimeError("rls_role must be a simple SQL identifier")
    return role


def set_session_tenant(session: Session, workspace_id: uuid.UUID | str) -> None:
    """Attach tenant context to a session before its first query."""
    session.info["workspace_id"] = str(workspace_id)


async def activate_tenant(session: Session, workspace_id: uuid.UUID | str) -> None:
    """Apply tenant context to the current transaction and future ones."""
    set_session_tenant(session, workspace_id)
    if not settings.rls_enabled:
        return
    role = _role_identifier()
    try:
        await session.execute(text(f'SET LOCAL ROLE "{role}"'))
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
    except Exception as exc:  # noqa: BLE001 - fail closed on tenant isolation
        logger.error(
            "tenant RLS activation failed (role=%s): %s", role, exc
        )
        session.info.pop("workspace_id", None)
        raise ApiError(
            503,
            "tenant_context_unavailable",
            "tenant row-security context could not be established",
        ) from exc


@event.listens_for(Session, "after_begin")
def _apply_tenant_context(
    session: Session, transaction: object, connection: object
) -> None:
    workspace_id = session.info.get("workspace_id")
    if not workspace_id or not settings.rls_enabled:
        return
    role = _role_identifier()
    try:
        connection.exec_driver_sql(f'SET LOCAL ROLE "{role}"')
        connection.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
    except Exception as exc:  # noqa: BLE001 - fail closed on tenant isolation
        logger.error("tenant context could not be applied: %s", exc)
        raise ApiError(
            503,
            "tenant_context_unavailable",
            "tenant row-security context could not be established",
        ) from exc


__all__ = ["activate_tenant", "set_session_tenant"]
