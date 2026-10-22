"""Watermarks and the "while you were away" digest (PRD AG-EV-04).

A watermark is a named event cursor owned by a principal (the directory's
``prn_`` id when the agent is registered), so a new token or a successor
keeps it. ``last_seen`` advances on every ``get_orientation``. A digest
summarises a window of events: counts by type, namespace and actor, what
needs attention, what was addressed to the caller and which documents
changed (once each), within a bounded size.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_permission
from app.errors import ApiError, NotFound
from app.services import events as event_service

NAME_MAX = 64
ITEMS_MAX = 200
SIZE_LIMIT = 64 * 1024
ATTENTION = ("escalation.", "budget.circuit_open", "doc.recalled", "task.lease_expired",
             "human_review.requested", "alert.raised", "security.incident_notice")


async def owner_ref(session: AsyncSession, principal: Principal) -> str:
    if principal.is_human and principal.user_id is not None:
        return f"human:{principal.user_id}"
    if principal.token_cid:
        prn = await session.scalar(
            text("SELECT principal_id FROM agent_directory WHERE :t = ANY(token_ids)"),
            {"t": principal.token_cid})
        if prn:
            return f"agent:{prn}"
    return f"agent:{principal.agent_signature}"


def _name(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > NAME_MAX:
        raise ApiError(422, "validation_error", f"a watermark name is 1-{NAME_MAX} characters",
                       details={"field": "name"})
    return name


async def set_watermark(session: AsyncSession, principal: Principal, name: str,
                        cursor: str) -> dict[str, Any]:
    require_permission(principal, "events:read")
    seq = event_service.decode_cursor(cursor)
    if seq is None:
        raise ApiError(400, "cursor_invalid", "event cursor is not valid",
                       details={"field": "cursor"})
    return await _store(session, principal, _name(name), seq, commit=True)


async def _store(session: AsyncSession, principal: Principal, name: str, seq: int, *,
                 commit: bool) -> dict[str, Any]:
    owner = await owner_ref(session, principal)
    await session.execute(
        text("INSERT INTO watermarks (workspace_id, principal_ref, name, seq) VALUES "
             "(:ws, :p, :n, :s) ON CONFLICT (workspace_id, principal_ref, name) DO UPDATE "
             "SET seq = EXCLUDED.seq, updated_at = NOW()"),
        {"ws": principal.workspace.id, "p": owner, "n": name, "s": seq})
    if commit:
        await session.commit()
    return {"name": name, "cursor": event_service.encode_cursor(seq), "seq": seq,
            "owner": owner}


async def get_watermark(session: AsyncSession, principal: Principal,
                        name: str) -> dict[str, Any]:
    require_permission(principal, "events:read")
    owner = await owner_ref(session, principal)
    seq = await session.scalar(
        text("SELECT seq FROM watermarks WHERE workspace_id = :ws AND principal_ref = :p "
             "AND name = :n"), {"ws": principal.workspace.id, "p": owner, "n": _name(name)})
    if seq is None:
        raise NotFound("watermark", f"watermark '{name}' not set")
    return {"name": name, "cursor": event_service.encode_cursor(seq), "seq": seq,
            "owner": owner}


async def advance_last_seen(session: AsyncSession, principal: Principal) -> None:
    head = await event_service.current_seq(session, principal.workspace.id)
    await _store(session, principal, "last_seen", head, commit=False)


async def _resolve_since(session: AsyncSession, principal: Principal, since: str | None) -> int:
    if since is None:
        return 0
    if since.startswith("watermark:"):
        try:
            return (await get_watermark(session, principal, since.split(":", 1)[1]))["seq"]
        except NotFound:
            return 0
    seq = event_service.decode_cursor(since)
    if seq is None:
        raise ApiError(400, "cursor_invalid", "event cursor is not valid",
                       details={"field": "since"})
    return seq


async def get_digest(session: AsyncSession, principal: Principal, *, since: str | None,
                     until: str | None = None, for_me_first: bool = True,
                     max_items: int = 50, advance_watermark: str | None = None
                     ) -> dict[str, Any]:
    require_permission(principal, "events:read")
    if not 1 <= max_items <= ITEMS_MAX:
        raise ApiError(422, "validation_error", f"max_items is 1-{ITEMS_MAX}",
                       details={"field": "max_items", "limit": ITEMS_MAX})
    from_seq = await _resolve_since(session, principal, since)
    to_seq = (event_service.decode_cursor(until) if until else None) or \
        await event_service.current_seq(session, principal.workspace.id)
    readable = event_service._readable(principal)
    scope = "AND (CAST(:ns AS text[]) IS NULL OR shard_key = ANY(:ns))"
    params = {"ws": principal.workspace.id, "a": from_seq, "b": to_seq, "ns": readable}
    window = (await session.execute(text(
        "SELECT min(created_at) AS first, max(created_at) AS last, count(*) AS total FROM "
        "workspace_events WHERE workspace_id = :ws AND seq > :a AND seq <= :b "
        "AND type IS NOT NULL " + scope), params)).mappings().one()

    async def grouped(column: str) -> dict[str, int]:
        rows = (await session.execute(text(
            f"SELECT {column} AS key, count(*) AS n FROM workspace_events WHERE "
            "workspace_id = :ws AND seq > :a AND seq <= :b AND type IS NOT NULL " + scope +
            " GROUP BY 1 ORDER BY 2 DESC LIMIT 50"), params)).all()
        return {str(key): int(count) for key, count in rows if key is not None}

    by_actor = await grouped("COALESCE(CASE WHEN stamp->>'kind' = 'human' THEN 'human:' "
                             "|| (stamp->>'principal_id') END, 'agent:' || actor)")
    counts = {"by_type": await grouped("type"), "by_namespace": await grouped("shard_key"),
              "by_actor": by_actor}
    attention_filter = " OR ".join(
        f"type LIKE '{prefix}%'" if prefix.endswith(".") else f"type = '{prefix}'"
        for prefix in ATTENTION)
    attention = (await session.execute(text(
        "SELECT * FROM workspace_events WHERE workspace_id = :ws AND seq > :a AND seq <= :b "
        f"AND ({attention_filter}) " + scope + " ORDER BY seq LIMIT :limit"),
        {**params, "limit": max_items})).mappings().all()
    tainted = (await session.execute(text(
        "SELECT namespace, doc_key, version, created_at FROM shared_documents WHERE "
        "workspace_id = :ws AND tainted "
        "AND created_at >= COALESCE(CAST(:since AS timestamptz), '-infinity') "
        "ORDER BY created_at DESC LIMIT :limit"),
        {"ws": principal.workspace.id, "since": window["first"], "limit": max_items}
    )).mappings().all() if window["total"] else []
    mine = await event_service._directory_identities(session, principal)
    identities = list(dict.fromkeys(event_service._identities(principal) + mine))
    for_me = (await session.execute(text(
        "SELECT * FROM workspace_events WHERE workspace_id = :ws AND seq > :a AND seq <= :b "
        "AND targets ?| CAST(:me AS text[]) ORDER BY seq LIMIT :limit"),
        {"ws": principal.workspace.id, "a": from_seq, "b": to_seq, "me": identities,
         "limit": max_items})).mappings().all()
    changed = (await session.execute(text(
        "SELECT shard_key AS namespace, doc_key, min(version) AS v0, max(version) AS v1, "
        "count(*) AS changes, (array_agg(actor ORDER BY seq DESC))[1] AS last_actor FROM "
        "workspace_events WHERE workspace_id = :ws AND seq > :a AND seq <= :b AND "
        "type LIKE 'doc.%' AND doc_key IS NOT NULL " + scope + " GROUP BY 1, 2 "
        "ORDER BY max(seq) DESC LIMIT :limit"), {**params, "limit": max_items})).mappings().all()

    def brief(row: Any) -> dict[str, Any]:
        return {"seq": row["seq"], "type": row["type"], "event_id": row["event_id"],
                "summary": row["summary"], "refs": row["refs"] or {},
                "at": row["created_at"].isoformat()}

    result: dict[str, Any] = {
        "from_seq": from_seq, "to_seq": to_seq,
        "window": {"from": window["first"].isoformat() if window["first"] else None,
                   "to": window["last"].isoformat() if window["last"] else None},
        "total_events": int(window["total"]), "counts": counts,
        "needs_attention": [brief(row) for row in attention] + [
            {"type": "doc.tainted", "refs": {"namespace": row["namespace"],
                                             "doc_key": row["doc_key"],
                                             "version": row["version"]},
             "at": row["created_at"].isoformat()} for row in tainted],
        "for_me": [brief(row) for row in for_me],
        "changed_documents": [{"namespace": row["namespace"], "doc_key": row["doc_key"],
                               "from_version": row["v0"], "to_version": row["v1"],
                               "changes": int(row["changes"]),
                               "last_actor": row["last_actor"]} for row in changed],
        "truncated": False,
        "cursor": event_service.encode_cursor(to_seq),
    }
    if not for_me_first:
        result["for_me"] = sorted(result["for_me"], key=lambda item: item["seq"])
    while len(json.dumps(result, default=str)) > SIZE_LIMIT:
        result["truncated"] = True
        longest = max(("changed_documents", "for_me", "needs_attention"),
                      key=lambda key: len(result[key]))
        if not result[longest]:
            break
        result[longest] = result[longest][: len(result[longest]) // 2]
    if advance_watermark:
        await _store(session, principal, _name(advance_watermark), to_seq, commit=False)
    await session.commit()
    return result
