"""Security incidents: scope, notices within 72 hours, the record (PRD AG-TC-06).

Platform admins only. The 72-hour clock starts at ``confirmed_at``. Scope
resolution turns inputs (time window, tokens, principals, objects, shards,
tables, components) into the affected tenant nodes, using the per-token
access log (reads), lineage stamps (writes) and object ownership. Notices go
to each affected node's owners (inherited from ancestors included), the
root's owners and the root's security contacts, on three channels: email, a
``security.incident_notice`` event and the console banner. Notice rows are
append-only (a database trigger refuses changes to their content); each
node's notice describes only that node and its subtree.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session_factory
from app.errors import ApiError, Forbidden, NotFound
from app.services import access_log, alerts
from app.services.common import append_event
from app.services.mailer import queue_email

settings = get_settings()

SEVERITIES = ("low", "medium", "high", "critical")
CHECKPOINTS_HOURS = (24, 48, 60)
SLA_HOURS = 72
# Components name groups of tables an incident can expose wholesale.
COMPONENTS: dict[str, tuple[str, ...]] = {
    "documents": ("shared_documents",),
    "search_index": ("shared_documents",),
    "tasks": ("collab_tasks",),
    "accounts": ("human_users", "memberships"),
    "tokens": ("agent_tokens",),
    "database": ("shared_documents", "collab_tasks", "human_users", "agent_tokens"),
}
SCOPE_TABLES = ("shared_documents", "collab_tasks", "human_users", "memberships",
                "agent_tokens")
REQUIRED_FIELDS = ("summary", "confirmed_at", "measures_taken", "recommended_actions",
                   "contact")
NOTICE_CHANNELS = ["email", "event", "banner"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(content: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def incident_out(row: dict[str, Any]) -> dict[str, Any]:
    deadline = None
    if row["confirmed_at"] is not None:
        from datetime import timedelta

        deadline = row["confirmed_at"] + timedelta(hours=SLA_HOURS)
    return {
        "incident_id": row["id"],
        "title": row["title"],
        "severity": row["severity"],
        "status": row["status"],
        "detected_at": row["detected_at"],
        "confirmed_at": row["confirmed_at"],
        "notify_deadline": deadline,
        "closed_at": row["closed_at"],
        "scope_inputs": row["scope_inputs"],
        "scope_resolved_at": row["scope_resolved_at"],
        "summary": row["summary"],
        "measures_taken": row["measures_taken"],
        "recommended_actions": row["recommended_actions"],
        "contact": row["contact"],
        "owner_admin": row["owner_admin"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def _load(session: AsyncSession, incident_id: str) -> dict[str, Any]:
    row = (
        await session.execute(text("SELECT * FROM incidents WHERE id = :id"), {"id": incident_id})
    ).mappings().one_or_none()
    if row is None:
        raise NotFound("incident", f"incident '{incident_id}' not found")
    return dict(row)


async def _timeline(session: AsyncSession, incident_id: str, kind: str,
                    details: dict[str, Any] | None = None, actor: str = "admin") -> None:
    await session.execute(
        text("INSERT INTO incident_timeline (incident_id, kind, actor, details) "
             "VALUES (:id, :kind, :actor, CAST(:details AS jsonb))"),
        {"id": incident_id, "kind": kind, "actor": actor,
         "details": json.dumps(details or {}, default=str)},
    )


# ------------------------------------------------------------- lifecycle

_EDITABLE = ("title", "severity", "summary", "measures_taken", "recommended_actions",
             "contact", "owner_admin")


async def create_incident(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("severity") not in SEVERITIES:
        raise ApiError(422, "validation_error", "severity must be low, medium, high or critical",
                       details={"field": "severity", "allowed": list(SEVERITIES)})
    async with async_session_factory() as session:
        row = (
            await session.execute(
                text("INSERT INTO incidents (title, severity, detected_at, summary, "
                     "measures_taken, recommended_actions, contact, owner_admin, scope_inputs) "
                     "VALUES (:title, :severity, COALESCE(:detected, NOW()), :summary, "
                     ":measures, :actions, :contact, :owner, CAST(:scope AS jsonb)) RETURNING *"),
                {"title": data["title"], "severity": data["severity"],
                 "detected": data.get("detected_at"), "summary": data.get("summary"),
                 "measures": data.get("measures_taken"),
                 "actions": data.get("recommended_actions"),
                 "contact": data.get("contact"), "owner": data.get("owner_admin"),
                 "scope": json.dumps(data.get("scope_inputs") or {}, default=str)},
            )
        ).mappings().one()
        await _timeline(session, row["id"], "created", {"severity": row["severity"]})
        await session.commit()
    return incident_out(dict(row))


async def update_incident(incident_id: str, data: dict[str, Any]) -> dict[str, Any]:
    changes = {key: value for key, value in data.items() if key in _EDITABLE and value is not None}
    if "severity" in changes and changes["severity"] not in SEVERITIES:
        raise ApiError(422, "validation_error", "severity must be low, medium, high or critical",
                       details={"field": "severity"})
    async with async_session_factory() as session:
        await _load(session, incident_id)
        if changes:
            sets = ", ".join(f"{key} = :{key}" for key in changes)
            await session.execute(
                text(f"UPDATE incidents SET {sets}, updated_at = NOW() WHERE id = :id"),
                {"id": incident_id, **changes},
            )
            await _timeline(session, incident_id, "updated", {"fields": sorted(changes)})
        await session.commit()
        return incident_out(await _load(session, incident_id))


async def confirm_incident(incident_id: str) -> dict[str, Any]:
    async with async_session_factory() as session:
        row = await _load(session, incident_id)
        if row["confirmed_at"] is None:
            await session.execute(
                text("UPDATE incidents SET confirmed_at = NOW(), status = 'confirmed', "
                     "updated_at = NOW() WHERE id = :id"),
                {"id": incident_id},
            )
            await _timeline(session, incident_id, "confirmed")
        await session.commit()
        return incident_out(await _load(session, incident_id))


async def close_incident(incident_id: str) -> dict[str, Any]:
    async with async_session_factory() as session:
        await _load(session, incident_id)
        await session.execute(
            text("UPDATE incidents SET status = 'closed', closed_at = NOW(), updated_at = NOW() "
                 "WHERE id = :id"),
            {"id": incident_id},
        )
        await _timeline(session, incident_id, "closed")
        await session.commit()
        return incident_out(await _load(session, incident_id))


async def list_incidents(limit: int = 100) -> dict[str, Any]:
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                text("SELECT * FROM incidents ORDER BY created_at DESC LIMIT :limit"),
                {"limit": limit},
            )
        ).mappings().all()
    return {"items": [incident_out(dict(row)) for row in rows]}


async def get_incident(incident_id: str) -> dict[str, Any]:
    async with async_session_factory() as session:
        row = await _load(session, incident_id)
        nodes = await _nodes(session, incident_id)
    return {**incident_out(row), "nodes": nodes}


# ------------------------------------------------------------------ scope


def _validate_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    allowed = {"time_from", "time_to", "token_ids", "principal_ids", "object_ids",
               "shard_keys", "tables", "components"}
    unknown = set(inputs) - allowed
    if unknown:
        raise ApiError(422, "validation_error", "unknown scope inputs",
                       details={"unknown": sorted(unknown), "allowed": sorted(allowed)})
    clean: dict[str, Any] = {}
    for key in ("token_ids", "principal_ids", "object_ids", "shard_keys", "tables",
                "components"):
        values = inputs.get(key) or []
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ApiError(422, "validation_error", f"{key} must be a list of strings",
                           details={"field": key})
        clean[key] = sorted(set(values))[:1_000]
    bad_tables = set(clean["tables"]) - set(SCOPE_TABLES)
    if bad_tables:
        raise ApiError(422, "validation_error", "tables must be scope tables",
                       details={"field": "tables", "allowed": list(SCOPE_TABLES)})
    bad_components = set(clean["components"]) - set(COMPONENTS)
    if bad_components:
        raise ApiError(422, "validation_error", "unknown components",
                       details={"field": "components", "allowed": sorted(COMPONENTS)})
    for key in ("time_from", "time_to"):
        value = inputs.get(key)
        if value is None:
            clean[key] = None
            continue
        try:
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
                str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ApiError(422, "validation_error", f"{key} must be an ISO 8601 time",
                           details={"field": key}) from exc
        clean[key] = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    if not any(clean[key] for key in ("token_ids", "principal_ids", "object_ids", "shard_keys",
                                      "tables", "components")):
        raise ApiError(422, "validation_error",
                       "name at least one token, principal, object, shard, table or component",
                       details={"field": "scope_inputs"})
    return clean


class _Acc:
    """Affected objects of one tenant node."""

    def __init__(self) -> None:
        self.documents: set[str] = set()
        self.tasks: set[str] = set()
        self.principals: set[str] = set()
        self.tokens: set[str] = set()
        self.evidence: list[str] = []
        self.personal = False

    def counts(self) -> dict[str, int]:
        return {"documents": len(self.documents), "tasks": len(self.tasks),
                "principals": len(self.principals), "tokens": len(self.tokens)}

    def evidence_ref(self, ref: str) -> None:
        if len(self.evidence) < 50 and ref not in self.evidence:
            self.evidence.append(ref)


async def _compute_scope(session: AsyncSession, inputs: dict[str, Any]) -> dict[str, _Acc]:
    nodes: dict[str, _Acc] = {}

    def node(tenant: str) -> _Acc:
        return nodes.setdefault(tenant, _Acc())

    window = {"t0": inputs["time_from"] or datetime(1970, 1, 1, tzinfo=timezone.utc),
              "t1": inputs["time_to"] or datetime(9999, 1, 1, tzinfo=timezone.utc)}
    tokens = list(inputs["token_ids"])
    if tokens:
        # Accept internal token ids too; stamps and the log use canonical ids.
        mapped = (
            await session.execute(
                text("SELECT canonical_id FROM agent_tokens WHERE CAST(id AS text) = ANY(:ids)"),
                {"ids": tokens},
            )
        ).scalars().all()
        tokens = sorted(set(tokens) | {item for item in mapped if item})
    principals = list(inputs["principal_ids"])
    if tokens or principals:
        params = {**window, "tokens": tokens, "principals": principals}
        for row in (
            await session.execute(
                text("SELECT id, tenant_id, token_id, principal_id, documents, tasks "
                     "FROM token_access_log WHERE (token_id = ANY(:tokens) "
                     "OR principal_id = ANY(:principals)) AND bucket >= date_trunc('minute', "
                     "CAST(:t0 AS timestamptz)) AND bucket <= :t1 ORDER BY id"),
                params,
            )
        ).mappings().all():
            acc = node(row["tenant_id"])
            acc.documents.update(row["documents"])
            acc.tasks.update(row["tasks"])
            acc.principals.add(row["principal_id"])
            if row["token_id"]:
                acc.tokens.add(row["token_id"])
            acc.evidence_ref(f"token_access_log:{row['id']}")
        for row in (
            await session.execute(
                text("SELECT v.id, v.tenant_id, COALESCE(d.canonical_id, CAST(d.id AS text)) "
                     "AS doc, v.stamp->>'token_id' AS token, v.stamp->>'principal_id' AS who "
                     "FROM document_versions v JOIN shared_documents d ON d.id = v.document_id "
                     "WHERE (v.stamp->>'token_id' = ANY(:tokens) "
                     "OR v.stamp->>'principal_id' = ANY(:principals)) "
                     "AND v.created_at BETWEEN :t0 AND :t1"),
                params,
            )
        ).mappings().all():
            acc = node(row["tenant_id"])
            acc.documents.add(row["doc"])
            if row["who"]:
                acc.principals.add(row["who"])
            if row["token"]:
                acc.tokens.add(row["token"])
            acc.evidence_ref(f"document_versions:{row['id']}")
        for row in (
            await session.execute(
                text("SELECT a.id, a.tenant_id, COALESCE(t.canonical_id, "
                     "CAST(a.target_record_id AS text)) AS task, a.stamp->>'token_id' AS token, "
                     "a.stamp->>'principal_id' AS who FROM state_audit_diffs a "
                     "LEFT JOIN collab_tasks t ON t.id = a.target_record_id "
                     "WHERE a.target_table = 'collab_tasks' AND (a.stamp->>'token_id' = "
                     "ANY(:tokens) OR a.stamp->>'principal_id' = ANY(:principals)) "
                     "AND a.created_at BETWEEN :t0 AND :t1"),
                params,
            )
        ).mappings().all():
            acc = node(row["tenant_id"])
            acc.tasks.add(row["task"])
            if row["who"]:
                acc.principals.add(row["who"])
            if row["token"]:
                acc.tokens.add(row["token"])
            acc.evidence_ref(f"state_audit_diffs:{row['id']}")
        for row in (
            await session.execute(
                text("SELECT tenant_id, canonical_id FROM agent_tokens "
                     "WHERE canonical_id = ANY(:tokens) UNION ALL "
                     "SELECT tenant_id, root_token_id FROM workspaces "
                     "WHERE root_token_id = ANY(:tokens)"),
                {"tokens": tokens},
            )
        ).all():
            acc = node(row[0])
            acc.tokens.add(row[1])
            acc.evidence_ref(f"token:{row[1]}")
    objects = list(inputs["object_ids"])
    if objects:
        for table, kind in (("shared_documents", "documents"), ("collab_tasks", "tasks")):
            for row in (
                await session.execute(
                    text(f"SELECT tenant_id, COALESCE(canonical_id, CAST(id AS text)) FROM "
                         f"{table} WHERE canonical_id = ANY(:ids) "
                         f"OR CAST(id AS text) = ANY(:ids)"),
                    {"ids": objects},
                )
            ).all():
                acc = node(row[0])
                getattr(acc, kind).add(row[1])
                acc.evidence_ref(f"{table}:{row[1]}")
        for row in (
            await session.execute(
                text("SELECT tenant_id, canonical_id FROM agent_tokens "
                     "WHERE canonical_id = ANY(:ids) OR CAST(id AS text) = ANY(:ids)"),
                {"ids": objects},
            )
        ).all():
            acc = node(row[0])
            acc.tokens.add(row[1])
            acc.evidence_ref(f"agent_tokens:{row[1]}")
    if inputs["shard_keys"]:
        for row in (
            await session.execute(
                text("SELECT tenant_id, COALESCE(canonical_id, CAST(id AS text)), namespace "
                     "FROM shared_documents WHERE namespace = ANY(:shards)"),
                {"shards": inputs["shard_keys"]},
            )
        ).all():
            acc = node(row[0])
            acc.documents.add(row[1])
            acc.evidence_ref(f"shard:{row[2]}")
    tables = set(inputs["tables"])
    for component in inputs["components"]:
        tables.update(COMPONENTS[component])
    if "shared_documents" in tables:
        for row in (
            await session.execute(text("SELECT tenant_id, COALESCE(canonical_id, CAST(id AS "
                                       "text)) FROM shared_documents"))
        ).all():
            node(row[0]).documents.add(row[1])
    if "collab_tasks" in tables:
        for row in (
            await session.execute(text("SELECT tenant_id, COALESCE(canonical_id, CAST(id AS "
                                       "text)) FROM collab_tasks"))
        ).all():
            node(row[0]).tasks.add(row[1])
    if tables & {"human_users", "memberships"}:
        for row in (
            await session.execute(text("SELECT w.tenant_id, CAST(u.id AS text) FROM human_users u "
                                       "JOIN workspaces w ON w.id = u.workspace_id"))
        ).all():
            acc = node(row[0])
            acc.principals.add(row[1])
            acc.personal = True
    if "agent_tokens" in tables:
        for row in (
            await session.execute(text("SELECT tenant_id, canonical_id FROM agent_tokens"))
        ).all():
            node(row[0]).tokens.add(row[1])
    for table in tables:
        for acc in nodes.values():
            acc.evidence_ref(f"table:{table}")
    return nodes


def _personal(acc: _Acc) -> bool:
    # Principal or membership records are personal information; documents and
    # tasks cannot be ruled out without a personal-data inventory, so they
    # count conservatively (PRD AG-TC-06 point 2).
    return acc.personal or bool(acc.principals or acc.documents or acc.tasks)


async def resolve_scope(incident_id: str, scope_inputs: dict[str, Any] | None) -> dict[str, Any]:
    await access_log.flush()
    async with async_session_factory() as session:
        incident = await _load(session, incident_id)
        raw = scope_inputs if scope_inputs is not None else incident["scope_inputs"]
        inputs = _validate_inputs(raw or {})
        computed = await _compute_scope(session, inputs)
        tenants = (
            await session.execute(
                text("SELECT id, root_tenant_id, path FROM tenants WHERE id = ANY(:ids)"),
                {"ids": list(computed)},
            )
        ).mappings().all()
        previous = {
            row["tenant_id"]: row
            for row in (
                await session.execute(
                    text("SELECT tenant_id, notified_at, sla_breached FROM incident_nodes "
                         "WHERE incident_id = :id"),
                    {"id": incident_id},
                )
            ).mappings().all()
        }
        await session.execute(text("DELETE FROM incident_nodes WHERE incident_id = :id"),
                              {"id": incident_id})
        for tenant in tenants:
            acc = computed[tenant["id"]]
            before = previous.get(tenant["id"])
            await session.execute(
                text("INSERT INTO incident_nodes (incident_id, tenant_id, root_tenant_id, "
                     "tenant_path, affected_objects, may_include_personal_info, evidence_refs, "
                     "notified_at, sla_breached) VALUES (:incident, :tenant, :root, :path, "
                     "CAST(:objects AS jsonb), :personal, CAST(:evidence AS jsonb), :notified, "
                     ":breached)"),
                {"incident": incident_id, "tenant": tenant["id"],
                 "root": tenant["root_tenant_id"], "path": list(tenant["path"]),
                 "objects": json.dumps(acc.counts()), "personal": _personal(acc),
                 "evidence": json.dumps(acc.evidence),
                 "notified": before["notified_at"] if before else None,
                 "breached": bool(before and before["sla_breached"])},
            )
        stored_inputs = {key: (value.isoformat() if isinstance(value, datetime) else value)
                         for key, value in inputs.items()}
        await session.execute(
            text("UPDATE incidents SET scope_inputs = CAST(:inputs AS jsonb), "
                 "scope_resolved_at = NOW(), updated_at = NOW() WHERE id = :id"),
            {"id": incident_id, "inputs": json.dumps(stored_inputs)},
        )
        nodes = await _nodes(session, incident_id)
        await _timeline(session, incident_id, "scope_resolved",
                        {"inputs": stored_inputs, "nodes": len(nodes),
                         "roots": len({item["root_tenant_id"] for item in nodes})})
        await session.commit()
    return {"incident_id": incident_id, "scope_inputs": stored_inputs,
            "roots": _by_root(nodes), "nodes": nodes}


async def _nodes(session: AsyncSession, incident_id: str) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text("SELECT * FROM incident_nodes WHERE incident_id = :id "
                 "ORDER BY root_tenant_id, array_length(tenant_path, 1), tenant_id"),
            {"id": incident_id},
        )
    ).mappings().all()
    return [
        {"tenant_id": row["tenant_id"], "tenant_path": list(row["tenant_path"]),
         "root_tenant_id": row["root_tenant_id"], "affected_objects": row["affected_objects"],
         "may_include_personal_info": row["may_include_personal_info"],
         "evidence_refs": row["evidence_refs"], "notified_at": row["notified_at"],
         "sla_breached": row["sla_breached"]}
        for row in rows
    ]


def _by_root(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    roots: dict[str, dict[str, Any]] = {}
    for item in nodes:
        root = roots.setdefault(item["root_tenant_id"], {
            "root_tenant_id": item["root_tenant_id"], "nodes": [],
            "affected_objects": {"documents": 0, "tasks": 0, "principals": 0, "tokens": 0},
            "may_include_personal_info": False,
        })
        root["nodes"].append(item["tenant_id"])
        for key, value in item["affected_objects"].items():
            root["affected_objects"][key] = root["affected_objects"].get(key, 0) + value
        root["may_include_personal_info"] |= item["may_include_personal_info"]
    return list(roots.values())


# ---------------------------------------------------------------- notices


async def _recipients(session: AsyncSession, tenant_id: str,
                      path: list[str], root: str) -> list[dict[str, Any]]:
    """Owners of the node and its ancestors, the root's owners, root contacts."""
    rows = (
        await session.execute(
            text("SELECT DISTINCT ON (lower(u.email)) u.id, u.email, u.workspace_id, "
                 "m.tenant_id FROM memberships m JOIN human_users u ON u.id = m.principal_id "
                 "WHERE m.role = 'owner' AND m.tenant_id = ANY(:tenants) AND u.is_active "
                 "AND (m.expires_at IS NULL OR m.expires_at > NOW()) "
                 "ORDER BY lower(u.email), array_position(:tenants, m.tenant_id)"),
            {"tenants": list(dict.fromkeys([*path, root]))},
        )
    ).mappings().all()
    recipients = [
        {"email": row["email"], "user_id": str(row["id"]),
         "workspace_id": str(row["workspace_id"]),
         "role": "owner" if row["tenant_id"] == tenant_id
         else ("root_owner" if row["tenant_id"] == root else "inherited_owner"),
         "via_tenant": row["tenant_id"]}
        for row in rows
    ]
    contacts = await session.scalar(
        text("SELECT security_contacts FROM tenants WHERE id = :id"), {"id": root}
    ) or []
    known = {item["email"].lower() for item in recipients}
    for email in contacts:
        if email.lower() not in known:
            recipients.append({"email": email, "user_id": None, "workspace_id": None,
                               "role": "security_contact", "via_tenant": root})
            known.add(email.lower())
    return recipients


def _missing(incident: dict[str, Any]) -> list[str]:
    values = {**incident, "contact": incident["contact"] or settings.security_contact}
    return [field for field in REQUIRED_FIELDS if not values.get(field)]


def _content(incident: dict[str, Any], node: dict[str, Any], subtree: list[dict[str, Any]],
             kind: str, message: str | None = None) -> dict[str, Any]:
    """The notice for one node: only that node and its subtree, never other customers."""
    totals = {"documents": 0, "tasks": 0, "principals": 0, "tokens": 0}
    for item in subtree:
        for key, value in item["affected_objects"].items():
            totals[key] = totals.get(key, 0) + value
    content = {
        "notice": kind,
        "incident_id": incident["id"],
        "title": incident["title"],
        "severity": incident["severity"],
        "nature": incident["summary"],
        "detected_at": incident["detected_at"].isoformat(),
        "confirmed_at": incident["confirmed_at"].isoformat(),
        "node": {"tenant_id": node["tenant_id"], "tenant_path": node["tenant_path"]},
        "impact": {
            "may_include_personal_info": any(item["may_include_personal_info"]
                                             for item in subtree),
            "affected_objects": totals,
            "affected_nodes": [
                {"tenant_id": item["tenant_id"], "affected_objects": item["affected_objects"]}
                for item in subtree
            ],
        },
        "measures_taken": incident["measures_taken"],
        "recommended_actions": incident["recommended_actions"],
        "contact": incident["contact"] or settings.security_contact,
    }
    if message:
        content["update"] = message
    return content


def _email_body(content: dict[str, Any]) -> str:
    impact = content["impact"]
    lines = [
        f"Security incident {content['incident_id']}: {content['title']}",
        f"Severity: {content['severity']}",
        "",
        f"What happened: {content['nature']}",
        f"Detected: {content['detected_at']}  Confirmed: {content['confirmed_at']}",
        f"Your organisation: {' / '.join(content['node']['tenant_path'])}",
        f"Affected objects: {json.dumps(impact['affected_objects'])}",
        "May include personal information: "
        + ("yes" if impact["may_include_personal_info"] else "no"),
        "",
        f"What we have done: {content['measures_taken']}",
        f"What we recommend you do: {content['recommended_actions']}",
        f"Contact: {content['contact']}",
    ]
    if content.get("update"):
        lines[1:1] = ["", f"Update: {content['update']}"]
    return "\n".join(lines)


def _subtree(node: dict[str, Any], nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in nodes if node["tenant_id"] in item["tenant_path"]]


async def send_notices(incident_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    async with async_session_factory() as session:
        incident = await _load(session, incident_id)
        if incident["confirmed_at"] is None:
            raise ApiError(409, "incident_not_confirmed",
                           "confirm the incident before notifying customers",
                           details={"confirm_with": f"POST /v1/admin/incidents/{incident_id}"
                                                    "/confirm"})
        missing = _missing(incident)
        if missing:
            raise ApiError(422, "notice_template_incomplete",
                           "the notice template is missing required fields",
                           details={"missing": missing})
        if incident["scope_resolved_at"] is None:
            raise ApiError(422, "validation_error", "resolve the incident scope first",
                           details={"resolve_with": f"POST /v1/admin/incidents/{incident_id}"
                                                    "/scope"})
        nodes = await _nodes(session, incident_id)
        pending = [item for item in nodes if item["notified_at"] is None]
        plan = []
        for node in pending:
            recipients = await _recipients(session, node["tenant_id"], node["tenant_path"],
                                           node["root_tenant_id"])
            content = _content(incident, node, _subtree(node, nodes), "initial")
            plan.append((node, recipients, content))
        if dry_run:
            preview = {
                "incident_id": incident_id,
                "dry_run": True,
                "nodes": len(plan),
                "notices": sum(len(recipients) for _, recipients, _ in plan),
                "items": [
                    {"tenant_id": node["tenant_id"], "recipients": recipients,
                     "content": content, "content_hash": _hash(content)}
                    for node, recipients, content in plan
                ],
            }
            await _timeline(session, incident_id, "notices_dry_run",
                            {"nodes": preview["nodes"], "notices": preview["notices"]})
            await session.commit()
            await alerts.raise_alert(
                "incident.dry_run", f"incident {incident_id}: notice preview (not sent)",
                severity="info", details={"incident_id": incident_id, "preview": preview},
            )
            if settings.platform_admin_email:
                async with async_session_factory() as mail:
                    await queue_email(
                        mail, to=settings.platform_admin_email,
                        subject=f"[dry run] incident {incident_id} notices",
                        body=json.dumps(preview, indent=2, default=str), ref=incident_id,
                    )
                    await mail.commit()
            return preview
        sent = await _deliver(session, incident_id, plan, "initial")
        if plan:
            await session.execute(
                text("UPDATE incident_nodes SET notified_at = NOW() WHERE incident_id = :id "
                     "AND tenant_id = ANY(:tenants)"),
                {"id": incident_id, "tenants": [node["tenant_id"] for node, _, _ in plan]},
            )
            await session.execute(
                text("UPDATE incidents SET status = 'notifying', updated_at = NOW() "
                     "WHERE id = :id AND status = 'confirmed'"),
                {"id": incident_id},
            )
        await _timeline(session, incident_id, "notices_sent",
                        {"nodes": len(plan), "notices": len(sent)})
        await session.commit()
    return {"incident_id": incident_id, "dry_run": False, "nodes": len(plan),
            "notices": len(sent), "items": sent}


async def _deliver(session: AsyncSession, incident_id: str,
                   plan: list[tuple[dict, list[dict], dict]], kind: str) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    event_targets: dict[str, set[str]] = {}
    for node, recipients, content in plan:
        digest = _hash(content)
        for recipient in recipients:
            notice_id = await session.scalar(text("SELECT agentgraph_new_id('ntc')"))
            outbox = await queue_email(
                session, to=recipient["email"],
                subject=f"[AgentGraph security] {content['title']}"
                + (" (update)" if kind == "update" else ""),
                body=_email_body(content), ref=notice_id,
            )
            channels = ["email", "banner"] + (["event"] if recipient["user_id"] else [])
            delivery = {
                "email": {"status": "queued", "outbox_id": outbox},
                "banner": {"status": "available"},
            }
            if recipient["user_id"]:
                delivery["event"] = {"status": "delivered",
                                     "workspace_id": recipient["workspace_id"]}
                event_targets.setdefault(recipient["workspace_id"], set()).add(
                    f"human:{recipient['user_id']}")
            await session.execute(
                text("INSERT INTO incident_notices (id, incident_id, kind, tenant_id, "
                     "root_tenant_id, recipient, channels, content, content_hash, "
                     "delivery_status) VALUES (:id, :incident, :kind, :tenant, :root, "
                     "CAST(:recipient AS jsonb), CAST(:channels AS jsonb), "
                     "CAST(:content AS jsonb), :hash, CAST(:delivery AS jsonb))"),
                {"id": notice_id, "incident": incident_id, "kind": kind,
                 "tenant": node["tenant_id"], "root": node["root_tenant_id"],
                 "recipient": json.dumps(recipient), "channels": json.dumps(channels),
                 "content": json.dumps(content, default=str), "hash": digest,
                 "delivery": json.dumps(delivery)},
            )
            if kind == "update":
                await session.execute(
                    text("UPDATE incident_notices SET superseded_by = :new WHERE incident_id = "
                         ":incident AND tenant_id = :tenant AND superseded_by IS NULL "
                         "AND id <> :new AND lower(recipient->>'email') = lower(:email)"),
                    {"new": notice_id, "incident": incident_id, "tenant": node["tenant_id"],
                     "email": recipient["email"]},
                )
            sent.append({"notice_id": notice_id, "tenant_id": node["tenant_id"],
                         "recipient": {key: recipient[key] for key in ("email", "role")},
                         "channels": channels, "content_hash": digest})
    for workspace_id, targets in event_targets.items():
        await append_event(
            session,
            workspace_id=uuid.UUID(workspace_id),
            agent="security",
            event_type="SECURITY_INCIDENT_NOTICE",
            payload={"incident_id": incident_id, "kind": kind,
                     "targets": sorted(targets)},
        )
    return sent


async def send_update(incident_id: str, message: str, *, measures_taken: str | None = None,
                      recommended_actions: str | None = None) -> dict[str, Any]:
    if not message or not message.strip():
        raise ApiError(422, "validation_error", "an update needs a message",
                       details={"field": "message"})
    if measures_taken or recommended_actions:
        await update_incident(incident_id, {"measures_taken": measures_taken,
                                            "recommended_actions": recommended_actions})
    async with async_session_factory() as session:
        incident = await _load(session, incident_id)
        if incident["confirmed_at"] is None:
            raise ApiError(409, "incident_not_confirmed",
                           "confirm the incident before notifying customers",
                           details={"confirm_with": f"POST /v1/admin/incidents/{incident_id}"
                                                    "/confirm"})
        missing = _missing(incident)
        if missing:
            raise ApiError(422, "notice_template_incomplete",
                           "the notice template is missing required fields",
                           details={"missing": missing})
        nodes = await _nodes(session, incident_id)
        plan = []
        for node in [item for item in nodes if item["notified_at"] is not None]:
            recipients = await _recipients(session, node["tenant_id"], node["tenant_path"],
                                           node["root_tenant_id"])
            plan.append((node, recipients,
                         _content(incident, node, _subtree(node, nodes), "update", message)))
        sent = await _deliver(session, incident_id, plan, "update")
        await _timeline(session, incident_id, "update_sent",
                        {"nodes": len(plan), "notices": len(sent)})
        await session.commit()
    return {"incident_id": incident_id, "nodes": len(plan), "notices": len(sent), "items": sent}


# ---------------------------------------------------------------- SLA


async def check_slas() -> dict[str, int]:
    """24/48/60-hour reminders for unnotified nodes; 72 hours marks a breach."""
    reminders = breaches = 0
    async with async_session_factory() as session:
        incidents = (
            await session.execute(
                text("SELECT id, confirmed_at, reminders_sent, EXTRACT(EPOCH FROM NOW() - "
                     "confirmed_at) / 3600 AS hours FROM incidents WHERE confirmed_at IS NOT NULL "
                     "AND status IN ('confirmed', 'notifying') AND scope_resolved_at IS NOT NULL")
            )
        ).mappings().all()
    for incident in incidents:
        hours = float(incident["hours"])
        async with async_session_factory() as session:
            pending = (
                await session.execute(
                    text("SELECT tenant_id FROM incident_nodes WHERE incident_id = :id "
                         "AND notified_at IS NULL AND NOT sla_breached"),
                    {"id": incident["id"]},
                )
            ).scalars().all()
            if not pending:
                continue
            done = set(incident["reminders_sent"] or [])
            for checkpoint in CHECKPOINTS_HOURS:
                if hours >= checkpoint and checkpoint not in done:
                    alert = await alerts.raise_alert(
                        "incident.sla_checkpoint",
                        f"incident {incident['id']}: {len(pending)} node(s) not notified "
                        f"{checkpoint} h after confirmation",
                        severity="critical" if checkpoint >= 48 else "warning",
                        details={"incident_id": incident["id"], "checkpoint_hours": checkpoint,
                                 "pending_tenants": pending},
                        dedupe_key=f"incident:{incident['id']}:sla:{checkpoint}",
                    )
                    done.add(checkpoint)
                    reminders += alert is not None
                    await _timeline(session, incident["id"], "sla_checkpoint",
                                    {"hours": checkpoint, "pending": len(pending)},
                                    actor="system")
            if hours >= SLA_HOURS:
                await session.execute(
                    text("UPDATE incident_nodes SET sla_breached = TRUE WHERE incident_id = :id "
                         "AND notified_at IS NULL"),
                    {"id": incident["id"]},
                )
                breaches += len(pending)
                await _timeline(session, incident["id"], "sla_breached",
                                {"tenants": pending}, actor="system")
                await alerts.raise_alert(
                    "incident.sla_breached",
                    f"incident {incident['id']}: {len(pending)} node(s) not notified within "
                    f"{SLA_HOURS} h", severity="critical",
                    details={"incident_id": incident["id"], "tenants": pending},
                    dedupe_key=f"incident:{incident['id']}:sla:breached",
                )
            await session.execute(
                text("UPDATE incidents SET reminders_sent = CAST(:sent AS jsonb) WHERE id = :id"),
                {"id": incident["id"], "sent": json.dumps(sorted(done))},
            )
            await session.commit()
    return {"reminders": reminders, "breaches": breaches}


# ----------------------------------------------------------------- record


async def export_record(incident_id: str) -> dict[str, Any]:
    async with async_session_factory() as session:
        incident = await _load(session, incident_id)
        timeline = (
            await session.execute(
                text("SELECT at, kind, actor, details FROM incident_timeline "
                     "WHERE incident_id = :id ORDER BY id"),
                {"id": incident_id},
            )
        ).mappings().all()
        nodes = await _nodes(session, incident_id)
        notices = (
            await session.execute(
                text("SELECT n.*, e.status AS email_status, e.sent_at AS email_sent_at, "
                     "e.error AS email_error FROM incident_notices n "
                     "LEFT JOIN email_outbox e ON e.id = n.delivery_status->'email'->>'outbox_id' "
                     "WHERE n.incident_id = :id ORDER BY n.sent_at, n.id"),
                {"id": incident_id},
            )
        ).mappings().all()
        related = (
            await session.execute(
                text("SELECT id, kind, severity, message, created_at FROM platform_alerts "
                     "WHERE details->>'incident_id' = :id ORDER BY created_at"),
                {"id": incident_id},
            )
        ).mappings().all()
    return {
        "format": "agentgraph-incident-record/1",
        "exported_at": _now().isoformat(),
        "incident": incident_out(incident),
        "timeline": [dict(row) for row in timeline],
        "scope": {"inputs": incident["scope_inputs"],
                  "resolved_at": incident["scope_resolved_at"],
                  "roots": _by_root(nodes), "nodes": nodes},
        "notices": [_notice_out(dict(row), include_status=True) for row in notices],
        "alerts": [dict(row) for row in related],
    }


def _notice_out(row: dict[str, Any], *, include_status: bool = False) -> dict[str, Any]:
    delivery = dict(row["delivery_status"] or {})
    if include_status and "email" in delivery:
        delivery["email"] = {**delivery["email"],
                             "status": row.get("email_status") or delivery["email"]["status"],
                             "sent_at": row.get("email_sent_at"),
                             "error": row.get("email_error")}
    return {
        "notice_id": row["id"],
        "incident_id": row["incident_id"],
        "kind": row["kind"],
        "tenant_id": row["tenant_id"],
        "recipient": {key: row["recipient"].get(key) for key in ("email", "role")},
        "channels": row["channels"],
        "content": row["content"],
        "content_hash": row["content_hash"],
        "delivery_status": delivery,
        "sent_at": row["sent_at"],
        "superseded_by": row["superseded_by"],
    }


def refuse_notice_change() -> None:
    raise Forbidden("incident notices are append-only; send an update instead")


# --------------------------------------------------------------- customer


async def notices_for(principal: Any) -> dict[str, Any]:
    """Notices addressed to the caller's node (owners and admins of the node)."""
    if principal.user_id is None:
        raise Forbidden("incident notices are read by signed-in owners")
    async with async_session_factory() as session:
        role = await session.scalar(
            text("SELECT role FROM memberships WHERE tenant_id = :t AND principal_id = :u"),
            {"t": principal.tenant_id, "u": principal.user_id},
        )
        if role not in ("owner", "admin"):
            raise Forbidden("incident notices are visible to owners and admins of the node")
        rows = (
            await session.execute(
                text("SELECT DISTINCT ON (incident_id, kind, content_hash) * "
                     "FROM incident_notices WHERE tenant_id = :t "
                     "ORDER BY incident_id, kind, content_hash, sent_at DESC"),
                {"t": principal.tenant_id},
            )
        ).mappings().all()
    items = sorted((_notice_out(dict(row)) for row in rows),
                   key=lambda item: item["sent_at"], reverse=True)
    for item in items:
        item["recipient"] = None  # other recipients are not the caller's business
    return {"items": items, "banner": next(
        (item for item in items if item["superseded_by"] is None), None)}


async def set_security_contacts(principal: Any, emails: list[str]) -> dict[str, Any]:
    if principal.user_id is None:
        raise Forbidden("security contacts are set by a signed-in owner")
    clean = []
    for email in emails:
        value = email.strip()
        if "@" not in value or len(value) > 320 or " " in value:
            raise ApiError(422, "validation_error", "not an email address",
                           details={"field": "emails", "value": value[:80]})
        if value.lower() not in {item.lower() for item in clean}:
            clean.append(value)
    if len(clean) > 10:
        raise ApiError(422, "validation_error", "at most 10 security contacts",
                       details={"field": "emails", "max": 10})
    async with async_session_factory() as session:
        root = await session.scalar(
            text("SELECT root_tenant_id FROM tenants WHERE id = :t"), {"t": principal.tenant_id}
        )
        role = await session.scalar(
            text("SELECT role FROM memberships WHERE tenant_id = :t AND principal_id = :u"),
            {"t": root, "u": principal.user_id},
        )
        if role != "owner":
            raise Forbidden("only an owner of the root organisation sets security contacts")
        await session.execute(
            text("UPDATE tenants SET security_contacts = CAST(:c AS jsonb), updated_at = NOW() "
                 "WHERE id = :t"),
            {"t": root, "c": json.dumps(clean)},
        )
        await session.commit()
    return {"tenant_id": root, "security_contacts": clean}


async def get_security_contacts(principal: Any) -> dict[str, Any]:
    async with async_session_factory() as session:
        row = (
            await session.execute(
                text("SELECT r.id, r.security_contacts FROM tenants t JOIN tenants r "
                     "ON r.id = t.root_tenant_id WHERE t.id = :t"),
                {"t": principal.tenant_id},
            )
        ).one()
    return {"tenant_id": row[0], "security_contacts": row[1]}


async def incident_check_loop() -> None:
    import asyncio
    import logging

    logger = logging.getLogger("agentgraph.incidents")
    while True:
        try:
            await check_slas()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - background resilience
            logger.exception("incident SLA check failed")
        await asyncio.sleep(settings.incident_check_interval_seconds)
