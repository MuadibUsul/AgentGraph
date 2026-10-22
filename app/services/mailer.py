"""Outgoing mail through an outbox (incident notices, operator alerts).

Mail is queued in ``email_outbox`` in the caller's transaction and delivered
by ``mail_worker_loop``. Without ``SMTP_HOST`` a message is marked
``no_transport`` so the delivery record says honestly that nothing was sent.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session_factory

logger = logging.getLogger("agentgraph.mailer")
settings = get_settings()
MAX_ATTEMPTS = 5


async def queue_email(
    session: AsyncSession, *, to: str, subject: str, body: str, ref: str | None = None
) -> str:
    return await session.scalar(
        text("INSERT INTO email_outbox (to_email, subject, body, ref) "
             "VALUES (:to, :subject, :body, :ref) RETURNING id"),
        {"to": to, "subject": subject[:512], "body": body, "ref": ref},
    )


def _send(message: EmailMessage) -> None:
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as client:
        if settings.smtp_starttls:
            client.starttls()
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password or "")
        client.send_message(message)


async def deliver_pending(limit: int = 50) -> dict[str, int]:
    """Deliver queued mail once; returns counts by outcome."""
    counts = {"sent": 0, "failed": 0, "no_transport": 0}
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                text("SELECT * FROM email_outbox WHERE status = 'queued' "
                     "ORDER BY created_at LIMIT :limit FOR UPDATE SKIP LOCKED"),
                {"limit": limit},
            )
        ).mappings().all()
        for row in rows:
            status, error = await _deliver(dict(row))
            if status in counts:
                counts[status] += 1
            await session.execute(
                text("UPDATE email_outbox SET status = CAST(:status AS varchar), "
                     "error = :error, attempts = attempts + 1, "
                     "sent_at = CASE WHEN :sent THEN NOW() ELSE sent_at END "
                     "WHERE id = :id"),
                {"id": row["id"], "status": status, "error": error,
                 "sent": status == "sent"},
            )
        await session.commit()
    return counts


async def _deliver(row: dict[str, Any]) -> tuple[str, str | None]:
    if not settings.smtp_host:
        return "no_transport", "SMTP_HOST is not configured"
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = row["to_email"]
    message["Subject"] = row["subject"]
    message.set_content(row["body"])
    try:
        await asyncio.to_thread(_send, message)
    except Exception as exc:  # noqa: BLE001 - recorded, retried
        attempts = row["attempts"] + 1
        return ("failed" if attempts >= MAX_ATTEMPTS else "queued"), str(exc)[:500]
    return "sent", None


async def mail_worker_loop() -> None:
    while True:
        try:
            await deliver_pending()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - background resilience
            logger.exception("mail delivery iteration failed")
        await asyncio.sleep(5)
