"""Situation awareness: per-principal metrics and anomaly alerts (PRD AG-TC-05).

Every authenticated request is counted per workspace, principal and minute
(requests, reads, writes, no-op writes, 409s, 429s, 5xx), buffered in memory
and flushed every few seconds. A loop evaluates the anomaly rules once a
minute; findings become ``situation_alerts`` (one live alert per rule and
subject), high-severity ones raise ``alert.raised`` to the workspace's owners
at most every 30 minutes, and alerts whose condition is gone resolve.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.auth import Principal
from app.config import get_settings
from app.database import async_session_factory
from app.errors import ApiError, Forbidden, NotFound
from app.services.common import append_event

logger = logging.getLogger("agentgraph.situation")
settings = get_settings()

FIELDS = ("requests", "reads", "writes", "noop_writes", "conflicts", "rate_limited",
          "errors_5xx")
NOTIFY_EVERY = timedelta(minutes=30)
RULES = {
    "retry_storm": "high", "idle_spin": "medium", "unclaimed_task": "medium",
    "repeated_orphan": "high", "conflict_anomaly": "medium", "cost_anomaly": "high",
    "index_backlog": "high", "taint_spread": "high",
}

_Key = tuple[str, str, datetime]
_buffer: dict[_Key, dict[str, int]] = {}
_holder: ContextVar[dict[str, Any] | None] = ContextVar("agentgraph_metrics", default=None)
_current: ContextVar[tuple[str, str] | None] = ContextVar("agentgraph_metrics_key",
                                                           default=None)


def _minute(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).replace(second=0, microsecond=0)


def principal_key_of(stamp: dict[str, Any] | None) -> str:
    stamp = stamp or {}
    return str(stamp.get("token_id") or stamp.get("principal_id") or "unknown")[:256]


def count(workspace_id: Any, key: str, field: str, amount: int = 1) -> None:
    bucket = _buffer.setdefault((str(workspace_id), key, _minute()),
                                dict.fromkeys(FIELDS, 0))
    bucket[field] += amount


def bind(workspace_id: Any, stamp: dict[str, Any] | None, *, write: bool) -> None:
    """Called once per authenticated request (REST and MCP)."""
    key = principal_key_of(stamp)
    _current.set((str(workspace_id), key))
    holder = _holder.get()
    if holder is not None:
        holder.update(workspace_id=str(workspace_id), key=key, write=write)
        return
    # MCP tools run outside the HTTP request's context: count here.
    count(workspace_id, key, "requests")
    count(workspace_id, key, "writes" if write else "reads")


def note(field: str) -> None:
    current = _current.get()
    if current is not None:
        count(current[0], current[1], field)


def note_status(status: int) -> None:
    field = _status_field(status)
    if field:
        note(field)


def _status_field(status: int) -> str | None:
    if status == 409:
        return "conflicts"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "errors_5xx"
    return None


class MetricsMiddleware:
    """Counts each authenticated REST request with its status."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return
        holder: dict[str, Any] = {}
        token = _holder.set(holder)
        status = 500

        async def capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            _holder.reset(token)
            if holder.get("key"):
                workspace_id, key = holder["workspace_id"], holder["key"]
                count(workspace_id, key, "requests")
                count(workspace_id, key, "writes" if holder.get("write") else "reads")
                field = _status_field(status)
                if field:
                    count(workspace_id, key, field)


async def flush() -> int:
    if not _buffer:
        return 0
    batch = dict(_buffer)
    _buffer.clear()
    try:
        async with async_session_factory() as session:
            for (workspace_id, key, minute), values in batch.items():
                await session.execute(
                    text("INSERT INTO principal_metrics (workspace_id, principal_key, minute, "
                         + ", ".join(FIELDS) + ") VALUES (:ws, :key, :minute, "
                         + ", ".join(f":{field}" for field in FIELDS) + ") ON CONFLICT "
                         "(workspace_id, principal_key, minute) DO UPDATE SET "
                         + ", ".join(f"{field} = principal_metrics.{field} + EXCLUDED.{field}"
                                     for field in FIELDS)),
                    {"ws": workspace_id, "key": key, "minute": minute, **values})
            await session.commit()
    except Exception:
        for bucket_key, values in batch.items():
            bucket = _buffer.setdefault(bucket_key, dict.fromkeys(FIELDS, 0))
            for field, amount in values.items():
                bucket[field] += amount
        raise
    return len(batch)


# ------------------------------------------------------------------ rules


async def _findings(session: AsyncSession) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def add(rule: str, workspace_id: Any, kind: str, ref: str, **details: Any) -> None:
        found.append({"rule": rule, "severity": RULES[rule], "workspace_id": workspace_id,
                      "subject_kind": kind, "subject_ref": ref, "details": details})

    for row in (await session.execute(text(
            "SELECT workspace_id, principal_key, sum(rate_limited) AS r, sum(conflicts) AS c "
            "FROM principal_metrics WHERE minute >= date_trunc('minute', NOW()) - "
            "INTERVAL '1 minute' GROUP BY 1, 2 HAVING sum(rate_limited + conflicts) >= 30"
    ))).mappings():
        add("retry_storm", row["workspace_id"], "principal", row["principal_key"],
            rate_limited=row["r"], conflicts=row["c"], window="1m")
    for row in (await session.execute(text(
            "SELECT workspace_id, principal_key, sum(writes) AS w, sum(noop_writes) AS n, "
            "sum(reads) AS r FROM principal_metrics WHERE minute >= NOW() - "
            "INTERVAL '30 minutes' GROUP BY 1, 2 HAVING (sum(writes) >= 50 AND "
            "sum(noop_writes) >= 0.8 * sum(writes)) OR (sum(reads) >= 50 AND "
            "sum(reads) >= 50 * GREATEST(sum(writes) - sum(noop_writes), 1))"
    ))).mappings():
        add("idle_spin", row["workspace_id"], "principal", row["principal_key"],
            writes=row["w"], noop_writes=row["n"], reads=row["r"], window="30m")
    for row in (await session.execute(text(
            "SELECT workspace_id, task_key, canonical_id, updated_at FROM collab_tasks "
            "WHERE status = 'READY' AND kind <> 'escalation' AND updated_at < NOW() - "
            "INTERVAL '2 hours' LIMIT 500"))).mappings():
        add("unclaimed_task", row["workspace_id"], "task", row["task_key"],
            id=row["canonical_id"], ready_since=row["updated_at"].isoformat())
    for row in (await session.execute(text(
            "SELECT workspace_id, task_key, canonical_id, orphan_count FROM collab_tasks "
            "WHERE orphan_count >= 2 AND status NOT IN ('DONE', 'FAILED', 'CANCELLED') "
            "LIMIT 500"))).mappings():
        add("repeated_orphan", row["workspace_id"], "task", row["task_key"],
            id=row["canonical_id"], orphan_count=row["orphan_count"])
    for row in (await session.execute(text(
            "SELECT c.workspace_id, d.doc_key, d.canonical_id, c.conflicts_total FROM "
            "doc_contention c JOIN shared_documents d ON d.id = c.document_id WHERE "
            "c.contended_until > NOW()"))).mappings():
        add("conflict_anomaly", row["workspace_id"], "document", row["doc_key"],
            id=row["canonical_id"], conflicts_total=row["conflicts_total"], contended=True)
    for row in (await session.execute(text(
            "WITH spend AS (SELECT workspace_id, metadata->>'principal_key' AS principal, "
            "sum(amount) AS used FROM usage_ledger WHERE category = 'embedding_tokens' AND "
            "created_at >= date_trunc('day', NOW()) GROUP BY 1, 2), total AS (SELECT "
            "workspace_id, sum(used) AS used FROM spend GROUP BY 1) SELECT s.workspace_id, "
            "s.principal, s.used, t.used AS total FROM spend s JOIN total t USING "
            "(workspace_id) WHERE s.principal IS NOT NULL AND t.used >= 10000 AND "
            "s.used >= 0.4 * t.used"))).mappings():
        add("cost_anomaly", row["workspace_id"], "principal", row["principal"],
            embedding_tokens=row["used"], workspace_total=row["total"])
    for row in (await session.execute(text(
            "SELECT workspace_id, principal_key, reason FROM budget_circuits WHERE "
            "cleared_at IS NULL AND reopen_at > NOW()"))).mappings():
        add("cost_anomaly", row["workspace_id"], "principal", row["principal_key"],
            circuit_open=row["reason"])
    for row in (await session.execute(text(
            "SELECT workspace_id, min(enqueued_at) AS oldest, count(*) AS pending FROM "
            "document_index WHERE state = 'pending' GROUP BY 1 HAVING min(enqueued_at) < "
            "NOW() - INTERVAL '10 minutes'"))).mappings():
        add("index_backlog", row["workspace_id"], "workspace", "index",
            oldest_pending_at=row["oldest"].isoformat(), pending=row["pending"])
    for row in (await session.execute(text(
            "SELECT workspace_id, count(DISTINCT document_id) AS docs FROM document_versions "
            "WHERE tainted AND created_at > NOW() - INTERVAL '1 hour' GROUP BY 1 "
            "HAVING count(DISTINCT document_id) >= 20"))).mappings():
        add("taint_spread", row["workspace_id"], "workspace", "taint", documents=row["docs"],
            window="1h")
    return found


async def evaluate() -> dict[str, int]:
    """One pass over every rule; returns how many alerts opened, notified, resolved."""
    await flush()
    opened = notified = resolved = 0
    async with async_session_factory() as session:
        findings = await _findings(session)
        seen: set[tuple[str, str, str]] = set()
        for item in findings:
            ident = (str(item["workspace_id"]), item["rule"], item["subject_ref"])
            if ident in seen:
                continue
            seen.add(ident)
            row = (await session.execute(
                text("INSERT INTO situation_alerts (workspace_id, rule, severity, subject_kind, "
                     "subject_ref, details) VALUES (:ws, :rule, :severity, :kind, :ref, "
                     "CAST(:details AS jsonb)) ON CONFLICT (workspace_id, rule, subject_ref) "
                     "WHERE state <> 'resolved' DO UPDATE SET last_seen = NOW(), details = "
                     "EXCLUDED.details RETURNING id, state, last_notified_at, "
                     "(xmax = 0) AS inserted"),
                {"ws": item["workspace_id"], "rule": item["rule"],
                 "severity": item["severity"], "kind": item["subject_kind"],
                 "ref": item["subject_ref"], "details": _dumps(item["details"])},
            )).mappings().one()
            opened += int(row["inserted"])
            due = row["last_notified_at"] is None or (
                datetime.now(timezone.utc) - row["last_notified_at"] >= NOTIFY_EVERY)
            if item["severity"] == "high" and row["state"] == "open" and due:
                await _raise(session, row["id"], item)
                notified += 1
        live = (await session.execute(text(
            "SELECT id, workspace_id, rule, subject_ref FROM situation_alerts "
            "WHERE state <> 'resolved'"))).mappings().all()
        for row in live:
            if (str(row["workspace_id"]), row["rule"], row["subject_ref"]) not in seen:
                await session.execute(
                    text("UPDATE situation_alerts SET state = 'resolved', resolved_at = NOW() "
                         "WHERE id = :id"), {"id": row["id"]})
                resolved += 1
        await session.execute(text(
            "DELETE FROM principal_metrics WHERE minute < NOW() - INTERVAL '30 days'"))
        await session.commit()
    return {"opened": opened, "notified": notified, "resolved": resolved}


async def _raise(session: AsyncSession, alert_id: str, item: dict[str, Any]) -> None:
    owners = (await session.execute(
        text("SELECT id FROM human_users WHERE workspace_id = :ws AND role = 'OWNER' "
             "AND is_active"), {"ws": item["workspace_id"]})).scalars().all()
    await session.execute(
        text("UPDATE situation_alerts SET last_notified_at = NOW(), "
             "notify_count = notify_count + 1 WHERE id = :id"), {"id": alert_id})
    await append_event(
        session, workspace_id=item["workspace_id"], agent="situation",
        event_type="ALERT_RAISED",
        payload={"id": alert_id, "targets": [f"human:{owner}" for owner in owners],
                 "details": {"alert_id": alert_id, "rule": item["rule"],
                             "severity": item["severity"], "subject": item["subject_ref"],
                             # SQL aggregates arrive as Decimal; JSON needs plain numbers.
                             **json.loads(_dumps(item["details"]))}})


async def situation_loop() -> None:
    while True:
        try:
            await flush()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - background resilience
            logger.exception("metrics flush failed")
        await asyncio.sleep(5)


async def evaluation_loop() -> None:
    while True:
        await asyncio.sleep(settings.situation_interval_seconds)
        try:
            await evaluate()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - background resilience
            logger.exception("situation evaluation failed")


def _dumps(value: Any) -> str:
    import json

    return json.dumps(value, default=str)


# ------------------------------------------------------------------ API


def _my_key(principal: Principal) -> str:
    return principal.token_cid or principal.agent_signature


async def list_alerts(session: AsyncSession, principal: Principal, *, state: str | None,
                      severity: str | None, cursor: str | None,
                      limit: int = 100) -> dict[str, Any]:
    """People see the workspace's alerts; an agent sees the alerts about itself."""
    rows = (await session.execute(
        text("SELECT * FROM situation_alerts WHERE workspace_id = :ws AND "
             "(CAST(:state AS text) IS NULL OR state = :state) AND "
             "(CAST(:severity AS text) IS NULL OR severity = :severity) AND "
             "(CAST(:mine AS text) IS NULL OR subject_ref = :mine) AND "
             "(CAST(:cursor AS text) IS NULL OR id < :cursor) "
             "ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
             "id DESC LIMIT :limit"),
        {"ws": principal.workspace.id, "state": state, "severity": severity,
         "mine": None if principal.is_human else _my_key(principal), "cursor": cursor,
         "limit": limit + 1})).mappings().all()
    items = [_alert_out(row) for row in rows[:limit]]
    return {"items": items, "next_cursor": items[-1]["alert_id"] if len(rows) > limit else None}


def _alert_out(row: Any) -> dict[str, Any]:
    return {"alert_id": row["id"], "rule": row["rule"], "severity": row["severity"],
            "subject": {"kind": row["subject_kind"], "ref": row["subject_ref"]},
            "state": row["state"], "details": row["details"],
            "first_seen": row["first_seen"].isoformat(),
            "last_seen": row["last_seen"].isoformat(),
            "notify_count": row["notify_count"], "acked_by": row["acked_by"],
            "acked_at": row["acked_at"].isoformat() if row["acked_at"] else None,
            "note": row["note"],
            "resolved_at": row["resolved_at"].isoformat() if row["resolved_at"] else None}


async def ack_alert(session: AsyncSession, principal: Principal, alert_id: str,
                    note: str | None) -> dict[str, Any]:
    if not principal.is_human:
        raise Forbidden("only a person acknowledges an alert")
    row = (await session.execute(
        text("UPDATE situation_alerts SET state = 'acked', acked_by = :by, acked_at = NOW(), "
             "note = :note WHERE id = :id AND workspace_id = :ws AND state = 'open' "
             "RETURNING *"),
        {"by": principal.agent_signature, "note": note, "id": alert_id,
         "ws": principal.workspace.id})).mappings().one_or_none()
    if row is None:
        raise NotFound("alert", f"no open alert '{alert_id}'")
    await session.commit()
    return _alert_out(row)


_WINDOWS = {"1h": timedelta(hours=1), "24h": timedelta(days=1), "7d": timedelta(days=7),
            "30d": timedelta(days=30)}


async def get_metrics(session: AsyncSession, principal: Principal, *,
                      principal_ref: str | None, doc_ref: str | None, task_ref: str | None,
                      window: str) -> dict[str, Any]:
    if window not in _WINDOWS:
        raise ApiError(422, "validation_error", "window is 1h, 24h, 7d or 30d",
                       details={"field": "window", "allowed": list(_WINDOWS)})
    since = datetime.now(timezone.utc) - _WINDOWS[window]
    await flush()
    if doc_ref or task_ref:
        if not principal.is_human:
            raise Forbidden("agents read only their own metrics")
        return await (_doc_metrics(session, principal, doc_ref, since) if doc_ref
                      else _task_metrics(session, principal, task_ref, since))
    key = principal_ref or _my_key(principal)
    if not principal.is_human and key not in (principal.token_cid, principal.agent_signature):
        raise Forbidden("agents read only their own metrics")
    totals = (await session.execute(
        text("SELECT " + ", ".join(f"COALESCE(sum({field}), 0) AS {field}" for field in FIELDS)
             + " FROM principal_metrics WHERE workspace_id = :ws AND principal_key = :key "
             "AND minute >= :since"),
        {"ws": principal.workspace.id, "key": key, "since": since})).mappings().one()
    embedding = await session.scalar(
        text("SELECT COALESCE(sum(amount), 0) FROM usage_ledger WHERE workspace_id = :ws AND "
             "category = 'embedding_tokens' AND created_at >= :since AND "
             "metadata->>'principal_key' = :key"),
        {"ws": principal.workspace.id, "since": since, "key": key})
    values = {field: int(totals[field]) for field in FIELDS}
    effective = max(values["writes"] - values["noop_writes"], 0)
    return {"principal": key, "window": window, **values,
            "embedding_tokens": int(embedding or 0),
            "noop_ratio": round(values["noop_writes"] / values["writes"], 3)
            if values["writes"] else 0.0,
            "read_write_ratio": round(values["reads"] / max(effective, 1), 3)}


async def _doc_metrics(session: AsyncSession, principal: Principal, ref: str,
                       since: datetime) -> dict[str, Any]:
    from app.services import addresses as address_service
    from app.services import contention

    document = await address_service.find_document(session, principal.workspace.id, ref)
    if document is None:
        raise NotFound("shared_document", f"document '{ref}' not found")
    versions = await session.scalar(
        text("SELECT count(*) FROM document_versions WHERE document_id = :d AND "
             "created_at >= :since"), {"d": document.id, "since": since})
    state = await contention.state(session, document.id)
    return {"doc_key": document.doc_key, "id": document.canonical_id,
            "versions_in_window": int(versions or 0), **state}


async def _task_metrics(session: AsyncSession, principal: Principal, ref: str,
                        since: datetime) -> dict[str, Any]:
    from app.services import addresses as address_service

    task = await address_service.find_task(session, principal.workspace.id, ref)
    if task is None:
        raise NotFound("collab_task", f"task '{ref}' not found")
    waiting = None
    if task.status.value == "READY":
        waiting = int((datetime.now(timezone.utc) - task.updated_at).total_seconds())
    events = (await session.execute(
        text("SELECT event_id, seq, type, created_at FROM workspace_events WHERE "
             "workspace_id = :ws AND task_key = :key AND created_at >= :since "
             "ORDER BY seq DESC LIMIT 50"),
        {"ws": principal.workspace.id, "key": task.task_key, "since": since})).mappings().all()
    return {"task_key": task.task_key, "id": task.canonical_id, "status": task.status.value,
            "waiting_seconds": waiting, "orphan_count": task.orphan_count or 0,
            "timeline": [{"event_id": row["event_id"], "seq": row["seq"], "type": row["type"],
                          "at": row["created_at"].isoformat()} for row in events]}
