"""The inbox: every change reaches the people it concerns (2026-10-22 delivery
protocol, "事事有回应").

Recipients are computed in the outbox trigger, in the same transaction as the
change (events' ``targets``; ``details.delivery_kind`` and ``priority`` for
the catalog kinds: task claimed, review requested, done, failed, escalated,
awaiting a human, handed off, review rejected; proposal created, applied,
rejected, superseded; document verified). The actor is never a recipient.
An inbox item is a targeted event; reading position is a watermark named
``inbox`` owned by the principal (people share one, owner ``human``), moved
forward only by ``ack``. Envelopes carry keys, statuses and the verdict note,
never document content.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.config import get_settings
from app.services import digest
from app.services import events as event_service

settings = get_settings()
WATERMARK = "inbox"
HUMAN = "human"
ORIENTATION_ITEMS = 10
LIMIT_MAX = 200
STALE_HOURS = 24


async def _who(session: AsyncSession, principal: Principal, human: bool) -> tuple[str, list[str]]:
    """Watermark owner and the target names that mean "me"."""
    if human:
        names = [HUMAN]
        if principal.user_id is not None:
            names.append(f"human:{principal.user_id}")
        return HUMAN, names
    names = event_service._identities(principal)
    names += await event_service._directory_identities(session, principal)
    return await digest.owner_ref(session, principal), list(dict.fromkeys(names))


async def _acked(session: AsyncSession, principal: Principal, owner: str) -> int:
    return int(await session.scalar(
        text("SELECT seq FROM watermarks WHERE workspace_id = :ws AND principal_ref = :p "
             "AND name = :n"),
        {"ws": principal.workspace.id, "p": owner, "n": WATERMARK}) or 0)


def envelope(row: Any) -> dict[str, Any]:
    """The whitelisted view of a delivery: no content, ever."""
    details = (row["payload"] or {}).get("details") or {}
    refs = row["refs"] or {}
    return {
        "event_seq": row["seq"], "event_id": row["event_id"],
        "kind": details.get("delivery_kind") or row["type"], "type": row["type"],
        "priority": int(details.get("priority", 1)), "source_table": row["source_table"],
        "task_key": row["task_key"], "doc_key": row["doc_key"] or refs.get("alias"),
        "from_status": details.get("from_status"), "to_status": details.get("to_status"),
        "actor": row["actor"], "note": details.get("note"), "version": row["version"],
        "applied_version": details.get("applied_version"),
        "created_at": row["created_at"].isoformat(),
    }


_PRIORITY = "COALESCE((payload->'details'->>'priority')::int, 1)"


async def summary(session: AsyncSession, principal: Principal, *, human: bool = False,
                  status: str = "unacked", limit: int = 50) -> dict[str, Any]:
    owner, names = await _who(session, principal, human)
    acked = await _acked(session, principal, owner)
    limit = max(1, min(limit, LIMIT_MAX))
    params = {"ws": principal.workspace.id, "me": names, "acked": acked}
    mine = "workspace_id = :ws AND targets ?| CAST(:me AS text[]) AND seq IS NOT NULL"
    counts = (await session.execute(text(
        f"SELECT count(*) AS total, count(*) FILTER (WHERE {_PRIORITY} = 0) AS terminal "
        f"FROM workspace_events WHERE {mine} AND seq > :acked"), params)).mappings().one()
    since = "" if status == "all" else "AND seq > :acked"
    rows = (await session.execute(text(
        "SELECT seq, event_id, type, source_table, task_key, doc_key, actor, version, refs, "
        f"payload, created_at FROM workspace_events WHERE {mine} {since} "
        f"ORDER BY {_PRIORITY}, seq LIMIT :limit"), {**params, "limit": limit})
    ).mappings().all()
    return {"items": [envelope(row) for row in rows], "unacked_count": int(counts["total"]),
            "unacked_terminal_count": int(counts["terminal"]), "acked_event_seq": acked}


async def ack(session: AsyncSession, principal: Principal, up_to: int, *,
              human: bool = False) -> dict[str, Any]:
    """Move the reading position forward (never back, never past the head)."""
    owner, names = await _who(session, principal, human)
    acked = await _acked(session, principal, owner)
    head = await event_service.current_seq(session, principal.workspace.id)
    target = max(acked, min(int(up_to), head))
    if target != acked:
        await digest._store(session, principal, WATERMARK, target, commit=False, owner=owner)
    await session.commit()
    remaining = await summary(session, principal, human=human, limit=1)
    return {"acked_event_seq": target, "unacked_count": remaining["unacked_count"]}


def surfaced(status: str, updated_at: datetime | None) -> bool:
    """Waiting on a person, or handed off without a verdict, for over 24 hours."""
    if status not in ("AWAITING_HUMAN", "WAITING_HUMAN", "HANDOFF", "REVIEWING"):
        return False
    return updated_at is not None and updated_at < datetime.now(timezone.utc) - timedelta(
        hours=STALE_HOURS)
