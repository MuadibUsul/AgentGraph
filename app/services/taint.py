"""Prompt-injection taint provenance and downstream propagation."""

from __future__ import annotations

import json
import re
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import (
    CollabTask,
    ContextSlot,
    SharedDocument,
    TaintEdge,
)
from app.services.provenance import cap_sources

settings = get_settings()

_DOC_URI = re.compile(r"doc://([A-Za-z0-9][A-Za-z0-9_.:/-]*)")
_EXTERNAL = re.compile(r"^(?:https?|external|untrusted)://", re.IGNORECASE)
_SOURCE_KEYS = (
    "sources",
    "source_documents",
    "derived_from",
    "doc_refs",
    "references",
    "source_doc_keys",
)


def _flatten_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for key in ("doc_key", "uri", "url", "source", "id"):
            item = value.get(key)
            if isinstance(item, str):
                result.append(item)
        return result
    if isinstance(value, (list, tuple, set)):
        result = []
        for item in value:
            result.extend(_flatten_strings(item))
        return result
    return []


def extract_references(content: str, frontmatter: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Return (document keys, external URIs) referenced by a document."""
    keys: set[str] = set()
    external: set[str] = set()
    for match in _DOC_URI.finditer(content or ""):
        keys.add(match.group(1).rstrip("/"))
    for source_key in _SOURCE_KEYS:
        for raw in _flatten_strings((frontmatter or {}).get(source_key)):
            raw = raw.strip()
            if not raw:
                continue
            if raw.startswith("doc://"):
                raw = raw[6:]
            if _EXTERNAL.match(raw):
                external.add(raw)
            else:
                keys.add(raw.rstrip("/"))
    if (frontmatter or {}).get("tainted") is True:
        external.add("frontmatter:tainted")
    if (frontmatter or {}).get("prompt_injection") is True:
        external.add("frontmatter:prompt_injection")
    return keys, external


async def compute_document_taint(
    session: AsyncSession,
    document: SharedDocument,
) -> tuple[bool, list[dict[str, Any]]]:
    keys, external = extract_references(
        document.content, document.frontmatter or {}
    )
    # Write-time provenance (AG-TC-01) comes first and survives recomputation.
    sources: list[dict[str, Any]] = [
        dict(entry) for entry in (document.provenance_taint or []) if isinstance(entry, dict)
    ]
    for uri in sorted(external):
        sources.append(
            {
                "uri": uri,
                "reason": "external_or_explicit_untrusted_source",
                "depth": 1,
                "verified": False,
            }
        )
    if keys:
        docs = (
            await session.execute(
                select(SharedDocument).where(
                    SharedDocument.workspace_id == document.workspace_id,
                    SharedDocument.doc_key.in_(list(keys)),
                )
            )
        ).scalars().all()
        for source in docs:
            if source.id == document.id:
                continue
            source_tainted = bool(source.tainted)
            source_untrusted = (
                settings.taint_untrusted_source_default
                and source.trust_level == "UNTRUSTED"
                and source.verification_status != "VERIFIED"
            )
            if source_tainted or source_untrusted:
                sources.append(
                    {
                        "doc_key": source.doc_key,
                        "doc_id": str(source.id),
                        "title": source.title,
                        "reason": (
                            "tainted_source" if source_tainted else "untrusted_source"
                        ),
                        "depth": 1,
                        "verification_status": source.verification_status,
                    }
                )
                if source_tainted:
                    for inherited in (source.taint_sources or [])[:20]:
                        if not isinstance(inherited, dict):
                            continue
                        entry = dict(inherited)
                        entry["depth"] = int(entry.get("depth") or 1) + 1
                        entry["via"] = source.doc_key
                        sources.append(entry)
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in sources:
        identity = (
            entry.get("doc_key")
            or entry.get("uri")
            or entry.get("doc_id")
            or json.dumps(entry, sort_keys=True, default=str)
        )
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(entry)
    return bool(deduped), cap_sources(deduped)


async def _replace_edges(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
    source_ids: set[uuid.UUID],
    reasons: dict[str, str] | None = None,
) -> None:
    await session.execute(
        delete(TaintEdge).where(
            TaintEdge.workspace_id == workspace_id,
            TaintEdge.target_type == target_type,
            TaintEdge.target_id == target_id,
        )
    )
    for source_id in source_ids:
        session.add(
            TaintEdge(
                workspace_id=workspace_id,
                source_doc_id=source_id,
                target_type=target_type,
                target_id=target_id,
                reason=(reasons or {}).get(str(source_id), "source"),
                depth=1,
            )
        )
    await session.flush()


async def refresh_document_taint(
    session: AsyncSession,
    document: SharedDocument,
) -> bool:
    tainted, sources = await compute_document_taint(session, document)
    document.tainted = tainted
    document.taint_sources = sources
    if tainted:
        document.trust_level = "UNTRUSTED"
    elif document.verification_status == "VERIFIED":
        document.trust_level = "TRUSTED"
    elif document.created_by != "human_commander":
        document.trust_level = "UNTRUSTED"
    source_ids = {
        uuid.UUID(str(item["doc_id"]))
        for item in sources
        if item.get("doc_id")
    }
    await _replace_edges(
        session,
        workspace_id=document.workspace_id,
        target_type="document",
        target_id=document.id,
        source_ids=source_ids,
        reasons={
            str(item["doc_id"]): str(item.get("reason") or "source")
            for item in sources
            if item.get("doc_id")
        },
    )
    return tainted


async def recompute_downstream_document_taint(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    source_doc_id: uuid.UUID,
) -> None:
    """Clear taint from documents whose verified sources just became trusted.

    Only documents that become untainted are traversed further; a target that
    remains tainted by another source keeps its downstream tainted.
    """
    queue: deque[uuid.UUID] = deque([source_doc_id])
    seen: set[uuid.UUID] = set()
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        target_ids = (
            await session.execute(
                select(TaintEdge.target_id).where(
                    TaintEdge.workspace_id == workspace_id,
                    TaintEdge.source_doc_id == current,
                    TaintEdge.target_type == "document",
                )
            )
        ).scalars().all()
        for target_id in target_ids:
            if target_id in seen:
                continue
            document = await session.get(SharedDocument, target_id)
            if document is None:
                continue
            still_tainted = await refresh_document_taint(session, document)
            if not still_tainted:
                queue.append(target_id)


def _iter_uri_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(_iter_uri_strings(item))
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            result.extend(_iter_uri_strings(item))
        return result
    return []


async def refresh_resource_taint(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
    values: Any,
    frontmatter: dict[str, Any] | None = None,
) -> bool:
    """Taint a slot/task from doc:// references in its state or input context."""
    keys: set[str] = set()
    external: set[str] = set()
    for text in _iter_uri_strings(values):
        content_keys, content_external = extract_references(
            text, frontmatter or {}
        )
        keys.update(content_keys)
        external.update(content_external)
    docs = (
        await session.execute(
            select(SharedDocument).where(
                SharedDocument.workspace_id == workspace_id,
                SharedDocument.doc_key.in_(list(keys)),
            )
        )
    ).scalars().all() if keys else []
    sources: list[dict[str, Any]] = [
        {"uri": uri, "reason": "external_source", "depth": 1}
        for uri in sorted(external)
    ]
    source_ids: set[uuid.UUID] = set()
    for doc in docs:
        if doc.tainted:
            sources.append(
                {
                    "doc_key": doc.doc_key,
                    "doc_id": str(doc.id),
                    "reason": "tainted_source",
                    "depth": 1,
                }
            )
            source_ids.add(doc.id)
    await _replace_edges(
        session,
        workspace_id=workspace_id,
        target_type=target_type,
        target_id=target_id,
        source_ids=source_ids,
    )
    return bool(sources)


async def refresh_slot_taint(session: AsyncSession, slot: ContextSlot) -> bool:
    tainted = await refresh_resource_taint(
        session,
        workspace_id=slot.workspace_id,
        target_type="slot",
        target_id=slot.id,
        values=slot.current_state or {},
    )
    slot.tainted = tainted
    if not tainted:
        slot.taint_sources = []
        return False
    _, sources = await _resource_sources(
        session, slot.workspace_id, slot.current_state or {}
    )
    slot.taint_sources = sources
    return True


async def refresh_task_taint(session: AsyncSession, task: CollabTask) -> bool:
    tainted = await refresh_resource_taint(
        session,
        workspace_id=task.workspace_id,
        target_type="task",
        target_id=task.id,
        values=task.input_context or {},
    )
    task.tainted = tainted
    if not tainted:
        task.taint_sources = []
        return False
    _, sources = await _resource_sources(
        session, task.workspace_id, task.input_context or {}
    )
    task.taint_sources = sources
    return True


async def _resource_sources(
    session: AsyncSession, workspace_id: uuid.UUID, values: Any
) -> tuple[bool, list[dict[str, Any]]]:
    keys: set[str] = set()
    external: set[str] = set()
    for text in _iter_uri_strings(values):
        content_keys, content_external = extract_references(text, {})
        keys.update(content_keys)
        external.update(content_external)
    docs = (
        await session.execute(
            select(SharedDocument).where(
                SharedDocument.workspace_id == workspace_id,
                SharedDocument.doc_key.in_(list(keys)),
            )
        )
    ).scalars().all() if keys else []
    sources: list[dict[str, Any]] = [
        {"uri": uri, "reason": "external_source", "depth": 1}
        for uri in sorted(external)
    ]
    for doc in docs:
        if doc.tainted:
            sources.append(
                {
                    "doc_key": doc.doc_key,
                    "doc_id": str(doc.id),
                    "reason": "tainted_source",
                    "depth": 1,
                }
            )
    return bool(sources), sources[:50]


async def propagate_from_document(
    session: AsyncSession,
    document: SharedDocument,
    *,
    max_depth: int | None = None,
) -> None:
    """Recompute taint for all resources downstream of a changed source doc."""
    limit = max_depth or settings.taint_max_depth
    queue: deque[tuple[uuid.UUID, int]] = deque([(document.id, 0)])
    visited: set[uuid.UUID] = {document.id}
    processed = 0
    while queue and processed < 500:
        source_id, depth = queue.popleft()
        if depth >= limit:
            continue
        edges = (
            await session.execute(
                select(TaintEdge).where(
                    TaintEdge.workspace_id == document.workspace_id,
                    TaintEdge.source_doc_id == source_id,
                )
            )
        ).scalars().all()
        for edge in edges:
            processed += 1
            if processed > 500:
                break
            if edge.target_type == "document":
                target = await session.get(SharedDocument, edge.target_id)
                if target is None or target.id in visited:
                    continue
                visited.add(target.id)
                await refresh_document_taint(session, target)
                queue.append((target.id, depth + 1))
            elif edge.target_type == "slot":
                target_slot = await session.get(ContextSlot, edge.target_id)
                if target_slot is not None:
                    await refresh_slot_taint(session, target_slot)
            elif edge.target_type == "task":
                target_task = await session.get(CollabTask, edge.target_id)
                if target_task is not None:
                    await refresh_task_taint(session, target_task)


async def clear_document_taint(
    session: AsyncSession,
    document: SharedDocument,
    *,
    actor: str,
    note: str | None = None,
) -> None:
    document.tainted = False
    document.taint_sources = []
    document.provenance_taint = []
    document.recall_severity = None
    document.trust_level = "TRUSTED"
    document.taint_cleared_at = datetime.now(timezone.utc)
    document.taint_cleared_by = actor
    if note:
        document.human_note = note
    await session.execute(
        delete(TaintEdge).where(
            TaintEdge.workspace_id == document.workspace_id,
            TaintEdge.target_type == "document",
            TaintEdge.target_id == document.id,
        )
    )
    await session.flush()
    await propagate_from_document(session, document)


async def mark_document_taint(
    session: AsyncSession,
    document: SharedDocument,
    *,
    tainted: bool,
    sources: list[Any],
    actor: str,
    note: str | None = None,
) -> None:
    if not tainted:
        await clear_document_taint(
            session, document, actor=actor, note=note
        )
        return
    document.tainted = True
    document.trust_level = "UNTRUSTED"
    document.taint_sources = sources or [
        {"reason": "manual_human_mark", "by": actor}
    ]
    doc_keys: set[str] = set()
    for entry in sources or []:
        for raw in _flatten_strings(entry):
            if raw.startswith("doc://"):
                raw = raw[6:]
            if not _EXTERNAL.match(raw):
                doc_keys.add(raw.rstrip("/"))
    docs = (
        await session.execute(
            select(SharedDocument).where(
                SharedDocument.workspace_id == document.workspace_id,
                SharedDocument.doc_key.in_(list(doc_keys)),
            )
        )
    ).scalars().all() if doc_keys else []
    await _replace_edges(
        session,
        workspace_id=document.workspace_id,
        target_type="document",
        target_id=document.id,
        source_ids={doc.id for doc in docs},
        reasons={str(doc.id): "manual_taint_source" for doc in docs},
    )
    document.taint_cleared_at = None
    document.taint_cleared_by = None
    if note:
        document.human_note = note
    await session.flush()
    await propagate_from_document(session, document)
