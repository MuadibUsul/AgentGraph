"""Platform alerts for the operator (restore drill failures, incident SLA checkpoints).

Alerts are platform rows, never tenant data. ``raise_alert`` with a
``dedupe_key`` is idempotent, so a checkpoint loop can call it every
iteration. When ``PLATFORM_ADMIN_EMAIL`` is set, new warning and critical
alerts are also mailed.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from app.config import get_settings
from app.database import async_session_factory

settings = get_settings()


def alert_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "alert_id": row["id"],
        "kind": row["kind"],
        "severity": row["severity"],
        "message": row["message"],
        "details": row["details"],
        "created_at": row["created_at"],
        "acknowledged_at": row["acknowledged_at"],
    }


async def raise_alert(
    kind: str,
    message: str,
    *,
    severity: str = "warning",
    details: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
) -> dict[str, Any] | None:
    """Record an alert; returns None when the dedupe key was already raised."""
    async with async_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "INSERT INTO platform_alerts (kind, severity, message, details, dedupe_key) "
                    "VALUES (:kind, :severity, :message, CAST(:details AS jsonb), :dedupe) "
                    "ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING "
                    "RETURNING *"
                ),
                {"kind": kind, "severity": severity, "message": message,
                 "details": json.dumps(details or {}, default=str), "dedupe": dedupe_key},
            )
        ).mappings().one_or_none()
        if row is not None and severity != "info" and settings.platform_admin_email:
            from app.services.mailer import queue_email

            await queue_email(
                session,
                to=settings.platform_admin_email,
                subject=f"[AgentGraph {severity}] {message[:180]}",
                body=f"{message}\n\n{json.dumps(details or {}, indent=2, default=str)}",
                ref=row["id"],
            )
        await session.commit()
    return alert_out(dict(row)) if row is not None else None


async def list_alerts(*, open_only: bool = False, kind: str | None = None,
                      limit: int = 100) -> dict[str, Any]:
    where = ["TRUE"]
    params: dict[str, Any] = {"limit": limit}
    if open_only:
        where.append("acknowledged_at IS NULL")
    if kind:
        where.append("kind = :kind")
        params["kind"] = kind
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                text(f"SELECT * FROM platform_alerts WHERE {' AND '.join(where)} "
                     "ORDER BY created_at DESC, id DESC LIMIT :limit"),
                params,
            )
        ).mappings().all()
    return {"items": [alert_out(dict(row)) for row in rows]}


async def acknowledge(alert_id: str) -> dict[str, Any] | None:
    async with async_session_factory() as session:
        row = (
            await session.execute(
                text("UPDATE platform_alerts SET acknowledged_at = COALESCE(acknowledged_at, "
                     "NOW()) WHERE id = :id RETURNING *"),
                {"id": alert_id},
            )
        ).mappings().one_or_none()
        await session.commit()
    return alert_out(dict(row)) if row else None
