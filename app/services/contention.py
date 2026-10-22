"""Hot-document protection and section hashes (PRD AG-ST-14 point 3).

Five or more version conflicts on a document within 10 seconds make it
contended for 60 seconds; meanwhile whole-document overwrites get 409
``contention`` with a suggestion (``patch_doc_section`` or ``append_entries``)
and ``Retry-After``. Section patches carry ``section_hash`` instead of the
whole document's version, so edits to different sections both succeed.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.engine.chunking import extract_section
from app.errors import ApiError
from app.models import SharedDocument
from app.services.common import append_event

logger = logging.getLogger(__name__)
WINDOW_SECONDS = 10
THRESHOLD = 5
CONTENDED_SECONDS = 60
RETRY_AFTER_MS = 2_000
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def section_hash(body: str | None) -> str:
    """sha256 of a section's body (heading excluded, surrounding blanks stripped)."""
    return hashlib.sha256((body or "").strip().encode("utf-8")).hexdigest()


def list_sections(content: str) -> list[dict[str, Any]]:
    sections = []
    for line in content.splitlines():
        match = _HEADING.match(line)
        if match:
            heading = match.group(2).strip()
            sections.append({"heading": heading, "level": len(match.group(1)),
                             "section_hash": section_hash(extract_section(content, heading))})
    return sections


async def check(session: AsyncSession, document: SharedDocument) -> None:
    """Refuse a whole-document overwrite while the document is contended."""
    row = (await session.execute(
        text("SELECT contended_until, conflicts FROM doc_contention WHERE document_id = :d"),
        {"d": document.id})).one_or_none()
    now = datetime.now(timezone.utc)
    if row is None or row[0] is None or row[0] <= now:
        return
    recent = [moment for moment in row[1] or [] if (now - moment).total_seconds()
              <= WINDOW_SECONDS]
    suggest = "append_entries" if document.doc_type == "log" else "patch_doc_section"
    raise ApiError(
        409, "contention",
        "this document is contended; patch a section or retry later",
        details={"doc_key": document.doc_key, "namespace": document.namespace,
                 "conflicts_10s": len(recent), "contended_until": row[0].isoformat(),
                 "suggest": suggest, "retry_after_ms": RETRY_AFTER_MS},
        headers={"Retry-After": str(RETRY_AFTER_MS // 1000)},
    )


async def note_conflict(document: SharedDocument) -> None:
    """Count one version conflict; never fails the request that hit it."""
    try:
        async with async_session_factory() as session:
            row = (await session.execute(
                text("INSERT INTO doc_contention (document_id, workspace_id, conflicts, "
                     "conflicts_total, updated_at) VALUES (:d, :ws, ARRAY[NOW()], 1, NOW()) "
                     "ON CONFLICT (document_id) DO UPDATE SET conflicts = ARRAY(SELECT c FROM "
                     "unnest(doc_contention.conflicts) c WHERE c > NOW() - make_interval("
                     "secs => :window)) || NOW(), conflicts_total = "
                     "doc_contention.conflicts_total + 1, updated_at = NOW() "
                     "RETURNING cardinality(conflicts), contended_until"),
                {"d": document.id, "ws": document.workspace_id, "window": WINDOW_SECONDS},
            )).one()
            count, until = row
            now = datetime.now(timezone.utc)
            if count >= THRESHOLD and (until is None or until <= now):
                await session.execute(
                    text("UPDATE doc_contention SET contended_until = NOW() + make_interval("
                         "secs => :s) WHERE document_id = :d"),
                    {"s": CONTENDED_SECONDS, "d": document.id})
                await append_event(
                    session, workspace_id=document.workspace_id, agent="contention",
                    event_type="DOC_CONTENDED",
                    payload={"doc_key": document.doc_key, "id": document.canonical_id,
                             "details": {"conflicts_10s": count,
                                         "contended_seconds": CONTENDED_SECONDS}},
                    namespace=document.namespace)
            await session.commit()
    except Exception:  # noqa: BLE001 - bookkeeping must not change the response
        logger.exception("could not record a version conflict")


async def state(session: AsyncSession, document_id: Any) -> dict[str, Any]:
    row = (await session.execute(
        text("SELECT conflicts, conflicts_total, contended_until FROM doc_contention "
             "WHERE document_id = :d"), {"d": document_id})).one_or_none()
    now = datetime.now(timezone.utc)
    if row is None:
        return {"conflicts_10s": 0, "conflicts_total": 0, "contended": False}
    return {"conflicts_10s": len([moment for moment in row[0] or []
                                  if (now - moment).total_seconds() <= WINDOW_SECONDS]),
            "conflicts_total": row[1],
            "contended": bool(row[2] and row[2] > now),
            "contended_until": row[2].isoformat() if row[2] else None}
