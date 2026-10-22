"""Per-token access log: who touched which workspace, when, and what they read.

Writes are already traceable through lineage stamps (AG-ID-03); reads leave
no trace in the data. Incident scope resolution (AG-TC-06) needs both, so
every authenticated request is counted per workspace, principal, token and
minute, together with the documents and tasks it returned (capped per
bucket). Counts are buffered in memory and flushed in batches, so a request
costs no extra write; ``flush`` runs on a short interval and before every
scope resolution.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from sqlalchemy import text

from app.config import get_settings
from app.database import async_session_factory

logger = logging.getLogger("agentgraph.access_log")
settings = get_settings()

# (workspace_id, principal_id, token_id, bucket) -> counters
_Key = tuple[str, str, str, datetime]
_buffer: dict[_Key, dict[str, Any]] = {}
_current: ContextVar[_Key | None] = ContextVar("agentgraph_access_key", default=None)
_lock = asyncio.Lock()


def _bucket(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(second=0, microsecond=0)


def _entry(key: _Key) -> dict[str, Any]:
    entry = _buffer.get(key)
    if entry is None:
        entry = _buffer[key] = {"requests": 0, "writes": 0, "documents": set(), "tasks": set()}
    return entry


def note_request(workspace_id: Any, stamp: dict[str, Any] | None, *, write: bool) -> None:
    """Count one authenticated request (REST or MCP)."""
    stamp = stamp or {}
    principal_id = str(stamp.get("principal_id") or stamp.get("agent_id") or "unknown")
    key = (str(workspace_id), principal_id[:256], str(stamp.get("token_id") or "")[:40],
           _bucket())
    entry = _entry(key)
    entry["requests"] += 1
    entry["writes"] += int(write)
    _current.set(key)


def note_objects(kind: str, ids: Iterable[Any]) -> None:
    key = _current.get()
    if key is None:
        return
    target = _entry(key)["documents" if kind == "document" else "tasks"]
    for value in ids:
        if value is None or len(target) >= settings.access_log_max_objects:
            break
        target.add(str(value))


def _ids_of(result: Any) -> list[Any]:
    """Canonical ids in a service result: one object, or a page of them."""

    def one(item: Any) -> Any:
        if isinstance(item, dict):
            return item.get("canonical_id") or item.get("id")
        return getattr(item, "canonical_id", None) or getattr(item, "id", None)

    found: list[Any] = []
    for name in ("document", "task"):
        item = result.get(name) if isinstance(result, dict) else getattr(result, name, None)
        if item is not None:
            found.append(one(item))
    for name in ("items", "results", "hits", "documents"):
        page = result.get(name) if isinstance(result, dict) else getattr(result, name, None)
        if isinstance(page, list):
            found.extend(one(item) for item in page[: settings.access_log_max_objects])
    if not found:
        found.append(one(result))
    return [value for value in found if value is not None]


def reads(kind: str):
    """Decorate a service read so the returned objects are logged."""

    def decorate(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            result = await fn(*args, **kwargs)
            try:
                note_objects(kind, _ids_of(result))
            except Exception:  # pragma: no cover - logging never fails a read
                logger.debug("access log note failed", exc_info=True)
            return result

        return wrapper

    return decorate


async def flush() -> int:
    """Write buffered counters; returns the number of rows upserted."""
    async with _lock:
        if not _buffer:
            return 0
        pending = dict(_buffer)
        _buffer.clear()
        cap = settings.access_log_max_objects
        try:
            async with async_session_factory() as session:
                for (workspace, principal, token, bucket), entry in pending.items():
                    await session.execute(
                        text(
                            "INSERT INTO token_access_log AS t (workspace_id, principal_id, "
                            "token_id, bucket, requests, writes, documents, tasks) VALUES "
                            "(CAST(:ws AS uuid), :principal, :token, :bucket, :requests, "
                            ":writes, CAST(:documents AS text[]), CAST(:tasks AS text[])) "
                            "ON CONFLICT (workspace_id, principal_id, token_id, bucket) DO UPDATE "
                            "SET requests = t.requests + EXCLUDED.requests, "
                            "writes = t.writes + EXCLUDED.writes, "
                            "documents = ARRAY(SELECT DISTINCT x FROM unnest(t.documents || "
                            "EXCLUDED.documents) AS x LIMIT :cap), "
                            "tasks = ARRAY(SELECT DISTINCT x FROM unnest(t.tasks || "
                            "EXCLUDED.tasks) AS x LIMIT :cap)"
                        ),
                        {"ws": workspace, "principal": principal, "token": token,
                         "bucket": bucket, "requests": entry["requests"],
                         "writes": entry["writes"], "documents": sorted(entry["documents"]),
                         "tasks": sorted(entry["tasks"]), "cap": cap},
                    )
                await session.commit()
        except Exception:
            # Keep the counts for the next attempt rather than losing them.
            for key, entry in pending.items():
                merged = _entry(key)
                merged["requests"] += entry["requests"]
                merged["writes"] += entry["writes"]
                merged["documents"] |= entry["documents"]
                merged["tasks"] |= entry["tasks"]
            raise
        return len(pending)


async def flush_loop() -> None:
    while True:
        await asyncio.sleep(settings.access_log_flush_seconds)
        try:
            await flush()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - background resilience
            logger.exception("access log flush failed")
