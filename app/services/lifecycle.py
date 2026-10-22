"""Document lifecycle: superseded, expired, cold (PRD AG-ST-11).

``expires_at`` and ``superseded_by`` are reserved frontmatter fields (checked
on write); ``lifecycle`` is computed and read-only. A document untouched for
90 days and not in the context of an unfinished task is cold; any read or
write makes it active again. Under ``2026-11`` queries leave superseded,
expired and cold documents out unless ``include_lifecycle`` names them, and
count them in ``excluded``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, not_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_permission
from app.database import async_session_factory
from app.errors import ApiError
from app.models import CollabTask, SharedDocument, TaskStatus

logger = logging.getLogger(__name__)
COLD_DAYS = 90
LIFECYCLES = ("active", "superseded", "expired", "cold")
_OPEN = [TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED]


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def validate_frontmatter(frontmatter: dict[str, Any]) -> None:
    """Reserved fields keep their types; lifecycle is the server's."""
    if "lifecycle" in frontmatter:
        raise ApiError(422, "validation_error", "lifecycle is computed by the server",
                       details={"field": "frontmatter.lifecycle"})
    if "expires_at" in frontmatter and frontmatter["expires_at"] is not None \
            and _parse(frontmatter["expires_at"]) is None:
        raise ApiError(422, "validation_error", "expires_at must be an ISO date or time",
                       details={"field": "frontmatter.expires_at"})
    superseded = frontmatter.get("superseded_by")
    if superseded is not None and not (isinstance(superseded, dict)
                                       and isinstance(superseded.get("doc_key"), str)):
        raise ApiError(422, "validation_error",
                       "superseded_by is {namespace, doc_key}",
                       details={"field": "frontmatter.superseded_by"})


def lifecycle_of(document: SharedDocument, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    frontmatter = document.frontmatter or {}
    if frontmatter.get("superseded_by"):
        return "superseded"
    expires = _parse(frontmatter.get("expires_at"))
    if expires is not None and expires <= now:
        return "expired"
    touched = max(filter(None, [document.last_read_at, document.updated_at]),
                  default=None)
    if touched is not None and touched < now - timedelta(days=COLD_DAYS):
        return "cold"
    return "active"


def conditions(include: list[str]) -> dict[str, Any]:
    """SQL conditions per lifecycle (True when the document is in that state)."""
    superseded = SharedDocument.frontmatter.has_key("superseded_by")
    # No expires_at is NULL, not false: coalesce so NOT (... OR expired) holds.
    expired = func.coalesce(func.agentgraph_try_timestamptz(
        SharedDocument.frontmatter["expires_at"].astext) <= func.now(), False)
    referenced = select(CollabTask.id).where(
        CollabTask.workspace_id == SharedDocument.workspace_id,
        CollabTask.status.notin_(_OPEN),
        CollabTask.context_docs.contains(
            func.jsonb_build_array(func.jsonb_build_object("doc_key",
                                                           SharedDocument.doc_key))),
    ).exists()
    cold = and_(func.coalesce(SharedDocument.last_read_at, SharedDocument.updated_at)
                < func.now() - text(f"INTERVAL '{COLD_DAYS} days'"), not_(referenced))
    return {"superseded": superseded, "expired": expired, "cold": cold}


def exclusion(include: list[str]) -> Any | None:
    hidden = [condition for name, condition in conditions(include).items()
              if name not in include]
    return not_(or_(*hidden)) if hidden else None


def touch_read(documents: list[SharedDocument]) -> None:
    """Remember reads once a day (a read makes a cold document active)."""
    stale = [document.id for document in documents
             if document.last_read_at is None
             or document.last_read_at < datetime.now(timezone.utc) - timedelta(days=1)]
    if not stale:
        return
    import asyncio

    async def write() -> None:
        try:
            async with async_session_factory() as session:
                await session.execute(
                    text("UPDATE shared_documents SET last_read_at = NOW() "
                         "WHERE id = ANY(:ids)"), {"ids": stale})
                await session.commit()
        except Exception:  # noqa: BLE001 - bookkeeping must not fail a read
            logger.exception("could not record a document read")

    task = asyncio.get_running_loop().create_task(write())
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    for document in documents:
        if document.id in stale:
            document.last_read_at = datetime.now(timezone.utc)


_pending: set[Any] = set()


async def supersede(session: AsyncSession, principal: Principal, *, old: dict[str, Any],
                    new: dict[str, Any], reason: str) -> dict[str, Any]:
    """Mark ``old`` superseded by ``new``; both get a new version (AG-ST-11 point 2)."""
    from app.schemas import DocumentUpsertRequest
    from app.services.common import append_event
    from app.services.documents import _get_document, upsert_document

    require_permission(principal, "doc:write")
    old_doc = await _get_document(session, principal.workspace.id, old["doc_key"])
    new_doc = await _get_document(session, principal.workspace.id, new["doc_key"])
    if old_doc.id == new_doc.id:
        raise ApiError(422, "validation_error", "a document cannot supersede itself",
                       details={"field": "new"})
    old_ref = {"namespace": old_doc.namespace, "doc_key": old_doc.doc_key}
    new_ref = {"namespace": new_doc.namespace, "doc_key": new_doc.doc_key}
    results = {}
    for document, key, ref in ((old_doc, "superseded_by", new_ref),
                               (new_doc, "supersedes", old_ref)):
        frontmatter = {**(document.frontmatter or {}), key: ref}
        body = DocumentUpsertRequest(content=document.content, title=document.title,
                                     frontmatter=frontmatter,
                                     expected_version=document.version,
                                     namespace=document.namespace)
        result = await upsert_document(session, principal, document.doc_key, body,
                                       commit=False)
        results[key] = result.document.version
    await append_event(session, workspace_id=principal.workspace.id,
                       agent=principal.agent_signature, event_type="DOC_SUPERSEDED",
                       payload={"doc_key": old_doc.doc_key, "id": old_doc.canonical_id,
                                "details": {"superseded_by": new_ref, "reason": reason}},
                       namespace=old_doc.namespace)
    await session.commit()
    return {"old": {**old_ref, "version": results["superseded_by"], "superseded_by": new_ref},
            "new": {**new_ref, "version": results["supersedes"], "supersedes": old_ref}}
