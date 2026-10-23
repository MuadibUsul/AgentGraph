"""Asynchronous document indexing (PRD AG-ST-13).

"Stored" and "searchable" are separate states. A write persists the document
and its event, then returns; embedding happens in a background worker. While a
job waits, further writes to the same document only bump ``pending_version``,
so only the latest version is ever embedded (and billed). Failures retry with
exponential backoff and end as ``failed`` with a DOC_INDEX_FAILED event.

Baseline clients (API version 2026-10-21) keep 3.2.1 semantics: the write
embeds synchronously and returns already searchable.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session_factory
from app.errors import ApiError
from app.models import DocumentIndex, SharedDocument
from app.services import budget
from app.services.common import append_event
from app.stamp_context import current_stamp, set_current_stamp
from app.versioning import LATEST_VERSION, at_least
from app.warnings_context import add_warning

logger = logging.getLogger("agentgraph.indexer")
settings = get_settings()


def asynchronous() -> bool:
    return at_least(LATEST_VERSION)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _row(session: AsyncSession, document: SharedDocument) -> DocumentIndex:
    row = await session.get(DocumentIndex, document.id)
    if row is None:
        row = DocumentIndex(document_id=document.id, workspace_id=document.workspace_id)
        session.add(row)
    return row


async def after_write(
    session: AsyncSession, document: SharedDocument, *, defer: bool = False
) -> DocumentIndex:
    """Index now (baseline) or enqueue (2026-11) after a write has been flushed.

    ``defer`` always enqueues: people's console actions (apply a proposal,
    undo a batch) must not wait on the embedding provider, whose latency
    grows with document size and can outlast the console's request timeout.
    With the daily embedding quota spent (enforce mode) the write is still
    stored, and indexing waits for the quota reset (AG-ST-15 point 3).
    """
    row = await _row(session, document)
    if await budget.defer_embedding(document.workspace_id):
        waiting = row.state in ("pending", "deferred") and row.pending_version
        if waiting and row.pending_version < document.version:
            row.superseded += 1
        row.state = "deferred"
        row.pending_version = document.version
        row.attempts = 0
        row.next_attempt_at = budget.next_quota_reset()
        row.enqueued_at = _now()
        row.updated_at = _now()
        add_warning(
            "embedding_deferred_quota",
            "stored; the daily embedding quota is spent, so indexing waits for the reset",
            index_after=row.next_attempt_at.isoformat(),
        )
    elif defer or asynchronous():
        waiting = row.state == "pending" and row.pending_version
        if waiting and row.pending_version < document.version:
            row.superseded += 1  # coalesced: only the latest version is embedded
        row.state = "pending"
        row.pending_version = document.version
        row.attempts = 0
        row.last_error = None
        row.next_attempt_at = _now()
        row.enqueued_at = _now()
        row.updated_at = _now()
    else:
        from app.services.retrieval import reindex_document_chunks

        await reindex_document_chunks(session, document)
        row.state = "indexed"
        row.pending_version = None
        row.indexed_version = document.version
        row.indexed_at = _now()
        row.last_lag_ms = 0
        row.updated_at = _now()
    await session.flush()
    document._index_status = status_of(row, document)
    return row


async def annotate(session: AsyncSession, documents: list[SharedDocument]) -> None:
    """Attach index status to loaded documents for response building."""
    rows = await statuses(session, documents)
    for document in documents:
        document._index_status = status_of(rows.get(document.id), document)


def status_of(row: DocumentIndex | None, document: SharedDocument) -> dict[str, Any]:
    if row is None:
        # Written before v3.13: indexed synchronously at write time.
        return {"index_state": "indexed", "indexed_version": document.version}
    return {"index_state": row.state, "indexed_version": row.indexed_version}


async def statuses(
    session: AsyncSession, documents: list[SharedDocument]
) -> dict[Any, DocumentIndex]:
    ids = [document.id for document in documents]
    if not ids:
        return {}
    rows = (
        await session.execute(select(DocumentIndex).where(DocumentIndex.document_id.in_(ids)))
    ).scalars().all()
    return {row.document_id: row for row in rows}


async def lag(session: AsyncSession, workspace_id) -> dict[str, Any]:
    pending, oldest = (
        await session.execute(
            select(func.count(DocumentIndex.document_id), func.min(DocumentIndex.enqueued_at))
            .where(
                DocumentIndex.workspace_id == workspace_id,
                DocumentIndex.state.in_(("pending", "deferred")),
            )
        )
    ).one()
    age = int((_now() - oldest).total_seconds() * 1000) if oldest else 0
    return {"pending_jobs": int(pending or 0), "oldest_pending_age_ms": age}


async def index_status(
    session: AsyncSession, workspace_id, namespace: str | None = None
) -> dict[str, Any]:
    base = select(DocumentIndex).join(
        SharedDocument, SharedDocument.id == DocumentIndex.document_id
    ).where(DocumentIndex.workspace_id == workspace_id)
    if namespace:
        base = base.where(SharedDocument.namespace == namespace)
    rows = (await session.execute(base)).scalars().all()
    now = _now()
    pending = [row for row in rows if row.state in ("pending", "deferred")]
    recent = sorted(
        row.last_lag_ms for row in rows
        if row.indexed_at and row.indexed_at >= now - timedelta(hours=1)
        and row.last_lag_ms is not None
    )
    p95 = recent[min(len(recent) - 1, int(len(recent) * 0.95))] if recent else 0
    oldest = min((row.enqueued_at for row in pending if row.enqueued_at), default=None)
    return {
        "namespace": namespace,
        "pending_jobs": sum(1 for row in rows if row.state == "pending"),
        "deferred_jobs": sum(1 for row in rows if row.state == "deferred"),
        "failed_jobs": sum(1 for row in rows if row.state == "failed"),
        "oldest_pending_age_ms": int((now - oldest).total_seconds() * 1000) if oldest else 0,
        "p95_lag_ms_1h": p95,
        "superseded_total": sum(row.superseded for row in rows),
        "repairable": await repairable(session, workspace_id),
    }


def _repair_condition() -> Any:
    """Documents whose index is wrong: missing, failed, behind, or another model.

    Queued documents (pending/deferred) are not repairable; they are on their
    way. Archived documents are left alone.
    """
    from app.engine.embeddings import get_embedding_provider
    from app.models import DocumentChunk

    model = get_embedding_provider().model
    has_chunks = (
        select(DocumentChunk.id).where(DocumentChunk.document_id == SharedDocument.id).exists()
    )
    queued = DocumentIndex.state.in_(("pending", "deferred"))
    return and_(
        SharedDocument.archived_at.is_(None),
        or_(DocumentIndex.document_id.is_(None), not_(queued)),
        or_(
            DocumentIndex.state == "failed",
            DocumentIndex.indexed_version < SharedDocument.version,
            and_(DocumentIndex.document_id.is_(None), not_(has_chunks)),
            func.coalesce(SharedDocument.embedding_model, "") != model,
            and_(func.length(SharedDocument.content) > 0, not_(has_chunks)),
        ),
    )


def _repair_query(workspace_id) -> Any:
    return (
        select(SharedDocument.id)
        .outerjoin(DocumentIndex, DocumentIndex.document_id == SharedDocument.id)
        .where(SharedDocument.workspace_id == workspace_id, _repair_condition())
    )


async def repairable(session: AsyncSession, workspace_id) -> int:
    return int(await session.scalar(
        select(func.count()).select_from(_repair_query(workspace_id).subquery())
    ) or 0)


async def repair(session: AsyncSession, workspace_id, *, mode: str = "repair",
                 confirm: str | None = None) -> dict[str, Any]:
    """Queue documents for re-indexing; the worker embeds them one by one.

    ``repair`` (the default) queues only documents whose index is wrong, so a
    healthy workspace costs nothing. ``full`` queues every live document and
    needs ``confirm="REBUILD"``. Each document is indexed and committed on its
    own by the worker, so an interrupted rebuild resumes where it stopped.
    """
    if mode not in ("repair", "full"):
        raise ApiError(422, "validation_error", "mode is repair or full",
                       details={"field": "mode", "allowed": ["repair", "full"]})
    if mode == "full" and confirm != "REBUILD":
        raise ApiError(409, "confirmation_required",
                       "a full rebuild re-embeds every document; confirm with REBUILD",
                       details={"confirm": "REBUILD"})
    query = (
        _repair_query(workspace_id) if mode == "repair"
        else select(SharedDocument.id).where(SharedDocument.workspace_id == workspace_id,
                                             SharedDocument.archived_at.is_(None))
    )
    ids = (await session.execute(query.order_by(SharedDocument.id))).scalars().all()
    for document_id in ids:
        document = await session.get(SharedDocument, document_id)
        await after_write(session, document, defer=True)
    await session.commit()
    status = await index_status(session, workspace_id)
    return {"mode": mode, "queued": len(ids), "pending_jobs": status["pending_jobs"],
            "deferred_jobs": status["deferred_jobs"], "failed_jobs": status["failed_jobs"]}


async def process_jobs(limit: int | None = None) -> int:
    """Index due documents. Returns how many were indexed in this pass."""
    from app.services.retrieval import reindex_document_chunks

    done = 0
    async with async_session_factory() as session:
        # Fair across shards (AG-EV-02 point 6): the oldest job of every
        # (workspace, namespace) first, then the second of each, and so on.
        rank = func.row_number().over(
            partition_by=(DocumentIndex.workspace_id, SharedDocument.namespace),
            order_by=DocumentIndex.next_attempt_at,
        ).label("rank")
        ranked = (
            select(DocumentIndex.document_id, rank, DocumentIndex.next_attempt_at)
            .join(SharedDocument, SharedDocument.id == DocumentIndex.document_id)
            .where(
                DocumentIndex.state.in_(("pending", "deferred")),
                DocumentIndex.next_attempt_at <= _now(),
            )
            .subquery()
        )
        picked = (
            select(ranked.c.document_id)
            .order_by(ranked.c.rank, ranked.c.next_attempt_at)
            .limit(limit or settings.index_worker_batch)
        )
        rows = (
            await session.execute(
                select(DocumentIndex)
                .where(DocumentIndex.document_id.in_(picked))
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        jobs = [(row.document_id, row.workspace_id) for row in rows]
        await session.commit()
    for document_id, workspace_id in jobs:
        try:
            if await _index_one(document_id, workspace_id, reindex_document_chunks):
                done += 1
        except Exception:  # noqa: BLE001 - one bad job must not stop the worker
            logger.exception("index job for %s crashed", document_id)
    return done


# Who pays for rebuilding an index that already existed (see _index_one).
REINDEX_STAMP: dict = {
    "kind": "system",
    "principal_id": "system:reindex",
    "token_id": None,
    "chain": ["system:reindex"],
}

async def _index_one(document_id, workspace_id, reindex) -> bool:
    previous_stamp = current_stamp()
    async with async_session_factory() as session:
        row = await session.scalar(
            select(DocumentIndex)
            .where(DocumentIndex.document_id == document_id)
            .with_for_update(skip_locked=True)
        )
        if row is None or row.state not in ("pending", "deferred"):
            return False
        document = await session.get(SharedDocument, document_id)
        if document is None:
            await session.delete(row)
            await session.commit()
            return False
        from app.services.token_budget import stamp_chain

        # A version that was already indexed is being rebuilt (workspace
        # reindex, embedding model or dimension change): the platform pays,
        # not whoever last wrote the document, possibly days ago.
        rebuild = row.indexed_version is not None and row.indexed_version >= document.version
        stamp = REINDEX_STAMP if rebuild else document.updated_stamp
        if await budget.defer_embedding(workspace_id, stamp_chain(stamp)):
            row.state = "deferred"
            row.next_attempt_at = budget.next_quota_reset()
            row.updated_at = _now()
            await session.commit()
            return False
        version = document.version
        snapshot = {
            "workspace_id": document.workspace_id,
            "doc_key": document.doc_key,
            "id": document.canonical_id,
            "version": version,
            "namespace": document.namespace,
            "updated_by": document.updated_by,
        }
        # Bill a first indexing to the writer of the version being indexed.
        set_current_stamp(stamp)
        try:
            await reindex(session, document)
        except Exception as exc:  # noqa: BLE001 - provider boundary
            await session.rollback()  # expires `document`; use the snapshot
            await _record_failure(document_id, snapshot, exc)
            return False
        finally:
            set_current_stamp(previous_stamp)
        row.indexed_version = version
        row.indexed_at = _now()
        row.last_lag_ms = (
            int((_now() - row.enqueued_at).total_seconds() * 1000) if row.enqueued_at else 0
        )
        row.attempts = 0
        row.last_error = None
        if (row.pending_version or 0) > version:
            row.state = "pending"  # a newer write arrived while we embedded
        else:
            row.state = "indexed"
            row.pending_version = None
        row.updated_at = _now()
        await append_event(
            session,
            workspace_id=workspace_id,
            agent="indexer",
            event_type="DOC_INDEXED",
            payload={
                "doc_key": document.doc_key,
                "id": document.canonical_id,
                "refs": {"version": version},
                "lag_ms": row.last_lag_ms,
            },
            namespace=document.namespace,
        )
        await session.commit()
        return True


async def _record_failure(document_id, document: dict[str, Any], exc: Exception) -> None:
    async with async_session_factory() as session:
        row = await session.get(DocumentIndex, document_id, with_for_update=True)
        if row is None:
            return
        row.attempts += 1
        row.last_error = str(exc)[:2_000]
        row.updated_at = _now()
        if row.attempts >= settings.index_max_attempts:
            row.state = "failed"
            row.next_attempt_at = None
            await append_event(
                session,
                workspace_id=document["workspace_id"],
                agent="indexer",
                event_type="DOC_INDEX_FAILED",
                payload={
                    "doc_key": document["doc_key"],
                    "id": document["id"],
                    "version": document["version"],
                    "error": row.last_error,
                    "targets": [document["updated_by"]],
                },
                namespace=document["namespace"],
            )
        else:
            row.next_attempt_at = _now() + timedelta(seconds=2 ** row.attempts)
        await session.commit()


async def retry_failed(session: AsyncSession, workspace_id, document_id) -> None:
    row = await session.get(DocumentIndex, document_id)
    if row is not None and row.workspace_id == workspace_id and row.state == "failed":
        row.state = "pending"
        row.attempts = 0
        row.next_attempt_at = _now()
        await session.commit()


async def index_worker_loop() -> None:
    while True:
        try:
            if settings.index_worker_enabled:
                await process_jobs()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - background resilience
            logger.exception("index worker iteration failed")
        await asyncio.sleep(settings.index_worker_interval_seconds)
