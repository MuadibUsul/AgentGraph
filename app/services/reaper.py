"""Background lease reaper for tasks and state slots."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import delete

from app.config import get_settings
from app.database import async_session_factory
from app.models import WorkspaceEvent
from app.services.common import db_now
from app.services.idempotency import purge_expired as purge_expired_idempotency
from app.services.slots import reap_expired_slot_leases
from app.services.tasks import reap_expired_task_leases
from app.services.tenants import purge_due_tenants

logger = logging.getLogger("agentgraph.reaper")
settings = get_settings()


async def lease_reaper_loop() -> None:
    while True:
        try:
            async with async_session_factory() as session:
                tasks = await reap_expired_task_leases(session)
                slots = await reap_expired_slot_leases(session)
                cutoff = await db_now(session) - timedelta(
                    days=settings.event_retention_days
                )
                deleted = await session.execute(
                    delete(WorkspaceEvent).where(
                        WorkspaceEvent.created_at < cutoff
                    )
                )
                await session.commit()
                await purge_expired_idempotency()
                from app.services.directory import sweep_offline

                await sweep_offline()
                from app.services.rights import upkeep as rights_upkeep

                await rights_upkeep()
                from app.services.collab import escalation_upkeep

                async with async_session_factory() as upkeep_session:
                    await escalation_upkeep(upkeep_session)
                purged = await purge_due_tenants(session)
                if purged:
                    logger.info("purged %s tenant(s) after the deletion grace period", purged)
                if tasks or slots:
                    logger.info(
                        "lease reaper reclaimed %s task(s) and %s slot(s)",
                        tasks,
                        slots,
                    )
                if deleted.rowcount:
                    logger.info(
                        "pruned %s old workspace event(s)", deleted.rowcount
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - background resilience
            logger.exception("lease reaper iteration failed")
        await asyncio.sleep(settings.lease_reaper_interval_seconds)
