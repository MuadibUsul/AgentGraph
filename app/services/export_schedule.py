"""Scheduled exports (PRD AG-ST-02 point 6).

When ``SCHEDULED_EXPORTS`` is on, every active root tenant gets one full
baseline a week and one increment a day on top of the latest scheduled
export, kept ``scheduled_export_keep_days`` (30). A full baseline is removed
only once no kept increment builds on it, so every kept increment can still
be restored. The chain is also what a restore drill can replay.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from app.config import get_settings
from app.database import async_session_factory

logger = logging.getLogger(__name__)
settings = get_settings()
FULL_EVERY_DAYS = 7
INCREMENT_EVERY_HOURS = 24


async def _admin_principal(tenant_id: str) -> Any:
    from app.auth import principal_for_admin
    from app.models import Workspace

    async with async_session_factory() as session:
        workspace = await session.scalar(text(
            "SELECT id FROM workspaces WHERE tenant_id = :t ORDER BY created_at LIMIT 1"),
            {"t": tenant_id})
        if workspace is None:
            return None
        return principal_for_admin(await session.get(Workspace, workspace))


async def plan() -> list[tuple[str, str | None]]:
    """(tenant_id, base export or None for a full) for every tenant that is due."""
    async with async_session_factory() as session:
        rows = (await session.execute(text(
            "SELECT t.id, "
            "(SELECT max(created_at) FROM exports e WHERE e.tenant_id = t.id AND "
            " e.purpose = 'scheduled' AND e.kind = 'full' AND e.status <> 'failed') AS last_full, "
            "(SELECT id FROM exports e WHERE e.tenant_id = t.id AND e.purpose = 'scheduled' "
            " AND e.status = 'done' ORDER BY created_at DESC LIMIT 1) AS last_done, "
            "(SELECT max(created_at) FROM exports e WHERE e.tenant_id = t.id AND "
            " e.purpose = 'scheduled' AND e.status <> 'failed') AS last_any, "
            "EXISTS (SELECT 1 FROM exports e WHERE e.tenant_id = t.id AND "
            " e.status IN ('queued', 'running')) AS busy "
            "FROM tenants t WHERE t.parent_tenant_id IS NULL AND t.status = 'active'"))
        ).mappings().all()
        now = await session.scalar(text("SELECT NOW()"))
    due: list[tuple[str, str | None]] = []
    for row in rows:
        if row["busy"]:
            continue
        if row["last_full"] is None or \
                (now - row["last_full"]).total_seconds() >= FULL_EVERY_DAYS * 86400:
            due.append((row["id"], None))
        elif row["last_done"] and (row["last_any"] is None or (now - row["last_any"])
                                   .total_seconds() >= INCREMENT_EVERY_HOURS * 3600):
            due.append((row["id"], row["last_done"]))
    return due


async def run_due(limit: int = 20) -> int:
    if not settings.scheduled_exports:
        return 0
    from app.services.exports import request_export

    started = 0
    for tenant_id, base in (await plan())[:limit]:
        principal = await _admin_principal(tenant_id)
        if principal is None:
            continue
        try:
            await request_export(principal, scope="tenant", scope_ref=tenant_id,
                                 since_export_id=base, purpose="scheduled",
                                 requested_by="schedule")
            started += 1
        except Exception:  # noqa: BLE001 - one tenant must not stop the others
            logger.exception("scheduled export failed to start")
    await prune()
    return started


async def prune() -> int:
    """Drop scheduled exports past retention, never a base a kept increment needs."""
    keep = settings.scheduled_export_keep_days
    async with async_session_factory() as session:
        result = await session.execute(text(
            "DELETE FROM exports e WHERE e.purpose = 'scheduled' AND "
            "e.created_at < NOW() - make_interval(days => :keep) AND NOT EXISTS ("
            "SELECT 1 FROM exports k WHERE (k.base_export_id = e.id OR k.prev_export_id = e.id) "
            "AND k.created_at >= NOW() - make_interval(days => :keep))"), {"keep": keep})
        await session.commit()
    return result.rowcount or 0
