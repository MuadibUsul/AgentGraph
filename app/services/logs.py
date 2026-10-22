"""Append-only log documents (PRD AG-ST-14 points 1-2).

A document created with ``doc_type=log`` takes ``append_entries`` only: no
expected version, no conflicts, a gapless per-document ``seq`` assigned under
the document's row lock. The document's body is re-rendered (its first
version, then the latest 100 entries) and re-indexed once per 20 entries or
5 minutes, so embeddings are computed per batch rather than per append; each
batch is one new document version.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    Principal,
    effective_agent,
    ensure_namespace,
    ensure_not_human_locked,
    require_permission,
)
from app.database import async_session_factory
from app.errors import ApiError, NotFound
from app.models import DocumentVersion, SharedDocument
from app.services import addresses as address_service
from app.services import budget, indexer, provenance
from app.services.common import append_event, utcnow
from app.stamp_context import current_stamp

ENTRIES_MAX = 25
ENTRY_BYTES_MAX = 64 * 1024
READ_LIMIT_MAX = 500
DEREFERENCE_ENTRIES = 100
BATCH_ENTRIES = 20
BATCH_MINUTES = 5


async def _log_document(session: AsyncSession, principal: Principal, namespace: str | None,
                        doc_key: str, *, for_update: bool) -> SharedDocument:
    document = await address_service.find_document(
        session, principal.workspace.id, doc_key, for_update=for_update)
    if document is None or (namespace and document.namespace != namespace):
        raise NotFound("shared_document", f"document '{doc_key}' not found")
    ensure_namespace(principal, document.namespace, write=for_update)
    if document.doc_type != "log":
        raise ApiError(409, "not_a_log_document", "entries belong to log documents only",
                       details={"doc_key": document.doc_key, "doc_type": document.doc_type})
    return document


def _entry_out(row: Any) -> dict[str, Any]:
    return {"entry_id": row["entry_id"], "seq": row["seq"], "body": row["body"],
            "frontmatter": row["frontmatter"], "author": row["author"],
            "tainted": row["tainted"], "created_at": row["created_at"].isoformat()}


async def append_entries(session: AsyncSession, principal: Principal, *, namespace: str | None,
                         doc_key: str, entries: list[Any]) -> dict[str, Any]:
    if not entries or len(entries) > ENTRIES_MAX:
        raise ApiError(422, "validation_error", f"append 1 to {ENTRIES_MAX} entries",
                       details={"field": "entries", "limit": ENTRIES_MAX})
    for index, entry in enumerate(entries):
        if len(entry.body.encode("utf-8")) > ENTRY_BYTES_MAX:
            raise ApiError(413, "content_too_large", "an entry body exceeds 64 KB",
                           details={"index": index, "limit_bytes": ENTRY_BYTES_MAX})
    require_permission(principal, "doc:write")
    agent = effective_agent(principal, None)
    document = await _log_document(session, principal, namespace, doc_key, for_update=True)
    ensure_not_human_locked(principal, "shared_document", document.locked_by_human,
                            document.human_note)
    if document.archived_at is not None:
        raise ApiError(409, "document_archived", "restore the document before appending",
                       details={"doc_key": document.doc_key})
    await budget.check_write(principal, document_id=None, doc_key=document.doc_key)
    origins = await provenance.collect(session, principal, agent=agent, derived_from=[],
                                       content_origin="internal",
                                       exclude_document_id=document.id)
    tainted = bool(origins.sources)
    stamp = current_stamp()
    out = []
    added = 0
    for entry in entries:
        if entry.idempotency_key:
            existing = (await session.execute(
                text("SELECT entry_id, seq FROM log_entries WHERE document_id = :d AND "
                     "idempotency_key = :k"),
                {"d": document.id, "k": entry.idempotency_key})).one_or_none()
            if existing is not None:
                out.append({"entry_id": existing[0], "seq": existing[1], "replayed": True})
                continue
        document.log_seq += 1
        entry_id = await session.scalar(
            text("INSERT INTO log_entries (workspace_id, document_id, seq, body, frontmatter, "
                 "idempotency_key, author, stamp, tainted) VALUES (:ws, :d, :seq, :body, "
                 "CAST(:fm AS jsonb), :k, :author, CAST(:stamp AS jsonb), :tainted) "
                 "RETURNING entry_id"),
            {"ws": document.workspace_id, "d": document.id, "seq": document.log_seq,
             "body": entry.body, "fm": provenance.dumps(entry.frontmatter or {}),
             "k": entry.idempotency_key, "author": agent,
             "stamp": provenance.dumps(stamp) if stamp else None, "tainted": tainted})
        out.append({"entry_id": entry_id, "seq": document.log_seq})
        added += 1
    if added:
        if tainted and not document.tainted:
            document.provenance_taint = provenance.cap_sources(
                list(document.provenance_taint or []) + origins.sources)
            document.tainted = True
            document.trust_level = "UNTRUSTED"
            document.taint_sources = provenance.cap_sources(
                list(document.taint_sources or []) + origins.sources)
        document.log_unindexed += added
        document.log_unindexed_since = document.log_unindexed_since or utcnow()
        document.updated_at = utcnow()
        document.updated_by = agent
        await session.flush()
        await append_event(
            session, workspace_id=document.workspace_id, agent=agent,
            event_type="LOG_APPENDED",
            payload={"doc_key": document.doc_key, "id": document.canonical_id,
                     "details": {"first_seq": out[0]["seq"], "last_seq": document.log_seq,
                                 "count": added}},
            namespace=document.namespace)
        if document.log_unindexed >= BATCH_ENTRIES:
            await rebuild(session, document, author=agent)
    await session.commit()
    return {"entries": out, "doc_version": document.version, "entries_total": document.log_seq}


async def read_entries(session: AsyncSession, principal: Principal, *, namespace: str | None,
                       doc_key: str, after_seq: int | None, limit: int) -> dict[str, Any]:
    require_permission(principal, "doc:read")
    document = await _log_document(session, principal, namespace, doc_key, for_update=False)
    limit = max(1, min(limit, READ_LIMIT_MAX))
    rows = (await session.execute(
        text("SELECT * FROM log_entries WHERE document_id = :d AND seq > :after "
             "ORDER BY seq LIMIT :limit"),
        {"d": document.id, "after": after_seq or 0, "limit": limit})).mappings().all()
    entries = [_entry_out(row) for row in rows]
    await provenance.note_reads(principal, [document])
    return {"doc_key": document.doc_key, "namespace": document.namespace, "entries": entries,
            "entries_total": document.log_seq,
            "next_after_seq": entries[-1]["seq"] if entries else after_seq,
            "has_more": bool(entries) and entries[-1]["seq"] < document.log_seq}


async def latest_entries(session: AsyncSession, document: SharedDocument,
                         limit: int = DEREFERENCE_ENTRIES) -> list[dict[str, Any]]:
    rows = (await session.execute(
        text("SELECT * FROM (SELECT * FROM log_entries WHERE document_id = :d "
             "ORDER BY seq DESC LIMIT :limit) latest ORDER BY seq"),
        {"d": document.id, "limit": limit})).mappings().all()
    return [_entry_out(row) for row in rows]


async def rebuild(session: AsyncSession, document: SharedDocument, *, author: str) -> None:
    """Re-render the body from the latest entries and index it: one batch."""
    header = await session.scalar(
        select(DocumentVersion.content).where(DocumentVersion.document_id == document.id,
                                              DocumentVersion.version == 1))
    entries = await latest_entries(session, document)
    lines = [(header or "").rstrip(), "",
             f"## Entries (latest {len(entries)} of {document.log_seq})", ""]
    lines += [f"- #{entry['seq']} {entry['author']}: {entry['body']}" for entry in entries]
    from app.services.documents import content_hash

    document.content = "\n".join(lines).strip() + "\n"
    document.content_hash = content_hash(document.content)
    document.version += 1
    document.log_unindexed = 0
    document.log_unindexed_since = None
    document.log_index_batches += 1
    await session.flush()
    from app.services.documents import _record_version

    await _record_version(session, document, author=author, batch_id=uuid.uuid4(),
                          change_summary=f"log entries through #{document.log_seq}")
    await indexer.after_write(session, document)


async def rebuild_due() -> int:
    """Reaper: logs whose oldest unindexed entry waited 5 minutes."""
    done = 0
    async with async_session_factory() as session:
        due = (await session.execute(
            select(SharedDocument).where(
                SharedDocument.doc_type == "log", SharedDocument.log_unindexed > 0,
                SharedDocument.log_unindexed_since < utcnow() - timedelta(minutes=BATCH_MINUTES))
            .with_for_update(skip_locked=True).limit(20))).scalars().all()
        for document in due:
            await rebuild(session, document, author="log_indexer")
            done += 1
        await session.commit()
    return done
