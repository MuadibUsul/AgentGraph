"""Personal rights requests: locate, export, correct, pseudonymise, redact (PRD AG-TC-07).

A request names a person by any identifiers (principal, emails, names,
phones, others) and is handled by an owner or admin of the node (or the
platform admin for platform data). ``locate`` builds the inventory: exact
structured records (account, memberships, tokens they issued, lineage stamps,
audit entries, usage, assigned tasks) and exact text matches in every
document version, frontmatter, task text and retained event (emails and
phones case-insensitive, names as full strings, no fuzzy matching).

Erasure keeps accountability: pseudonymisation replaces a principal's names
and email everywhere they appear as a label and keeps its ``prn_`` id, so
lineage still says who wrote what under which grant, but no longer who that
was. Redaction writes a new version of each hit document and rewrites the
hit historical versions in place (``redacted_at``, ``redaction_hash``; the
original ``content_hash`` stays). Both are recorded in the append-only
``redaction_log`` and replayed after any restore.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import secrets
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthKind, Principal
from app.database import async_session_factory
from app.errors import ApiError, Forbidden, NotFound

PLACEHOLDER = "[已删除的个人信息]"
TYPES = ("access", "copy", "portability", "correction", "deletion", "explanation")
MODES = ("pseudonymise", "redact", "delete")
DUE_BUSINESS_DAYS = 15
REMIND_REMAINING_DAYS = 5
MINIMIZE_AFTER = timedelta(days=30)
EXPORT_TTL = timedelta(days=7)
SNIPPET = 80
# Customer content: redaction handles it, the label sweep leaves it alone.
CONTENT_COLUMNS = {
    ("shared_documents", "content"), ("shared_documents", "frontmatter"),
    ("shared_documents", "title"), ("document_versions", "content"),
    ("document_versions", "frontmatter"), ("document_versions", "title"),
    ("collab_tasks", "title"), ("collab_tasks", "description"),
}
# Never rewritten by the sweep.
SWEEP_SKIP_TABLES = {"schema_migrations", "redaction_log", "rights_requests",
                     "rights_request_events", "rights_exports", "export_archives"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def add_business_days(start: datetime, days: int) -> datetime:
    current = start
    while days > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            days -= 1
    return current


def business_days_between(start: datetime, end: datetime) -> int:
    days, current = 0, start
    while current.date() < end.date():
        current += timedelta(days=1)
        if current.weekday() < 5:
            days += 1
    return days


def request_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": row["id"],
        "tenant_id": row["tenant_id"],
        "include_subtree": row["include_subtree"],
        "scope": row["scope"],
        "subject": row["subject"],
        "type": row["type"],
        "status": row["status"],
        "received_at": row["received_at"],
        "due_at": row["due_at"],
        "handler": row["handler"],
        "notes": row["notes"],
        "located_at": row["located_at"],
        "counts": row["counts"],
        "closed_at": row["closed_at"],
        "minimized_at": row["minimized_at"],
    }


# ------------------------------------------------------------ authorization


async def _authorize(session: AsyncSession, principal: Principal, tenant_id: str,
                     *, scope: str = "customer") -> str:
    """Owners and admins of the node; the platform admin for platform data."""
    if principal.auth_kind is AuthKind.HUMAN_ADMIN:
        return "admin"
    if principal.auth_kind is not AuthKind.HUMAN or principal.user_id is None:
        raise Forbidden("rights requests are handled by people, not agent tokens")
    if scope == "platform":
        raise Forbidden("platform-data requests are handled by the platform admin")
    from app.services.members import effective_role

    role = await effective_role(session, principal.user_id, tenant_id)
    if role is None or role["role"] not in ("owner", "admin"):
        raise Forbidden("only owners and admins of the node handle rights requests")
    return f"human:{principal.user_id}"


async def _load(session: AsyncSession, principal: Principal, request_id: str,
                *, open_only: bool = False) -> dict[str, Any]:
    row = (
        await session.execute(text("SELECT * FROM rights_requests WHERE id = :id"),
                              {"id": request_id})
    ).mappings().one_or_none()
    if row is None:
        raise NotFound("rights_request", f"request '{request_id}' not found")
    row = dict(row)
    actor = await _authorize(session, principal, row["tenant_id"], scope=row["scope"])
    row["_actor"] = actor
    if open_only and row["status"] == "closed":
        raise ApiError(409, "request_closed", "this request is closed",
                       details={"closed_at": row["closed_at"].isoformat()})
    return row


async def _event(session: AsyncSession, request_id: str, kind: str, actor: str,
                 details: dict[str, Any] | None = None) -> None:
    await session.execute(
        text("INSERT INTO rights_request_events (request_id, kind, actor, details) "
             "VALUES (:r, :k, :a, CAST(:d AS jsonb))"),
        {"r": request_id, "k": kind, "a": actor,
         "d": json.dumps(details or {}, default=str)},
    )


async def _tenants(session: AsyncSession, row: dict[str, Any]) -> list[str]:
    if not row["include_subtree"]:
        return [row["tenant_id"]]
    return list(
        (
            await session.execute(
                text("SELECT id FROM tenants WHERE :t = ANY(path)"), {"t": row["tenant_id"]}
            )
        ).scalars().all()
    )


async def _workspaces(session: AsyncSession, tenants: list[str]) -> list[str]:
    return [str(item) for item in (
        await session.execute(
            text("SELECT id FROM workspaces WHERE tenant_id = ANY(:t)"), {"t": tenants}
        )
    ).scalars().all()]


# ---------------------------------------------------------------- lifecycle


def _clean_subject(subject: dict[str, Any]) -> dict[str, Any]:
    clean = {
        "principal_ref": (subject.get("principal_ref") or None),
        "emails": sorted({item.strip() for item in subject.get("emails") or [] if item.strip()}),
        "names": sorted({item.strip() for item in subject.get("names") or [] if item.strip()}),
        "phones": sorted({item.strip() for item in subject.get("phones") or [] if item.strip()}),
        "other_identifiers": sorted({item.strip()
                                     for item in subject.get("other_identifiers") or []
                                     if item.strip()}),
    }
    if not any(clean[key] for key in clean):
        raise ApiError(422, "subject_identifier_required",
                       "name the person: principal_ref, emails, names, phones or others",
                       details={"fields": list(clean)})
    return clean


async def create_request(session: AsyncSession, principal: Principal, tenant_ref: str,
                         data: dict[str, Any]) -> dict[str, Any]:
    tenant_id = principal.tenant_id if tenant_ref in ("current", "self") else tenant_ref
    scope = data.get("scope") or "customer"
    actor = await _authorize(session, principal, tenant_id, scope=scope)
    if data.get("type") not in TYPES:
        raise ApiError(422, "validation_error", "unknown request type",
                       details={"field": "type", "allowed": list(TYPES)})
    subject = _clean_subject(data.get("subject") or {})
    received = data.get("received_at") or _now()
    row = (
        await session.execute(
            text("INSERT INTO rights_requests (tenant_id, include_subtree, scope, subject, type, "
                 "received_at, due_at, handler, notes) VALUES (:t, :sub, :scope, "
                 "CAST(:subject AS jsonb), :type, :received, :due, :handler, :notes) "
                 "RETURNING *"),
            {"t": tenant_id, "sub": bool(data.get("include_subtree", True)), "scope": scope,
             "subject": json.dumps(subject), "type": data["type"], "received": received,
             "due": add_business_days(received, DUE_BUSINESS_DAYS), "handler": actor,
             "notes": data.get("notes")},
        )
    ).mappings().one()
    await _event(session, row["id"], "created", actor, {"type": data["type"]})
    await _audit(session, tenant_id, "rights_request.created",
                 {"request_id": row["id"], "type": data["type"], "by": actor})
    await session.commit()
    return request_out(dict(row))


async def _audit(session: AsyncSession, tenant_id: str, action: str,
                 details: dict[str, Any]) -> None:
    workspace = await session.scalar(
        text("SELECT id FROM workspaces WHERE tenant_id = :t ORDER BY created_at LIMIT 1"),
        {"t": tenant_id},
    )
    if workspace is not None:
        await session.execute(
            text("INSERT INTO access_audit (workspace_id, action, details) "
                 "VALUES (:w, :a, CAST(:d AS jsonb))"),
            {"w": workspace, "a": action, "d": json.dumps(details, default=str)},
        )


async def get_request(session: AsyncSession, principal: Principal,
                      request_id: str) -> dict[str, Any]:
    row = await _load(session, principal, request_id)
    events = (
        await session.execute(
            text("SELECT at, kind, actor, details FROM rights_request_events "
                 "WHERE request_id = :r ORDER BY id"),
            {"r": request_id},
        )
    ).mappings().all()
    return {**request_out(row), "inventory": row["inventory"],
            "timeline": [dict(item) for item in events]}


async def list_requests(session: AsyncSession, principal: Principal,
                        tenant_ref: str) -> dict[str, Any]:
    tenant_id = principal.tenant_id if tenant_ref in ("current", "self") else tenant_ref
    await _authorize(session, principal, tenant_id)
    rows = (
        await session.execute(
            text("SELECT * FROM rights_requests WHERE tenant_id = :t ORDER BY received_at DESC"),
            {"t": tenant_id},
        )
    ).mappings().all()
    return {"items": [request_out(dict(row)) for row in rows]}


async def close_request(session: AsyncSession, principal: Principal, request_id: str, *,
                        outcome: str = "fulfilled", notes: str | None = None) -> dict:
    row = await _load(session, principal, request_id, open_only=True)
    await session.execute(
        text("UPDATE rights_requests SET status = 'closed', closed_at = NOW(), "
             "notes = COALESCE(:n, notes), updated_at = NOW() WHERE id = :id"),
        {"id": request_id, "n": notes},
    )
    await _event(session, request_id, "closed", row["_actor"], {"outcome": outcome})
    await _audit(session, row["tenant_id"], "rights_request.closed",
                 {"request_id": request_id, "outcome": outcome})
    await session.commit()
    return await get_request(session, principal, request_id)


# ------------------------------------------------------------------- locate


async def _subject_records(session: AsyncSession, workspaces: list[str],
                           subject: dict[str, Any]) -> dict[str, Any]:
    """The principal(s) the subject names: human accounts and directory agents."""
    ref = subject.get("principal_ref")
    emails = [item.lower() for item in subject["emails"]]
    humans = (
        await session.execute(
            text("SELECT id, canonical_id, email, display_name, workspace_id FROM human_users "
                 "WHERE CAST(workspace_id AS text) = ANY(:ws) AND (lower(email) = ANY(:emails) "
                 "OR CAST(id AS text) = :ref OR canonical_id = :ref)"),
            {"ws": workspaces, "emails": emails, "ref": ref or ""},
        )
    ).mappings().all()
    agents = (
        await session.execute(
            text("SELECT principal_id, agent_id, display_name, signatures, token_ids "
                 "FROM agent_directory WHERE CAST(workspace_id AS text) = ANY(:ws) "
                 "AND (principal_id = :ref OR agent_id = :ref)"),
            {"ws": workspaces, "ref": (ref or "").removeprefix("agent:")},
        )
    ).mappings().all()
    return {"humans": [dict(row) for row in humans], "agents": [dict(row) for row in agents]}


def _terms(subject: dict[str, Any], records: dict[str, Any]) -> list[tuple[str, bool]]:
    """(term, case_insensitive) pairs to search for in text."""
    terms: dict[str, bool] = {}
    for email in subject["emails"]:
        terms[email] = True
    for human in records["humans"]:
        terms[human["email"]] = True
    for phone in subject["phones"]:
        terms[phone] = True
    for name in subject["names"]:
        terms[name] = False
    for other in subject["other_identifiers"]:
        terms[other] = False
    return [(term, insensitive) for term, insensitive in terms.items() if len(term) >= 3]


def _snippet(body: str, term: str, insensitive: bool) -> str:
    haystack = body.lower() if insensitive else body
    needle = term.lower() if insensitive else term
    index = haystack.find(needle)
    if index < 0:
        return ""
    start = max(0, index - (SNIPPET - len(term)) // 2)
    return body[start:start + SNIPPET]


def _match_sql(column: str, insensitive: bool) -> str:
    return (f"strpos(lower({column}), lower(:term)) > 0" if insensitive
            else f"strpos({column}, :term) > 0")


async def _structured(session: AsyncSession, workspaces: list[str],
                      records: dict[str, Any]) -> dict[str, Any]:
    principal_ids = [row["canonical_id"] for row in records["humans"] if row["canonical_id"]]
    principal_ids += [row["principal_id"] for row in records["agents"]]
    user_ids = [str(row["id"]) for row in records["humans"]]
    labels = [row["email"] for row in records["humans"]]
    for agent in records["agents"]:
        labels += list(agent["signatures"] or [])
    params = {"ws": workspaces, "pids": principal_ids, "uids": user_ids, "labels": labels}
    memberships = (
        await session.execute(
            text("SELECT id, tenant_id, role FROM memberships "
                 "WHERE CAST(principal_id AS text) = ANY(:uids)"), params)
    ).mappings().all()
    tokens = (
        await session.execute(
            text("SELECT canonical_id FROM agent_tokens WHERE CAST(workspace_id AS text) = "
                 "ANY(:ws) AND (CAST(issued_by_human AS text) = ANY(:uids) OR "
                 "agent_signature = ANY(:labels))"), params)
    ).scalars().all()
    stamps = (
        await session.execute(
            text("SELECT DISTINCT COALESCE(d.canonical_id, CAST(d.id AS text)) FROM "
                 "document_versions v JOIN shared_documents d ON d.id = v.document_id "
                 "WHERE CAST(v.workspace_id AS text) = ANY(:ws) AND "
                 "(v.stamp->>'principal_id' = ANY(:pids) OR v.author = ANY(:labels))"), params)
    ).scalars().all()
    audit = int(await session.scalar(
        text("SELECT (SELECT count(*) FROM state_audit_diffs WHERE CAST(workspace_id AS text) "
             "= ANY(:ws) AND (stamp->>'principal_id' = ANY(:pids) OR agent_signature = "
             "ANY(:labels))) + (SELECT count(*) FROM access_audit WHERE CAST(workspace_id AS "
             "text) = ANY(:ws) AND (details->>'principal' = ANY(:labels) OR "
             "details->>'principal_id' = ANY(:uids)))"), params) or 0)
    usage = int(await session.scalar(
        text("SELECT count(*) FROM usage_ledger WHERE CAST(workspace_id AS text) = ANY(:ws) "
             "AND agent_signature = ANY(:labels)"), params) or 0)
    tasks = (
        await session.execute(
            text("SELECT COALESCE(canonical_id, CAST(id AS text)) FROM collab_tasks WHERE "
                 "CAST(workspace_id AS text) = ANY(:ws) AND (assignee = ANY(:labels) OR "
                 "claimed_by = ANY(:labels))"), params)
    ).scalars().all()
    return {
        "principals": [
            *({"item_id": f"principal:human:{row['id']}", "type": "human",
               "principal_id": row["canonical_id"], "display_name": row["display_name"]}
              for row in records["humans"]),
            *({"item_id": f"principal:agent:{row['principal_id']}", "type": "agent",
               "principal_id": row["principal_id"], "agent_id": row["agent_id"]}
              for row in records["agents"]),
        ],
        "memberships": [{"item_id": f"membership:{row['id']}", "tenant_id": row["tenant_id"],
                         "role": row["role"]} for row in memberships],
        "tokens": [{"item_id": f"token:{item}", "token_id": item} for item in tokens],
        "lineage_stamps": {"documents": len(stamps), "refs": list(stamps)[:200]},
        "audit_entries": audit,
        "usage_records": {"count": usage, "legal_retention": True,
                          "reason": "billing records are kept as the law requires"},
        "assigned_tasks": [{"item_id": f"task:{item}", "task_id": item} for item in tasks],
    }


async def _unstructured(session: AsyncSession, workspaces: list[str],
                        terms: list[tuple[str, bool]]) -> list[dict[str, Any]]:
    hits: dict[str, dict[str, Any]] = {}
    for term, insensitive in terms:
        params = {"ws": workspaces, "term": term}
        docs = (
            await session.execute(
                text("SELECT id, COALESCE(canonical_id, CAST(id AS text)) AS ref, doc_key, "
                     "version, content, CAST(frontmatter AS text) AS fm, title FROM "
                     "shared_documents WHERE CAST(workspace_id AS text) = ANY(:ws) AND ("
                     f"{_match_sql('content', insensitive)} OR "
                     f"{_match_sql('CAST(frontmatter AS text)', insensitive)} OR "
                     f"{_match_sql('title', insensitive)})"), params)
        ).mappings().all()
        for doc in docs:
            field = next(name for name, value in (("content", doc["content"]),
                                                   ("frontmatter", doc["fm"]),
                                                   ("title", doc["title"] or ""))
                         if _snippet(value, term, insensitive))
            source = {"content": doc["content"], "frontmatter": doc["fm"],
                      "title": doc["title"] or ""}[field]
            hits.setdefault(f"doc:{doc['id']}", {
                "item_id": f"doc:{doc['id']}", "kind": "document", "object_ref": doc["ref"],
                "alias": doc["doc_key"], "version": doc["version"], "field": field,
                "snippet": _snippet(source, term, insensitive)})
        versions = (
            await session.execute(
                text("SELECT v.document_id, v.version, v.content, v.title, CAST(v.frontmatter "
                     "AS text) AS fm, COALESCE(d.canonical_id, CAST(d.id AS text)) AS ref, "
                     "d.doc_key FROM document_versions v JOIN shared_documents d ON "
                     "d.id = v.document_id WHERE CAST(v.workspace_id AS text) = ANY(:ws) AND "
                     f"v.version < d.version AND ({_match_sql('v.content', insensitive)} OR "
                     f"{_match_sql('v.title', insensitive)} OR "
                     f"{_match_sql('CAST(v.frontmatter AS text)', insensitive)})"), params)
        ).mappings().all()
        for version in versions:
            key = f"ver:{version['document_id']}:{version['version']}"
            field = next((name for name, value in (("content", version["content"]),
                                                   ("title", version["title"] or ""),
                                                   ("frontmatter", version["fm"] or ""))
                          if _snippet(value, term, insensitive)), "content")
            source = {"content": version["content"], "title": version["title"] or "",
                      "frontmatter": version["fm"] or ""}[field]
            hits.setdefault(key, {
                "item_id": key, "kind": "version", "object_ref": version["ref"],
                "alias": version["doc_key"], "version": version["version"], "field": field,
                "snippet": _snippet(source, term, insensitive)})
        description = _match_sql("COALESCE(description, '')", insensitive)
        tasks = (
            await session.execute(
                text("SELECT id, COALESCE(canonical_id, CAST(id AS text)) AS ref, task_key, "
                     "title, COALESCE(description, '') AS description FROM collab_tasks WHERE "
                     "CAST(workspace_id AS text) = ANY(:ws) AND ("
                     f"{_match_sql('title', insensitive)} OR {description})"), params)
        ).mappings().all()
        for task in tasks:
            field = "title" if _snippet(task["title"], term, insensitive) else "description"
            hits.setdefault(f"task:{task['id']}", {
                "item_id": f"tasktext:{task['id']}", "kind": "task", "object_ref": task["ref"],
                "alias": task["task_key"], "version": None, "field": field,
                "snippet": _snippet(task[field], term, insensitive)})
        events = (
            await session.execute(
                text("SELECT id, event_id, CAST(payload AS text) AS body FROM workspace_events "
                     "WHERE CAST(workspace_id AS text) = ANY(:ws) AND "
                     f"{_match_sql('CAST(payload AS text)', insensitive)} LIMIT 500"), params)
        ).mappings().all()
        for event in events:
            hits.setdefault(f"event:{event['id']}", {
                "item_id": f"event:{event['id']}", "kind": "event",
                "object_ref": event["event_id"], "alias": None, "version": None,
                "field": "payload", "snippet": _snippet(event["body"], term, insensitive)})
    return sorted(hits.values(), key=lambda item: item["item_id"])


async def locate(session: AsyncSession, principal: Principal, request_id: str) -> dict:
    row = await _load(session, principal, request_id, open_only=True)
    workspaces = await _workspaces(session, await _tenants(session, row))
    subject = row["subject"]
    records = await _subject_records(session, workspaces, subject)
    terms = _terms(subject, records)
    inventory = {
        "structured": await _structured(session, workspaces, records),
        "unstructured": await _unstructured(session, workspaces, terms),
        "searched_terms": len(terms),
        "matching": "exact; emails and phones ignore case; names match the full string",
    }
    counts = {
        "principals": len(inventory["structured"]["principals"]),
        "memberships": len(inventory["structured"]["memberships"]),
        "tokens": len(inventory["structured"]["tokens"]),
        "stamped_documents": inventory["structured"]["lineage_stamps"]["documents"],
        "documents": sum(1 for hit in inventory["unstructured"] if hit["kind"] == "document"),
        "versions": sum(1 for hit in inventory["unstructured"] if hit["kind"] == "version"),
        "tasks": sum(1 for hit in inventory["unstructured"] if hit["kind"] == "task"),
        "events": sum(1 for hit in inventory["unstructured"] if hit["kind"] == "event"),
    }
    await session.execute(
        text("UPDATE rights_requests SET status = 'located', inventory = CAST(:i AS jsonb), "
             "located_at = NOW(), counts = CAST(:c AS jsonb), updated_at = NOW() "
             "WHERE id = :id"),
        {"id": request_id, "i": json.dumps(inventory, default=str), "c": json.dumps(counts)},
    )
    await _event(session, request_id, "located", row["_actor"], counts)
    await _audit(session, row["tenant_id"], "rights_request.located",
                 {"request_id": request_id, **counts})
    if workspaces:
        from app.services.common import append_event

        await append_event(session, workspace_id=uuid.UUID(workspaces[0]), agent="rights",
                           event_type="RIGHTS_REQUEST_LOCATED",
                           payload={"request_id": request_id, "counts": counts,
                                    "targets": [row["handler"]]})
    await session.commit()
    return {"request_id": request_id, "status": "located", "counts": counts,
            "inventory": inventory}


def _require_located(row: dict[str, Any]) -> dict[str, Any]:
    if not row["inventory"]:
        raise ApiError(409, "locate_required", "locate the person's data first",
                       details={"run": "locate_personal_data"})
    return row["inventory"]


# ------------------------------------------------------------------- export


async def export(session: AsyncSession, principal: Principal, request_id: str,
                 items: list[str] | None = None) -> dict[str, Any]:
    """An agentgraph-export/1 subset with only the inventory's objects, plus summary.md."""
    from app.services import exports as export_service

    row = await _load(session, principal, request_id, open_only=True)
    inventory = _require_located(row)
    wanted = set(items) if items else None
    files: dict[str, bytes] = {}
    objects: list[str] = []
    versions: list[str] = []
    events: list[str] = []
    documents_manifest = []

    def keep(item_id: str) -> bool:
        return wanted is None or item_id in wanted

    for hit in inventory["unstructured"]:
        if not keep(hit["item_id"]):
            continue
        if hit["kind"] == "document":
            doc = (
                await session.execute(
                    text("SELECT * FROM shared_documents WHERE id = CAST(:id AS uuid)"),
                    {"id": hit["item_id"].split(":", 1)[1]},
                )
            ).mappings().one_or_none()
            if doc is None:
                continue
            doc = dict(doc)
            path = export_service._doc_path(doc)
            files[path] = export_service._render_document(doc)
            documents_manifest.append({"id": doc.get("canonical_id"), "alias": doc["doc_key"],
                                       "namespace": doc["namespace"],
                                       "version": doc["version"],
                                       "content_hash": doc["content_hash"], "path": path})
        elif hit["kind"] == "version":
            _, document_id, version = hit["item_id"].split(":")
            entry = (
                await session.execute(
                    text("SELECT document_id, version, content, content_hash, created_at "
                         "FROM document_versions WHERE document_id = CAST(:d AS uuid) "
                         "AND version = :v"),
                    {"d": document_id, "v": int(version)},
                )
            ).mappings().one_or_none()
            if entry is None:
                continue
            body = (entry["content"] or "").encode("utf-8")
            digest = hashlib.sha256(body).hexdigest()
            files[f"blobs/{digest}"] = body
            meta = {key: value for key, value in dict(entry).items() if key != "content"}
            versions.append(json.dumps({"table": "document_versions", "row": meta,
                                        "blob": digest}, default=str))
        elif hit["kind"] == "event":
            event = (
                await session.execute(
                    text("SELECT event_id, type, created_at, payload FROM workspace_events "
                         "WHERE id = :id"), {"id": int(hit["item_id"].split(":")[1])})
            ).mappings().one_or_none()
            if event is not None:
                events.append(json.dumps({"table": "workspace_events", "row": dict(event)},
                                         default=str))
        elif hit["kind"] == "task":
            task = (
                await session.execute(
                    text("SELECT canonical_id, task_key, title, description, status, "
                         "created_at FROM collab_tasks WHERE id = CAST(:id AS uuid)"),
                    {"id": hit["item_id"].split(":", 1)[1]})
            ).mappings().one_or_none()
            if task is not None:
                objects.append(json.dumps({"table": "collab_tasks", "row": dict(task)},
                                          default=str))
    structured = inventory["structured"]
    for principal_item in structured["principals"]:
        if not keep(principal_item["item_id"]):
            continue
        if principal_item["type"] == "human":
            user = (
                await session.execute(
                    text("SELECT id, canonical_id, email, display_name, role, created_at, "
                         "last_login_at FROM human_users WHERE id = CAST(:id AS uuid)"),
                    {"id": principal_item["item_id"].rsplit(":", 1)[1]})
            ).mappings().one_or_none()
            if user is not None:
                objects.append(json.dumps({"table": "human_users", "row": dict(user)},
                                          default=str))
    for membership in structured["memberships"]:
        if keep(membership["item_id"]):
            objects.append(json.dumps({"table": "memberships", "row": membership}))
    for token in structured["tokens"]:
        if keep(token["item_id"]):
            objects.append(json.dumps({"table": "agent_tokens", "row": token}))
    for name, lines in (("objects.jsonl", objects), ("versions.jsonl", versions),
                        ("events.jsonl", events)):
        if lines:
            files[name] = ("\n".join(lines) + "\n").encode("utf-8")
    files["summary.md"] = _summary(row, inventory).encode("utf-8")
    manifest = {
        "format": export_service.FORMAT,
        "export_id": f"rr-{request_id}",
        "kind": "full",
        "subset": "personal_data",
        "request_id": request_id,
        "tenant_id": row["tenant_id"],
        "as_of": _now().isoformat(),
        "documents": documents_manifest,
        "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                  for name, data in sorted(files.items())},
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2,
                                default=str).encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", manifest_bytes)
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    data = buffer.getvalue()
    expires = _now() + EXPORT_TTL
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    await session.execute(
        text("INSERT INTO rights_exports (request_id, data, manifest_sha256, expires_at) "
             "VALUES (:r, :d, :m, :x) ON CONFLICT (request_id) DO UPDATE SET data = "
             "EXCLUDED.data, manifest_sha256 = EXCLUDED.manifest_sha256, "
             "created_at = NOW(), expires_at = EXCLUDED.expires_at"),
        {"r": request_id, "d": data, "m": manifest_sha, "x": expires},
    )
    await _event(session, request_id, "exported", row["_actor"],
                 {"bytes": len(data), "files": len(files)})
    await _audit(session, row["tenant_id"], "rights_request.exported",
                 {"request_id": request_id, "bytes": len(data)})
    await session.commit()
    return {"request_id": request_id, "bytes": len(data), "manifest_sha256": manifest_sha,
            "expires_at": expires, "files": sorted(files)}


def _summary(row: dict[str, Any], inventory: dict[str, Any]) -> str:
    structured = inventory["structured"]
    lines = [
        f"# Personal data export ({row['id']})",
        "",
        f"Request type: {row['type']}. Received {row['received_at']:%Y-%m-%d}.",
        "",
        "## Categories of data",
        "",
        f"- Account and directory records: {len(structured['principals'])}",
        f"- Memberships: {len(structured['memberships'])}",
        f"- Agent tokens issued by this person: {len(structured['tokens'])}",
        f"- Documents written (lineage stamps): {structured['lineage_stamps']['documents']}",
        f"- Audit entries: {structured['audit_entries']}",
        f"- Documents, versions, tasks and events mentioning this person: "
        f"{len(inventory['unstructured'])}",
        "",
        "## Sources and purposes",
        "",
        "Accounts and memberships come from sign-up and invitations; documents, tasks and "
        "events were written in this organisation's workspaces by people and agents, for "
        "the purposes stated in the workspace constitution and namespace readmes.",
        "",
        "## Retention",
        "",
        "Workspace content is kept until the organisation deletes it; events for the "
        "configured retention window; backups for 30 days. Billing records are kept as the "
        "law requires.",
        "",
    ]
    return "\n".join(lines)


async def download_export(session: AsyncSession, principal: Principal,
                          request_id: str) -> bytes:
    row = await _load(session, principal, request_id)
    if row["_actor"] != row["handler"]:
        raise Forbidden("only the request's handler downloads the export")
    data = await session.scalar(
        text("SELECT data FROM rights_exports WHERE request_id = :r AND expires_at > NOW()"),
        {"r": request_id},
    )
    if data is None:
        raise NotFound("export", "no current export for this request (links last 7 days)")
    return bytes(data)


# -------------------------------------------------------------------- erase


async def _disable(session: AsyncSession, tables: set[str]) -> None:
    for table in sorted(tables):
        await session.execute(text(f'ALTER TABLE "{table}" DISABLE TRIGGER USER'))


async def _enable(session: AsyncSession, tables: set[str]) -> None:
    for table in sorted(tables):
        await session.execute(text(f'ALTER TABLE "{table}" ENABLE TRIGGER USER'))


async def sweep_labels(session: AsyncSession, replacements: dict[str, str]) -> dict[str, int]:
    """Replace identifiers in every label column (not customer content); exact, any case."""
    columns = (
        await session.execute(
            text("SELECT c.table_name, c.column_name, c.data_type, c.udt_name FROM "
                 "information_schema.columns c JOIN information_schema.tables t ON "
                 "t.table_name = c.table_name AND t.table_schema = c.table_schema "
                 "WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE' AND "
                 "(c.data_type IN ('text', 'character varying', 'jsonb') OR "
                 "(c.data_type = 'ARRAY' AND c.udt_name IN ('_text', '_varchar')))")
        )
    ).all()
    touched: dict[str, int] = {}
    tables: set[str] = set()
    plan = []
    for table, column, data_type, udt in columns:
        if table in SWEEP_SKIP_TABLES or (table, column) in CONTENT_COLUMNS:
            continue
        plan.append((table, column, data_type, udt))
        tables.add(table)
    await _disable(session, tables)
    try:
        for old, new in replacements.items():
            if not old:
                continue
            pattern = re.escape(old)
            for table, column, data_type, _udt in plan:
                if data_type == "jsonb":
                    sql = (f'UPDATE "{table}" SET "{column}" = CAST(regexp_replace(CAST("{column}" '
                           f'AS text), :pattern, :json_new, \'gi\') AS jsonb) WHERE '
                           f'strpos(lower(CAST("{column}" AS text)), lower(:old)) > 0')
                    params = {"pattern": pattern, "old": old,
                              "json_new": json.dumps(new)[1:-1]}
                elif data_type == "ARRAY":
                    sql = (f'UPDATE "{table}" SET "{column}" = ARRAY(SELECT regexp_replace(x, '
                           f':pattern, :new, \'gi\') FROM unnest("{column}") AS x) WHERE '
                           f'strpos(lower(array_to_string("{column}", chr(31))), lower(:old)) > 0')
                    params = {"pattern": pattern, "old": old, "new": new}
                else:
                    sql = (f'UPDATE "{table}" SET "{column}" = regexp_replace("{column}", '
                           f':pattern, :new, \'gi\') '
                           f'WHERE strpos(lower("{column}"), lower(:old)) > 0')
                    params = {"pattern": pattern, "old": old, "new": new}
                result = await session.execute(text(sql), params)
                if result.rowcount:
                    key = f"{table}.{column}"
                    touched[key] = touched.get(key, 0) + int(result.rowcount)
    finally:
        await _enable(session, tables)
    return touched


def _pseudonym() -> str:
    return f"former-member-{secrets.token_hex(4)}"


async def _pseudonymise(session: AsyncSession, row: dict[str, Any], inventory: dict,
                        wanted: set[str] | None) -> dict[str, Any]:
    done = []
    for item in inventory["structured"]["principals"]:
        if wanted is not None and item["item_id"] not in wanted:
            continue
        pseudonym = _pseudonym()
        if item["type"] == "human":
            user_id = item["item_id"].rsplit(":", 1)[1]
            user = (
                await session.execute(
                    text("SELECT email, display_name FROM human_users WHERE id = "
                         "CAST(:id AS uuid)"), {"id": user_id})
            ).mappings().one_or_none()
            if user is None:
                continue
            email = f"{pseudonym}@redacted.invalid"
            replacements = {user["email"]: email}
            if user["display_name"] and len(user["display_name"]) >= 3:
                replacements[user["display_name"]] = pseudonym
            await sweep_labels(session, replacements)
            await session.execute(
                text("UPDATE human_users SET email = :e, display_name = :n, google_sub = NULL "
                     "WHERE id = CAST(:id AS uuid)"),
                {"e": email, "n": pseudonym, "id": user_id},
            )
            await _log(session, row, "pseudonymise", "human_users", user_id, None, None,
                       {"email": email, "display_name": pseudonym})
        else:
            principal_id = item["item_id"].rsplit(":", 1)[1]
            agent = (
                await session.execute(
                    text("SELECT agent_id, display_name, signatures FROM agent_directory "
                         "WHERE principal_id = :p"), {"p": principal_id})
            ).mappings().one_or_none()
            if agent is None:
                continue
            replacements = {agent["agent_id"]: pseudonym}
            for signature in agent["signatures"] or []:
                replacements[signature] = pseudonym
            if agent["display_name"] and len(agent["display_name"]) >= 3:
                replacements[agent["display_name"]] = pseudonym
            await session.execute(text("DELETE FROM agent_aliases WHERE principal_id = :p"),
                                  {"p": principal_id})
            await sweep_labels(session, replacements)
            await _log(session, row, "pseudonymise", "agent_directory", principal_id, None,
                       None, {"agent_id": pseudonym})
        done.append({"item_id": item["item_id"], "pseudonym": pseudonym})
    return {"pseudonymised": done}


def _redact_text(body: str, terms: list[tuple[str, bool]]) -> str:
    for term, insensitive in terms:
        body = re.sub(re.escape(term), PLACEHOLDER, body,
                      flags=re.IGNORECASE if insensitive else 0)
    return body


async def _redact(session: AsyncSession, row: dict[str, Any], inventory: dict,
                  terms: list[tuple[str, bool]], wanted: set[str] | None) -> dict[str, Any]:
    from app.services.documents import content_hash

    counts = {"documents": 0, "versions": 0, "tasks": 0, "events": 0}
    await _disable(session, {"document_versions", "workspace_events"})
    try:
        for hit in inventory["unstructured"]:
            if wanted is not None and hit["item_id"] not in wanted:
                continue
            if hit["kind"] == "version":
                _, document_id, version = hit["item_id"].split(":")
                current = (
                    await session.execute(
                        text("SELECT content, title, CAST(frontmatter AS text) AS fm FROM "
                             "document_versions WHERE document_id = CAST(:d AS uuid) AND "
                             "version = :v"),
                        {"d": document_id, "v": int(version)})
                ).mappings().one_or_none()
                if current is None:
                    continue
                redacted = _redact_text(current["content"], terms)
                title = _redact_text(current["title"] or "", terms)
                frontmatter = _redact_text(current["fm"] or "{}", [
                    (json.dumps(term)[1:-1], flag) for term, flag in terms])
                digest = hashlib.sha256(redacted.encode("utf-8")).hexdigest()
                await session.execute(
                    text("UPDATE document_versions SET content = :c, title = :t, frontmatter = "
                         "CAST(:f AS jsonb), redacted_at = NOW(), redaction_hash = :h "
                         "WHERE document_id = CAST(:d AS uuid) AND version = :v"),
                    {"c": redacted, "t": title, "f": frontmatter, "h": digest,
                     "d": document_id, "v": int(version)})
                await _log(session, row, "redact", "document_versions", document_id,
                           int(version), "content",
                           {"content": redacted, "title": title,
                            "frontmatter": json.loads(frontmatter)}, digest)
                counts["versions"] += 1
            elif hit["kind"] == "event":
                event_id = int(hit["item_id"].split(":")[1])
                body = await session.scalar(
                    text("SELECT CAST(payload AS text) FROM workspace_events WHERE id = :id"),
                    {"id": event_id})
                if body is None:
                    continue
                redacted = _redact_text(body, [(json.dumps(term)[1:-1], flag)
                                               for term, flag in terms])
                await session.execute(
                    text("UPDATE workspace_events SET payload = CAST(:p AS jsonb) "
                         "WHERE id = :id"), {"p": redacted, "id": event_id})
                await _log(session, row, "redact", "workspace_events", str(event_id), None,
                           "payload", {"payload": json.loads(redacted)})
                counts["events"] += 1
    finally:
        await _enable(session, {"document_versions", "workspace_events"})
    for hit in inventory["unstructured"]:
        if wanted is not None and hit["item_id"] not in wanted:
            continue
        if hit["kind"] == "document":
            document_id = hit["item_id"].split(":", 1)[1]
            doc = (
                await session.execute(
                    text("SELECT version, content, title, CAST(frontmatter AS text) AS fm FROM "
                         "shared_documents WHERE id = CAST(:id AS uuid) FOR UPDATE"),
                    {"id": document_id})
            ).mappings().one_or_none()
            if doc is None:
                continue
            body = _redact_text(doc["content"], terms)
            title = _redact_text(doc["title"] or "", terms) or None
            frontmatter = _redact_text(doc["fm"] or "{}", [(json.dumps(term)[1:-1], flag)
                                                           for term, flag in terms])
            version = doc["version"] + 1
            digest = content_hash(body)
            # A new version through the normal ledger: stamped, versioned, evented.
            await session.execute(
                text("UPDATE shared_documents SET content = :c, title = COALESCE(:t, title), "
                     "frontmatter = CAST(:f AS jsonb), version = :v, content_hash = :h, "
                     "redacted_at = NOW(), updated_by = 'rights-redaction', updated_at = NOW() "
                     "WHERE id = CAST(:id AS uuid)"),
                {"c": body, "t": title, "f": frontmatter, "v": version, "h": digest,
                 "id": document_id})
            await session.execute(
                text("INSERT INTO document_versions (document_id, workspace_id, version, "
                     "parent_version, title, content, frontmatter, namespace, author, "
                     "content_hash, change_summary, redacted_at, redaction_hash) SELECT id, "
                     "workspace_id, :v, :v - 1, title, content, frontmatter, namespace, "
                     "'rights-redaction', content_hash, 'personal data redacted', NOW(), "
                     "content_hash FROM shared_documents WHERE id = CAST(:id AS uuid)"),
                {"v": version, "id": document_id})
            await _log(session, row, "redact", "shared_documents", document_id, version,
                       "content", {"content": body, "title": title,
                                   "frontmatter": json.loads(frontmatter)}, digest)
            counts["documents"] += 1
            # The version that was current until now holds the same data.
            counts["versions"] += await _redact_older_versions(
                session, row, document_id, version, terms)
        elif hit["kind"] == "task":
            task_id = hit["item_id"].split(":", 1)[1]
            task = (
                await session.execute(
                    text("SELECT title, description FROM collab_tasks WHERE id = "
                         "CAST(:id AS uuid)"), {"id": task_id})
            ).mappings().one_or_none()
            if task is None:
                continue
            title = _redact_text(task["title"], terms)
            description = _redact_text(task["description"] or "", terms) or None
            await session.execute(
                text("UPDATE collab_tasks SET title = :t, description = :d, "
                     "version = version + 1, updated_at = NOW() WHERE id = CAST(:id AS uuid)"),
                {"t": title, "d": description, "id": task_id})
            await _log(session, row, "redact", "collab_tasks", task_id, None, "text",
                       {"title": title, "description": description})
            counts["tasks"] += 1
    return {"redacted": counts}


async def _redact_older_versions(session: AsyncSession, row: dict[str, Any],
                                 document_id: str, newest: int,
                                 terms: list[tuple[str, bool]]) -> int:
    versions = (
        await session.execute(
            text("SELECT version, content, title, CAST(frontmatter AS text) AS fm FROM "
                 "document_versions WHERE document_id = CAST(:d AS uuid) AND version < :v "
                 "AND redacted_at IS NULL"),
            {"d": document_id, "v": newest})
    ).mappings().all()
    json_terms = [(json.dumps(term)[1:-1], flag) for term, flag in terms]
    changed = 0
    await _disable(session, {"document_versions"})
    try:
        for version in versions:
            content = _redact_text(version["content"], terms)
            title = _redact_text(version["title"] or "", terms)
            frontmatter = _redact_text(version["fm"] or "{}", json_terms)
            if (content, title, frontmatter) == (version["content"], version["title"] or "",
                                                 version["fm"] or "{}"):
                continue
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            await session.execute(
                text("UPDATE document_versions SET content = :c, title = :t, frontmatter = "
                     "CAST(:f AS jsonb), redacted_at = NOW(), redaction_hash = :h "
                     "WHERE document_id = CAST(:d AS uuid) AND version = :v"),
                {"c": content, "t": title, "f": frontmatter, "h": digest, "d": document_id,
                 "v": version["version"]})
            await _log(session, row, "redact", "document_versions", document_id,
                       version["version"], "content",
                       {"content": content, "title": title,
                        "frontmatter": json.loads(frontmatter)}, digest)
            changed += 1
    finally:
        await _enable(session, {"document_versions"})
    return changed


async def _delete(session: AsyncSession, principal: Principal, row: dict[str, Any],
                  inventory: dict, wanted: set[str] | None) -> dict[str, Any]:
    from app.models import SharedDocument
    from app.services import addresses as address_service

    deleted = []
    for hit in inventory["unstructured"]:
        if hit["kind"] != "document" or (wanted is not None and hit["item_id"] not in wanted):
            continue
        document = await session.get(SharedDocument, uuid.UUID(hit["item_id"].split(":", 1)[1]))
        if document is None:
            continue
        if document.archived_at is None:
            document.archived_at = _now()
            document.archived_by = "rights-request"
            document.archive_reason = f"rights request {row['id']}"
            await session.flush()
        await address_service.tombstone_document(
            session, document, purged_by="rights-request", reason=f"rights request {row['id']}")
        await session.delete(document)
        await session.flush()
        await _log(session, row, "delete", "shared_documents", hit["item_id"].split(":", 1)[1],
                   None, None, {"tombstone": True})
        deleted.append(hit["object_ref"])
    return {"deleted": deleted}


async def _log(session: AsyncSession, row: dict[str, Any], kind: str, table: str,
               target: str, version: int | None, field: str | None,
               replacement: dict[str, Any], digest: str | None = None) -> None:
    await session.execute(
        text("INSERT INTO redaction_log (request_id, tenant_id, kind, target_table, target_id, "
             "version, field, replacement, redaction_hash) VALUES (:r, :t, :k, :tb, :id, :v, "
             ":f, CAST(:rep AS jsonb), :h)"),
        {"r": row["id"], "t": row["tenant_id"], "k": kind, "tb": table, "id": target,
         "v": version, "f": field, "rep": json.dumps(replacement, default=str), "h": digest},
    )


async def erase(session: AsyncSession, principal: Principal, request_id: str, *,
                mode: str, items: list[str] | None = None) -> dict[str, Any]:
    row = await _load(session, principal, request_id, open_only=True)
    inventory = _require_located(row)
    if mode not in MODES:
        raise ApiError(422, "validation_error", "mode is pseudonymise, redact or delete",
                       details={"field": "mode", "allowed": list(MODES)})
    wanted = set(items) if items else None
    workspaces = await _workspaces(session, await _tenants(session, row))
    records = await _subject_records(session, workspaces, row["subject"])
    terms = _terms(row["subject"], records)
    if mode == "pseudonymise":
        result = await _pseudonymise(session, row, inventory, wanted)
    elif mode == "redact":
        result = await _redact(session, row, inventory, terms, wanted)
    else:
        result = await _delete(session, principal, row, inventory, wanted)
    result["legal_retention"] = [{"category": "usage_records",
                                  "reason": inventory["structured"]["usage_records"]["reason"]}]
    await session.execute(
        text("UPDATE rights_requests SET status = 'fulfilled', updated_at = NOW() "
             "WHERE id = :id"), {"id": request_id})
    await _event(session, request_id, mode, row["_actor"],
                 {key: value for key, value in result.items() if key != "pseudonymised"})
    await _audit(session, row["tenant_id"], f"rights_request.{mode}",
                 {"request_id": request_id, "items": len(items or []) or "all"})
    await session.commit()
    return {"request_id": request_id, "mode": mode, **result}


# ------------------------------------------------------------------ explain


async def explain(session: AsyncSession, principal: Principal, request_id: str) -> dict:
    row = await _load(session, principal, request_id)
    inventory = _require_located(row)
    refs = [hit["object_ref"] for hit in inventory["unstructured"]
            if hit["kind"] in ("document", "version")]
    agents = (
        await session.execute(
            text("SELECT DISTINCT v.stamp->>'principal_id' AS principal, v.stamp->>'token_id' "
                 "AS token, d.namespace FROM document_versions v JOIN shared_documents d ON "
                 "d.id = v.document_id WHERE COALESCE(d.canonical_id, CAST(d.id AS text)) = "
                 "ANY(:refs)"), {"refs": refs})
    ).mappings().all()
    namespaces = sorted({item["namespace"] for item in agents})
    purposes = []
    for namespace in namespaces:
        readme = await session.scalar(
            text("SELECT content FROM shared_documents WHERE namespace = 'system' AND "
                 "doc_key = :k AND workspace_id = ANY(CAST(:ws AS uuid[])) LIMIT 1"),
            {"k": f"system/namespaces/{namespace}",
             "ws": await _workspaces(session, await _tenants(session, row))})
        purposes.append({"namespace": namespace,
                         "purpose": (readme or "").strip().splitlines()[0][:200]
                         if readme else None})
    return {
        "request_id": request_id,
        "categories": {key: value for key, value in (row["counts"] or {}).items()},
        "processed_by": [{"principal_id": item["principal"], "token_id": item["token"]}
                         for item in agents],
        "purposes": purposes,
        "legal_retention": inventory["structured"]["usage_records"],
    }


# --------------------------------------------------------- reminders, upkeep


async def upkeep() -> dict[str, int]:
    """Due reminders, overdue notices and subject minimization 30 days after closing."""
    from app.services import alerts

    reminded = minimized = 0
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                text("SELECT * FROM rights_requests WHERE status NOT IN ('closed', 'rejected')")
            )
        ).mappings().all()
        for row in rows:
            remaining = business_days_between(_now(), row["due_at"])
            sent = set(row["reminders_sent"] or [])
            key = None
            if _now() > row["due_at"] and "overdue" not in sent:
                key = "overdue"
            elif remaining <= REMIND_REMAINING_DAYS and "handler" not in sent:
                key = "handler"
            if key is None:
                continue
            await alerts.raise_alert(
                f"rights_request.{key}",
                f"rights request {row['id']} is {'overdue' if key == 'overdue' else 'due soon'}",
                severity="critical" if key == "overdue" else "warning",
                details={"request_id": row["id"], "tenant_id": row["tenant_id"],
                         "due_at": row["due_at"], "handler": row["handler"]},
                dedupe_key=f"rights:{row['id']}:{key}",
            )
            sent.add(key)
            reminded += 1
            await session.execute(
                text("UPDATE rights_requests SET reminders_sent = CAST(:s AS jsonb) "
                     "WHERE id = :id"), {"s": json.dumps(sorted(sent)), "id": row["id"]})
            await _event(session, row["id"], f"reminder.{key}", "system")
        closed = (
            await session.execute(
                text("SELECT id, subject, inventory FROM rights_requests WHERE status = 'closed' "
                     "AND minimized_at IS NULL AND closed_at < :cutoff"),
                {"cutoff": _now() - MINIMIZE_AFTER})
        ).mappings().all()
        for row in closed:
            digest = hashlib.sha256(json.dumps(row["subject"], sort_keys=True)
                                    .encode()).hexdigest()
            await session.execute(
                text("UPDATE rights_requests SET subject = CAST(:s AS jsonb), subject_hash = :h, "
                     "inventory = NULL, minimized_at = NOW() WHERE id = :id"),
                {"s": json.dumps({"minimized": True, "sha256": digest}), "h": digest,
                 "id": row["id"]})
            await _event(session, row["id"], "minimized", "system")
            minimized += 1
        await session.commit()
    return {"reminded": reminded, "minimized": minimized}


# ------------------------------------------------------------------- replay


async def replay(session: AsyncSession, tenant_ids: list[str],
                 source_factory: Any = None) -> int:
    """Re-apply erasures to restored data (AG-TC-07 point 7); returns entries applied.

    ``session`` is the restored database; the log is read from production.
    """
    factory = source_factory or async_session_factory
    async with factory() as source:
        entries = (
            await source.execute(
                text("SELECT * FROM redaction_log WHERE tenant_id = ANY(:t) ORDER BY id"),
                {"t": tenant_ids})
        ).mappings().all()
    applied = 0
    await _disable(session, {"document_versions", "workspace_events"})
    try:
        for entry in entries:
            replacement = entry["replacement"]
            table, target = entry["target_table"], entry["target_id"]
            if entry["kind"] == "pseudonymise" and table == "human_users":
                user = (
                    await session.execute(
                        text("SELECT email, display_name FROM human_users WHERE id = "
                             "CAST(:id AS uuid)"), {"id": target})
                ).mappings().one_or_none()
                if user is None or user["email"] == replacement["email"]:
                    continue
                replacements = {user["email"]: replacement["email"]}
                if user["display_name"] and len(user["display_name"]) >= 3:
                    replacements[user["display_name"]] = replacement["display_name"]
                await _enable(session, {"document_versions", "workspace_events"})
                await sweep_labels(session, replacements)
                await _disable(session, {"document_versions", "workspace_events"})
                await session.execute(
                    text("UPDATE human_users SET email = :e, display_name = :n "
                         "WHERE id = CAST(:id AS uuid)"),
                    {"e": replacement["email"], "n": replacement["display_name"], "id": target})
            elif table == "document_versions":
                await session.execute(
                    text("UPDATE document_versions SET content = :c, "
                         "title = COALESCE(:t, title), frontmatter = COALESCE(CAST(:f AS jsonb), "
                         "frontmatter), redacted_at = NOW(), redaction_hash = :h "
                         "WHERE document_id = CAST(:d AS uuid) AND version = :v"),
                    {"c": replacement["content"], "t": replacement.get("title"),
                     "f": json.dumps(replacement["frontmatter"])
                     if replacement.get("frontmatter") is not None else None,
                     "h": entry["redaction_hash"], "d": target, "v": entry["version"]})
            elif table == "shared_documents" and entry["kind"] == "redact":
                # The restore point may predate the redacted version: rewrite the current
                # body (and its version row) with the redacted text.
                fields = {"c": replacement["content"], "t": replacement.get("title"),
                          "f": json.dumps(replacement["frontmatter"])
                          if replacement.get("frontmatter") is not None else None,
                          "h": entry["redaction_hash"], "id": target}
                await session.execute(
                    text("UPDATE shared_documents SET content = :c, title = COALESCE(:t, title), "
                         "frontmatter = COALESCE(CAST(:f AS jsonb), frontmatter), "
                         "redacted_at = NOW() WHERE id = CAST(:id AS uuid)"), fields)
                await session.execute(
                    text("UPDATE document_versions v SET content = :c, "
                         "title = COALESCE(:t, v.title), frontmatter = COALESCE(CAST(:f AS jsonb), "
                         "v.frontmatter), redacted_at = NOW(), redaction_hash = :h "
                         "FROM shared_documents d WHERE d.id = v.document_id "
                         "AND d.id = CAST(:id AS uuid) AND v.version = d.version"), fields)
            elif table == "collab_tasks":
                await session.execute(
                    text("UPDATE collab_tasks SET title = :t, description = :d "
                         "WHERE id = CAST(:id AS uuid)"),
                    {"t": replacement["title"], "d": replacement["description"], "id": target})
            elif table == "workspace_events":
                await session.execute(
                    text("UPDATE workspace_events SET payload = CAST(:p AS jsonb) "
                         "WHERE id = :id"),
                    {"p": json.dumps(replacement["payload"]), "id": int(target)})
            applied += 1
    finally:
        await _enable(session, {"document_versions", "workspace_events"})
    return applied
