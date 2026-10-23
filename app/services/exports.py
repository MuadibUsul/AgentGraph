"""Tenant export and import: the storage format, round-trip exact (PRD AG-ST-02).

Format ``agentgraph-export/1`` is a zip archive:

    manifest.json        format, scope, tenant path, seq range, file hashes, counts,
                         one entry per document {id, alias, version, content_hash, path}
    documents/<ws>/<namespace>/<doc_key>.md
                         JSON frontmatter block (valid YAML) + the exact body
    objects.jsonl        current state of every other exported row
    versions.jsonl       document version ledger; bodies live in blobs/
    blobs/<sha256>       version bodies, content-addressed
    doc_edges.jsonl      $ref edges and typed links
    grants.jsonl         grant snapshots (no secrets)
    events.jsonl         timeline and change feed
    audit_log.jsonl      state audit and access audit
    deleted.jsonl        incremental only: rows gone since the previous export

Every line is ``{"table": <table>, "row": <row>}`` where the row is the
database's own ``to_jsonb`` of the record, so a column added to a table is
exported and imported without code changes. Each table is classified below as
exported, derived (rebuilt after import) or ephemeral; ``unclassified_tables``
lets CI fail on a table nobody classified. Embeddings and secrets are never
exported: imported workspaces get new keys, agent tokens must be rotated and
human users must reset their passwords.

An incremental export diffs every row against the previous export's row-hash
index, so a baseline plus its increments applied in order equals a full
export taken at the same moment.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import logging
import secrets
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthKind, Principal, generate_api_key, hash_api_key
from app.config import get_settings
from app.database import async_session_factory
from app.errors import ApiError, Forbidden, NotFound

logger = logging.getLogger("agentgraph.exports")
settings = get_settings()

FORMAT = "agentgraph-export/1"
SUPPORTED_FORMATS = (FORMAT,)

# table -> (archive file, columns never exported)
EXPORTED: dict[str, tuple[str, tuple[str, ...]]] = {
    "tenants": ("objects.jsonl", ()),
    "workspaces": ("objects.jsonl", ("api_key_hash", "root_key_hash")),
    "memberships": ("objects.jsonl", ()),
    "namespaces": ("objects.jsonl", ()),
    "human_users": ("objects.jsonl", ("password_hash",)),
    "agent_tokens": ("objects.jsonl", ("api_key_hash",)),
    "grant_snapshots": ("grants.jsonl", ()),
    "shared_documents": ("documents", ("embedding",)),
    "document_versions": ("versions.jsonl", ()),
    "document_refs": ("doc_edges.jsonl", ()),
    "workspace_links": ("doc_edges.jsonl", ()),
    "alias_redirects": ("objects.jsonl", ()),
    "tombstones": ("objects.jsonl", ()),
    "collab_tasks": ("objects.jsonl", ()),
    "context_slots": ("objects.jsonl", ()),
    "leases": ("objects.jsonl", ()),
    "constitution_acks": ("objects.jsonl", ()),
    "document_proposals": ("objects.jsonl", ()),
    "proposal_votes": ("objects.jsonl", ()),
    "path_policies": ("objects.jsonl", ()),
    "external_actions": ("objects.jsonl", ()),
    "external_action_attempts": ("objects.jsonl", ()),
    "taint_edges": ("objects.jsonl", ()),
    "workspace_budgets": ("objects.jsonl", ()),
    "webhook_rules": ("objects.jsonl", ("secret",)),
    "usage_ledger": ("objects.jsonl", ()),
    "event_subscriptions": ("objects.jsonl", ()),
    "onboarding_progress": ("objects.jsonl", ()),
    "agent_onboarding": ("objects.jsonl", ()),
    # The per-workspace event counter: imported logs continue their seq.
    "workspace_event_seq": ("objects.jsonl", ()),
    "timeline_streams": ("events.jsonl", ()),
    "workspace_events": ("events.jsonl", ()),
    "state_audit_diffs": ("audit_log.jsonl", ()),
    "access_audit": ("audit_log.jsonl", ()),
    "token_access_log": ("audit_log.jsonl", ()),
    "agent_directory": ("objects.jsonl", ()),
    "agent_aliases": ("objects.jsonl", ()),
    "escalations": ("objects.jsonl", ()),
    "doc_derivations": ("doc_edges.jsonl", ()),
    "recalls": ("objects.jsonl", ()),
    "recall_nodes": ("objects.jsonl", ()),
    "log_entries": ("objects.jsonl", ()),
    # Replayed after a restore or import so erased data stays erased (AG-TC-07).
    "redaction_log": ("audit_log.jsonl", ()),
    # Phase 3: named cursors survive with their events; resource definitions.
    "watermarks": ("objects.jsonl", ()),
    "resources": ("objects.jsonl", ()),
}
# Rebuilt from exported state after an import.
DERIVED = frozenset({"document_chunks", "document_index"})
# Operational state with no meaning outside this deployment, or secrets.
EPHEMERAL = frozenset({
    "budget_backoff", "budget_buckets", "budget_circuits", "budget_limits",
    "human_sessions", "idempotency_records", "rate_limit_windows", "schema_migrations",
    "webhook_deliveries", "exports", "export_archives", "imports",
    # Platform records (R1.3): backups, drills, incidents and their notices
    # belong to the operator, not to a tenant's data.
    "backups", "restore_runs", "restore_drills", "platform_alerts", "email_outbox",
    "incidents", "incident_nodes", "incident_notices", "incident_timeline",
    # Invitations are transient; rights requests hold the very personal data
    # they are about and are minimized in place, never copied out (AG-TC-07).
    "member_invitations", "rights_requests", "rights_request_events", "rights_exports",
    # The read window lives 15 minutes; daily usage counters reset (R2.2).
    "token_reads", "token_usage", "token_budgets",
    # Conflict counters, per-minute metrics and situation alerts (R2.3).
    "doc_contention", "principal_metrics", "situation_alerts",
    # Tree governance (R2.4): policies, confirmations, shares and admin access
    # sessions belong to the live tree, not to one node's data.
    "tenant_policies", "tenant_confirmations", "tenant_shares", "admin_access_sessions",
    # Queue tickets live a minute past their turn (AG-CO-07).
    "resource_queue",
    # Evaluation runs and probes are the operator's; signing keys never leave.
    "retrieval_eval_runs", "retrieval_probes", "tenant_signing_keys",
    # The deployment's own release history.
    "platform_releases",
    # Quality control (v3.32): the pipeline's operating record in this
    # deployment (gates, audits, samples, escapes, circuit, tiers, planted
    # defects). Tiers are re-granted by people after a move, never copied.
    "change_claims", "quality_gate_runs", "audits", "autonomy_tiers", "defect_escapes",
    "audit_samples", "quality_circuit", "poison_marks", "audit_view_marks", "quality_releases",
})
# Archive groups a caller may leave out with ``include``; objects always ship.
GROUPS = ("documents", "versions", "doc_edges", "grants", "events", "audit_log")
_GROUP_OF = {
    "documents": "documents", "versions.jsonl": "versions", "doc_edges.jsonl": "doc_edges",
    "grants.jsonl": "grants", "events.jsonl": "events", "audit_log.jsonl": "audit_log",
}

_running: set[asyncio.Task] = set()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def unclassified_tables(session: AsyncSession) -> list[str]:
    """Tables that are neither exported, derived nor ephemeral (CI guard)."""
    names = (
        await session.execute(
            text("SELECT table_name FROM information_schema.tables "
                 "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                 "AND table_name NOT IN (SELECT relname FROM pg_class WHERE relispartition)")
        )
    ).scalars().all()
    known = set(EXPORTED) | DERIVED | EPHEMERAL
    return sorted(set(names) - known)


async def _primary_keys(session: AsyncSession) -> dict[str, list[str]]:
    rows = (
        await session.execute(
            text(
                "SELECT tc.table_name, kcu.column_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "ON tc.constraint_name = kcu.constraint_name AND tc.table_name = kcu.table_name "
                "WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = 'public' "
                "ORDER BY tc.table_name, kcu.ordinal_position"
            )
        )
    ).all()
    keys: dict[str, list[str]] = {}
    for table, column in rows:
        keys.setdefault(table, []).append(column)
    return keys


async def _columns(session: AsyncSession, table: str) -> set[str]:
    return set(
        (
            await session.execute(
                text("SELECT column_name FROM information_schema.columns "
                     "WHERE table_schema = 'public' AND table_name = :table"),
                {"table": table},
            )
        ).scalars().all()
    )


# ------------------------------------------------------------------ requests


def _authorize(principal: Principal, scope: str) -> None:
    """Humans who own the tenant/workspace, the workspace key, or the admin console.

    Agents never export by default (it moves the whole tenant out of bounds).
    """
    if principal.auth_kind is AuthKind.HUMAN_ADMIN:
        return
    if principal.auth_kind is AuthKind.HUMAN and principal.human_role in ("OWNER", "ADMIN"):
        return
    if principal.auth_kind is AuthKind.WORKSPACE and scope != "tenant":
        return
    raise Forbidden(
        "exports need a tenant or workspace owner, the workspace key (workspace scope) "
        "or the admin console; agent tokens are not granted 'export'"
    )


async def _subtree(session: AsyncSession, tenant_id: str, include_subtree: bool) -> list[str]:
    if not include_subtree:
        return [tenant_id]
    return list(
        (
            await session.execute(
                text("SELECT id FROM tenants WHERE :tenant = ANY(path) ORDER BY depth, id"),
                {"tenant": tenant_id},
            )
        ).scalars().all()
    )


def export_out(row: dict[str, Any]) -> dict[str, Any]:
    done = row["status"] == "done"
    return {
        "export_id": row["id"],
        "status": row["status"],
        "kind": row["kind"],
        "purpose": row["purpose"],
        "scope": row["scope"],
        "scope_ref": row["scope_ref"],
        "include_history": row["include_history"],
        "include_subtree": row["include_subtree"],
        "progress": row["progress"],
        "error": row["error"],
        "download_url": f"/v1/exports/{row['id']}/download" if done else None,
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        "manifest_sha256": row["manifest_sha256"],
        "counts": row["counts"] or {},
        "seq_from": row["seq_from"],
        "seq_to": row["seq_to"],
        "as_of": row["as_of"].isoformat() if row["as_of"] else None,
        "base_export_id": row["base_export_id"],
        "prev_export_id": row["prev_export_id"],
        "size_bytes": row["size_bytes"],
        "created_at": row["created_at"].isoformat(),
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
    }


async def _load_export(session: AsyncSession, export_id: str) -> dict[str, Any] | None:
    row = (
        await session.execute(text("SELECT * FROM exports WHERE id = :id"), {"id": export_id})
    ).mappings().one_or_none()
    return dict(row) if row else None


async def request_export(
    principal: Principal,
    *,
    scope: str = "tenant",
    scope_ref: str | None = None,
    since_export_id: str | None = None,
    include_history: bool = True,
    include_subtree: bool = True,
    include: list[str] | None = None,
    purpose: str = "manual",
    requested_by: str | None = None,
    start: bool = True,
    deliver_to: list[str] | None = None,
) -> dict[str, Any]:
    if scope not in ("tenant", "workspace", "namespace"):
        raise ApiError(422, "validation_error", "scope must be tenant, workspace or namespace",
                       details={"field": "scope"})
    if include is not None and set(include) - set(GROUPS):
        raise ApiError(422, "validation_error", "unknown include groups",
                       details={"field": "include", "allowed": list(GROUPS)})
    if purpose == "manual":
        _authorize(principal, scope)
    tenant_id = principal.tenant_id
    deliver_to = list(deliver_to or [])
    if scope == "tenant":
        scope_ref = scope_ref or tenant_id
        if scope_ref != tenant_id and principal.auth_kind is not AuthKind.HUMAN_ADMIN:
            await _authorize_node(principal, scope_ref)
            deliver_to.append(tenant_id)
        tenant_id = scope_ref
    elif scope == "workspace":
        scope_ref = str(principal.workspace.id)
    else:
        if not scope_ref:
            raise ApiError(422, "validation_error", "scope_ref must name the namespace",
                           details={"field": "scope_ref"})
    async with async_session_factory() as session:
        base_id = prev_id = None
        kind = "full"
        if since_export_id:
            previous = await _load_export(session, since_export_id)
            if previous is None or previous["tenant_id"] != tenant_id:
                raise NotFound("export", f"export '{since_export_id}' not found")
            if previous["status"] != "done":
                raise ApiError(409, "export_in_progress", "the base export has not finished",
                               details={"export_id": since_export_id})
            if (previous["scope"], previous["scope_ref"]) != (scope, scope_ref):
                raise ApiError(422, "validation_error",
                               "an increment must use the scope of its base",
                               details={"field": "since_export_id"})
            kind = "incremental"
            prev_id = previous["id"]
            base_id = previous["base_export_id"] or previous["id"]
        if purpose == "manual":
            running = await session.scalar(
                text("SELECT id FROM exports WHERE tenant_id = :tenant "
                     "AND status IN ('queued', 'running') LIMIT 1"),
                {"tenant": tenant_id},
            )
            if running:
                raise ApiError(409, "export_in_progress",
                               "one export per tenant runs at a time",
                               details={"export_id": running})
            recent = int(
                await session.scalar(
                    text("SELECT count(*) FROM exports WHERE tenant_id = :tenant "
                         "AND purpose = 'manual' AND created_at > NOW() - INTERVAL '1 hour'"),
                    {"tenant": tenant_id},
                ) or 0
            )
            if recent >= settings.exports_per_hour:
                raise ApiError(429, "rate_limited", "manual export limit per tenant reached",
                               details={"dimension": "exports",
                                        "limit_per_hour": settings.exports_per_hour,
                                        "retry_after_ms": 600_000},
                               headers={"Retry-After": "600"})
        row = (
            await session.execute(
                text(
                    "INSERT INTO exports (tenant_id, workspace_id, requested_by, purpose, scope, "
                    "scope_ref, include_subtree, include_history, include_groups, kind, "
                    "base_export_id, prev_export_id, deliver_to) VALUES (:tenant, :workspace, "
                    ":by, :purpose, :scope, :ref, :subtree, :history, CAST(:groups AS jsonb), "
                    ":kind, :base, :prev, :deliver) RETURNING *"
                ),
                {"tenant": tenant_id, "workspace": principal.workspace.id,
                 "by": requested_by or principal.agent_signature, "purpose": purpose,
                 "scope": scope, "ref": scope_ref, "subtree": include_subtree,
                 "history": include_history,
                 "groups": json.dumps(include) if include is not None else None,
                 "kind": kind, "base": base_id, "prev": prev_id, "deliver": deliver_to},
            )
        ).mappings().one()
        await session.commit()
    if start:
        start_export(row["id"])
    return export_out(dict(row))


async def start_final_export(tenant_id: str) -> str | None:
    """Full tenant export taken when deletion is confirmed (AG-ID-07 / AG-ST-02)."""
    from app.auth import principal_for_admin
    from app.models import Workspace

    async with async_session_factory() as session:
        existing = await session.scalar(
            text("SELECT final_export_id FROM tenants WHERE id = :id"), {"id": tenant_id}
        )
        if existing:
            return existing
        workspace = await session.scalar(
            text("SELECT id FROM workspaces WHERE tenant_id = :tenant ORDER BY created_at "
                 "LIMIT 1"),
            {"tenant": tenant_id},
        )
        if workspace is None:
            return None
        principal = principal_for_admin(await session.get(Workspace, workspace))
    export = await request_export(
        principal, scope="tenant", scope_ref=tenant_id, purpose="final",
        requested_by="platform",
    )
    async with async_session_factory() as session:
        await session.execute(
            text("UPDATE tenants SET final_export_id = :export WHERE id = :id"),
            {"export": export["export_id"], "id": tenant_id},
        )
        await session.commit()
    return export["export_id"]


def start_export(export_id: str) -> None:
    task = asyncio.create_task(run_export(export_id))
    _running.add(task)
    task.add_done_callback(_running.discard)


async def _authorize_node(principal: Principal, node_id: str) -> None:
    """Another node: an owner/admin of it or an ancestor, or a person's own
    personal space unless an ancestor's policy forbids it (AG-ID-13)."""
    from app.services import tree

    if principal.auth_kind is not AuthKind.HUMAN:
        raise Forbidden("you may export your own tenant only")
    async with async_session_factory() as session:
        target = await tree.node(session, None, node_id)
        role = await tree.role_on(session, principal, target["id"])
        if role in ("owner", "admin"):
            return
        if target["tenant_type"] == "personal" and role == "member" and (
                await tree.effective(session, target["id"]))["personal_space_export"]["value"]:
            return
    raise Forbidden("you may export your own tenant only")


def _visible(principal: Principal, row: dict[str, Any]) -> bool:
    if principal.auth_kind is AuthKind.HUMAN_ADMIN:
        return True
    if principal.tenant_id in (row.get("deliver_to") or []):
        return principal.auth_kind is AuthKind.HUMAN and principal.human_role in (
            "OWNER", "ADMIN")
    if row["tenant_id"] != principal.tenant_id:
        return False
    if principal.auth_kind is AuthKind.WORKSPACE:
        return row["workspace_id"] == principal.workspace.id
    return principal.auth_kind is AuthKind.HUMAN and principal.human_role in ("OWNER", "ADMIN")


async def get_export_any(export_id: str) -> dict[str, Any]:
    async with async_session_factory() as session:
        row = await _load_export(session, export_id)
    if row is None:
        raise NotFound("export", f"export '{export_id}' not found")
    return export_out(row)


async def get_export(principal: Principal, export_id: str) -> dict[str, Any]:
    async with async_session_factory() as session:
        row = await _load_export(session, export_id)
    if row is None or not _visible(principal, row):
        raise NotFound("export", f"export '{export_id}' not found")
    return export_out(row)


async def list_exports(
    principal: Principal, *, scope: str | None, cursor: str | None, limit: int = 50
) -> dict[str, Any]:
    _authorize(principal, scope or "workspace")
    params: dict[str, Any] = {"tenant": principal.tenant_id, "limit": limit + 1}
    where = "tenant_id = :tenant"
    if scope:
        where += " AND scope = :scope"
        params["scope"] = scope
    if cursor:
        where += " AND id < :cursor"
        params["cursor"] = cursor
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                text(f"SELECT * FROM exports WHERE {where} ORDER BY id DESC LIMIT :limit"),
                params,
            )
        ).mappings().all()
    items = [dict(row) for row in rows if _visible(principal, dict(row))][:limit]
    return {
        "items": [export_out(row) for row in items],
        "next_cursor": items[-1]["id"] if len(rows) > limit and items else None,
    }


async def download(
    principal: Principal | None, export_id: str
) -> tuple[bytes, dict[str, Any]]:
    """``principal=None`` is the admin console."""
    async with async_session_factory() as session:
        row = await _load_export(session, export_id)
        if row is None or (principal is not None and not _visible(principal, row)):
            raise NotFound("export", f"export '{export_id}' not found")
        if row["status"] != "done":
            raise ApiError(409, "export_in_progress", "the export has not finished",
                           details={"export_id": export_id, "status": row["status"]})
        if row["expires_at"] and row["expires_at"] < _now():
            raise ApiError(410, "gone", "the export archive expired",
                           details={"export_id": export_id})
        data = await session.scalar(
            text("SELECT data FROM export_archives WHERE export_id = :id"), {"id": export_id}
        )
    if data is None:
        raise ApiError(410, "gone", "the export archive is no longer stored",
                       details={"export_id": export_id})
    return bytes(data), row


# ------------------------------------------------------------------ export job


async def _scope_filters(
    session: AsyncSession, row: dict[str, Any]
) -> tuple[list[str], list[str], str | None]:
    """(tenant ids, workspace ids, namespace) covered by the export."""
    if row["scope"] == "tenant":
        tenants = await _subtree(session, row["tenant_id"], row["include_subtree"])
        workspaces = (
            await session.execute(
                text("SELECT CAST(id AS text) FROM workspaces WHERE tenant_id = ANY(:tenants) "
                     "ORDER BY id"),
                {"tenants": tenants},
            )
        ).scalars().all()
        return tenants, list(workspaces), None
    workspace_id = str(row["workspace_id"])
    tenant = await session.scalar(
        text("SELECT tenant_id FROM workspaces WHERE id = CAST(:id AS uuid)"), {"id": workspace_id}
    )
    namespace = row["scope_ref"] if row["scope"] == "namespace" else None
    return [tenant], [workspace_id], namespace


async def _table_rows(
    session: AsyncSession,
    table: str,
    excluded: tuple[str, ...],
    pk: list[str],
    tenants: list[str],
    workspaces: list[str],
    namespace: str | None,
    columns: set[str],
    document_ids: list[str] | None,
) -> list[dict[str, Any]]:
    drop = " - ".join(["to_jsonb(t)", *[f"'{column}'" for column in excluded]])
    order = ", ".join(f"t.{column}" for column in pk)
    params: dict[str, Any] = {}
    if table == "tenants":
        where = "t.id = ANY(:tenants)"
        params["tenants"] = tenants
    elif table == "memberships" or ("workspace_id" not in columns and "tenant_id" in columns):
        where = "t.tenant_id = ANY(:tenants)"
        params["tenants"] = tenants
    elif table == "workspaces":
        where = "CAST(t.id AS text) = ANY(:workspaces)"
        params["workspaces"] = workspaces
    else:
        where = "CAST(t.workspace_id AS text) = ANY(:workspaces)"
        params["workspaces"] = workspaces
        if namespace is not None:
            if "namespace" in columns:
                where += " AND t.namespace = :namespace"
                params["namespace"] = namespace
            elif table == "document_refs":
                where += " AND CAST(t.source_document_id AS text) = ANY(:docs)"
                params["docs"] = document_ids or []
            elif table not in ("workspace_budgets",):
                return []
    rows = (
        await session.execute(
            text(f"SELECT {drop} FROM {table} t WHERE {where} ORDER BY {order}"), params
        )
    ).scalars().all()
    return list(rows)


def _pk_of(row: dict[str, Any], pk: list[str]) -> str:
    return _dumps([row.get(column) for column in pk])


def _doc_path(row: dict[str, Any]) -> str:
    return f"documents/{row['workspace_id']}/{row['namespace']}/{row['doc_key']}.md"


def _render_document(row: dict[str, Any]) -> bytes:
    header = {
        "id": row.get("canonical_id"),
        "alias": row["doc_key"],
        "namespace": row["namespace"],
        "version": row["version"],
        "content_hash": row["content_hash"],
        "trust": row.get("trust_level"),
        "verification_status": row.get("verification_status"),
        "tainted": row.get("tainted"),
        "lifecycle": "archived" if row.get("archived_at") else "active",
        "frontmatter": row.get("frontmatter") or {},
    }
    return (
        "---\n" + json.dumps(header, ensure_ascii=False, indent=2) + "\n---\n"
        + row["content"]
    ).encode("utf-8")


def _parse_document(data: bytes) -> tuple[dict[str, Any], str]:
    text_value = data.decode("utf-8")
    if not text_value.startswith("---\n"):
        raise ValueError("document file has no frontmatter")
    end = text_value.index("\n---\n", 4)
    return json.loads(text_value[4:end]), text_value[end + 5:]


async def run_export(export_id: str) -> None:
    try:
        await _run_export(export_id)
    except Exception as exc:  # noqa: BLE001 - recorded on the job
        logger.exception("export %s failed", export_id)
        async with async_session_factory() as session:
            await session.execute(
                text("UPDATE exports SET status = 'failed', error = :error, finished_at = NOW() "
                     "WHERE id = :id"),
                {"id": export_id, "error": str(exc)[:2_000]},
            )
            await session.commit()


async def _run_export(export_id: str) -> None:
    async with async_session_factory() as session:
        # One snapshot for the whole archive.
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        row = await _load_export(session, export_id)
        if row is None:
            return
        await session.execute(
            text("UPDATE exports SET status = 'running', started_at = NOW() WHERE id = :id"),
            {"id": export_id},
        )
        as_of = await session.scalar(text("SELECT NOW()"))
        tenants, workspaces, namespace = await _scope_filters(session, row)
        pks = await _primary_keys(session)
        groups = set(row["include_groups"]) if row["include_groups"] is not None else set(GROUPS)
        previous_hashes: dict[str, dict[str, str]] = {}
        if row["prev_export_id"]:
            blob = await session.scalar(
                text("SELECT row_hashes FROM exports WHERE id = :id"),
                {"id": row["prev_export_id"]},
            )
            previous_hashes = json.loads(gzip.decompress(blob)) if blob else {}

        files: dict[str, bytes] = {}
        jsonl: dict[str, list[str]] = {}
        hashes: dict[str, dict[str, str]] = {}
        counts: dict[str, int] = {}
        deleted: list[str] = []
        documents_manifest: list[dict[str, Any]] = []
        document_ids: list[str] | None = None
        current_versions: set[tuple[Any, Any]] = set()
        for index, (table, (target, excluded)) in enumerate(EXPORTED.items()):
            group = _GROUP_OF.get(target)
            if group is not None and group not in groups:
                continue
            columns = await _columns(session, table)
            rows = await _table_rows(
                session, table, excluded, pks[table], tenants, workspaces, namespace,
                columns, document_ids,
            )
            if table == "shared_documents":
                document_ids = [item["id"] for item in rows]
            if table == "shared_documents":
                current_versions = {(item["id"], item["version"]) for item in rows}
            if table == "document_versions" and not row["include_history"]:
                rows = [item for item in rows
                        if (item["document_id"], item["version"]) in current_versions]
            table_hashes: dict[str, str] = {}
            previous = previous_hashes.get(table, {})
            changed = 0
            for item in rows:
                key = _pk_of(item, pks[table])
                digest = _sha(_dumps(item).encode("utf-8"))
                table_hashes[key] = digest
                if previous.get(key) == digest:
                    continue
                changed += 1
                if table == "shared_documents":
                    path = _doc_path(item)
                    body = _render_document(item)
                    files[path] = body
                    meta = {key_: value for key_, value in item.items() if key_ != "content"}
                    jsonl.setdefault("objects.jsonl", []).append(
                        _dumps({"table": table, "row": meta, "file": path})
                    )
                    documents_manifest.append({
                        "id": item.get("canonical_id"), "alias": item["doc_key"],
                        "namespace": item["namespace"], "version": item["version"],
                        "content_hash": item["content_hash"], "path": path,
                    })
                elif table == "document_versions":
                    content = (item.get("content") or "").encode("utf-8")
                    digest_blob = _sha(content)
                    files[f"blobs/{digest_blob}"] = content
                    meta = {key_: value for key_, value in item.items() if key_ != "content"}
                    meta_line = {"table": table, "row": meta, "blob": digest_blob}
                    jsonl.setdefault(target, []).append(_dumps(meta_line))
                else:
                    jsonl.setdefault(target, []).append(_dumps({"table": table, "row": item}))
            for key in set(previous) - set(table_hashes):
                deleted.append(_dumps({"table": table, "pk": json.loads(key),
                                       "columns": pks[table]}))
            hashes[table] = table_hashes
            counts[table] = changed if row["prev_export_id"] else len(rows)
            await session.execute(
                text("UPDATE exports SET progress = :p WHERE id = :id"),
                {"id": export_id, "p": round((index + 1) / len(EXPORTED) * 0.9, 3)},
            )
        for name, lines in jsonl.items():
            files[name] = ("\n".join(lines) + "\n").encode("utf-8")
        if row["prev_export_id"]:
            files["deleted.jsonl"] = ("\n".join(deleted) + ("\n" if deleted else "")).encode()
        seq_from_to = (
            await session.execute(
                text("SELECT MIN(id), MAX(id) FROM workspace_events "
                     "WHERE CAST(workspace_id AS text) = ANY(:workspaces)"),
                {"workspaces": workspaces},
            )
        ).one()
        seq_from = seq_from_to[0]
        if row["prev_export_id"]:
            seq_from = await session.scalar(
                text("SELECT seq_to FROM exports WHERE id = :id"), {"id": row["prev_export_id"]}
            )
            seq_from = (seq_from or 0) + 1
        seq_to = seq_from_to[1]
        tenant_rows = (
            await session.execute(
                text("SELECT id, alias, parent_tenant_id, path, depth FROM tenants "
                     "WHERE id = ANY(:tenants) ORDER BY depth, id"),
                {"tenants": tenants},
            )
        ).mappings().all()
        manifest = {
            "format": FORMAT,
            "export_id": export_id,
            "kind": row["kind"],
            "base_export_id": row["base_export_id"],
            "prev_export_id": row["prev_export_id"],
            "tenant_id": row["tenant_id"],
            "tenant_path": next(
                (list(item["path"]) for item in tenant_rows if item["id"] == row["tenant_id"]),
                [row["tenant_id"]],
            ),
            "subtree": [
                {"id": item["id"], "alias": item["alias"], "parent": item["parent_tenant_id"],
                 "depth": item["depth"]}
                for item in tenant_rows
            ],
            "scope": row["scope"],
            "scope_ref": row["scope_ref"],
            "include_history": row["include_history"],
            "include": sorted(groups),
            "workspaces": workspaces,
            "as_of": as_of.isoformat(),
            "seq_from": seq_from,
            "seq_to": seq_to,
            "counts": counts,
            "deleted": len(deleted),
            "documents": documents_manifest,
            "files": {name: {"sha256": _sha(data), "bytes": len(data)}
                      for name, data in sorted(files.items())},
        }
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", manifest_bytes)
            for name, data in sorted(files.items()):
                archive.writestr(name, data)
        data = buffer.getvalue()
        await session.execute(
            text("INSERT INTO export_archives (export_id, data) VALUES (:id, :data)"),
            {"id": export_id, "data": data},
        )
        await session.execute(
            text(
                "UPDATE exports SET status = 'done', progress = 1, finished_at = NOW(), "
                "as_of = :as_of, seq_from = :seq_from, seq_to = :seq_to, "
                "manifest_sha256 = :manifest, counts = CAST(:counts AS jsonb), "
                "row_hashes = :hashes, size_bytes = :size, expires_at = :expires WHERE id = :id"
            ),
            {"id": export_id, "as_of": as_of, "seq_from": seq_from, "seq_to": seq_to,
             "manifest": _sha(manifest_bytes),
             "counts": json.dumps({**counts, "documents": len(documents_manifest),
                                   "deleted": len(deleted)}),
             "hashes": gzip.compress(_dumps(hashes).encode("utf-8")),
             "size": len(data),
             "expires": _now() + timedelta(days=settings.export_retention_days)},
        )
        await session.commit()


async def wait_for(export_id: str, timeout: float = 120.0) -> None:
    """Await a job started in this process (used by tests and the final export)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        async with async_session_factory() as session:
            status = await session.scalar(
                text("SELECT status FROM exports WHERE id = :id"), {"id": export_id}
            )
        if status in ("done", "failed"):
            return
        await asyncio.sleep(0.2)


# ------------------------------------------------------------------ import


def _read_archive(data: bytes) -> tuple[dict[str, Any], zipfile.ZipFile]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        manifest = json.loads(archive.read("manifest.json"))
    except Exception as exc:  # noqa: BLE001 - anything unreadable is the client's
        raise ApiError(422, "export_format_unsupported", "not an Veynod export archive",
                       details={"supported": list(SUPPORTED_FORMATS)}) from exc
    if manifest.get("format") not in SUPPORTED_FORMATS:
        raise ApiError(422, "export_format_unsupported", "unsupported export format",
                       details={"format": manifest.get("format"),
                                "supported": list(SUPPORTED_FORMATS)})
    return manifest, archive


def _verify(manifest: dict[str, Any], archive: zipfile.ZipFile) -> None:
    mismatches: list[dict[str, Any]] = []
    names = set(archive.namelist()) - {"manifest.json"}
    for name, meta in manifest.get("files", {}).items():
        if name not in names:
            mismatches.append({"file": name, "problem": "missing"})
            continue
        if _sha(archive.read(name)) != meta["sha256"]:
            mismatches.append({"file": name, "problem": "sha256"})
    for name in names - set(manifest.get("files", {})):
        mismatches.append({"file": name, "problem": "not_in_manifest"})
    for entry in manifest.get("documents", []):
        if entry["path"] not in names:
            continue
        _, body = _parse_document(archive.read(entry["path"]))
        if _sha(body.encode("utf-8")) != entry["content_hash"]:
            mismatches.append({"file": entry["path"], "problem": "content_hash",
                               "id": entry["id"]})
    if mismatches:
        raise ApiError(422, "checksum_mismatch", "the archive does not match its manifest",
                       details={"mismatches": mismatches[:100], "total": len(mismatches)})


def _rows_from(manifest: dict[str, Any], archive: zipfile.ZipFile) -> dict[str, list[dict]]:
    rows: dict[str, list[dict[str, Any]]] = {}
    names = set(archive.namelist())
    for name in ("objects.jsonl", "versions.jsonl", "doc_edges.jsonl", "grants.jsonl",
                 "events.jsonl", "audit_log.jsonl"):
        if name not in names:
            continue
        for line in archive.read(name).decode("utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            row = entry["row"]
            if "file" in entry:
                _, body = _parse_document(archive.read(entry["file"]))
                row["content"] = body
            if "blob" in entry:
                row["content"] = archive.read(f"blobs/{entry['blob']}").decode("utf-8")
            rows.setdefault(entry["table"], []).append(row)
    return rows


def _rebase_tenants(rows: list[dict[str, Any]], root_id: str) -> None:
    """The exported node becomes a root tenant; recompute the tree fields."""
    by_id = {row["id"]: row for row in rows}

    def chain(tenant_id: str) -> list[str]:
        path = []
        current = by_id.get(tenant_id)
        while current is not None:
            path.insert(0, current["id"])
            if current["id"] == root_id:
                break
            current = by_id.get(current.get("parent_tenant_id"))
        return path

    for row in rows:
        path = chain(row["id"])
        row["path"] = path
        row["depth"] = len(path)  # the root is depth 1
        row["root_tenant_id"] = root_id
        if row["id"] == root_id:
            row["parent_tenant_id"] = None
        if row.get("billing_owner_tenant_id") not in by_id:
            row["billing_owner_tenant_id"] = root_id


async def _disable_triggers(session: AsyncSession, tables: list[str]) -> list[str]:
    """Keep exported stamps and ids verbatim: imported rows are not new writes."""
    superuser = await session.scalar(
        text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
    )
    if superuser:
        await session.execute(text("SET LOCAL session_replication_role = replica"))
        return []
    for table in tables:
        await session.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER USER"))
    return tables


async def import_archive(
    principal: Principal | None, data: bytes, *, target: str, verify: bool = True,
    into_tenant_id: str | None = None, session_factory: Any = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Import an archive; ``principal=None`` is the admin console.

    ``session_factory`` lets a restore import into another database (staging).
    An increment imported with ``into_tenant_id`` is moved into that tenant,
    like the ``new_workspace`` import it continues.
    """
    factory = session_factory or async_session_factory
    if principal is not None and not (
        principal.auth_kind is AuthKind.HUMAN_ADMIN
        or (principal.auth_kind is AuthKind.HUMAN and principal.human_role == "OWNER")
    ):
        raise Forbidden("imports are run by a human owner or the admin console")
    if target not in ("new_workspace", "new_tenant", "increment"):
        raise ApiError(422, "validation_error", "target must be new_workspace, new_tenant "
                       "or increment", details={"field": "target"})
    if len(data) > settings.max_import_bytes:
        raise ApiError(413, "payload_too_large", "the archive is larger than allowed",
                       details={"limit_bytes": settings.max_import_bytes})
    manifest, archive = _read_archive(data)
    if target == "increment" and manifest["kind"] != "incremental":
        raise ApiError(422, "validation_error", "target=increment needs an incremental export",
                       details={"field": "target"})
    if target != "increment" and manifest["kind"] != "full":
        raise ApiError(422, "validation_error", "a new workspace or tenant starts from a full "
                       "export; apply increments with target=increment",
                       details={"field": "target"})
    if verify:
        _verify(manifest, archive)
    rows = _rows_from(manifest, archive)
    manifest_sha = _sha(archive.read("manifest.json"))
    actor = actor or (principal.agent_signature if principal is not None else "admin")
    tenant_id = into_tenant_id or (principal.tenant_id if principal is not None else None)
    if target == "new_workspace" and tenant_id is None:
        raise ApiError(422, "validation_error", "name the tenant that receives the workspace",
                       details={"field": "into_tenant"})
    record_tenant = tenant_id or manifest["tenant_id"]
    import_id = None
    async with factory() as session:
        import_id = await session.scalar(
            text("INSERT INTO imports (tenant_id, requested_by, target, verify, "
                 "source_export_id, source_kind, manifest_sha256) VALUES (:tenant, :by, "
                 ":target, :verify, :source, :kind, :sha) RETURNING id"),
            {"tenant": record_tenant, "by": actor, "target": target,
             "verify": verify, "source": manifest["export_id"], "kind": manifest["kind"],
             "sha": manifest_sha},
        )
        await session.commit()
    try:
        async with factory() as session:
            result = await _apply(
                session, actor, manifest, archive, rows, target, tenant_id,
                rewrite_tenant=into_tenant_id if target == "increment" else None,
            )
            await session.commit()
    except ApiError as exc:
        await _finish_import(import_id, "failed", error=exc.body()["error"], factory=factory)
        raise
    except Exception as exc:
        await _finish_import(import_id, "failed", error={"message": str(exc)[:2_000]},
                             factory=factory)
        raise
    await _finish_import(import_id, "done", counts=result["counts"],
                         workspace_ids=[item["id"] for item in result["workspaces"]],
                         factory=factory)
    return {"import_id": import_id, "status": "done", **result}


async def _finish_import(
    import_id: str, status: str, *, error: dict | None = None, counts: dict | None = None,
    workspace_ids: list[str] | None = None, factory: Any = None,
) -> None:
    async with (factory or async_session_factory)() as session:
        await session.execute(
            text("UPDATE imports SET status = :status, error = CAST(:error AS jsonb), "
                 "counts = CAST(:counts AS jsonb), workspace_ids = CAST(:ws AS jsonb), "
                 "finished_at = NOW() WHERE id = :id"),
            {"id": import_id, "status": status, "error": json.dumps(error) if error else None,
             "counts": json.dumps(counts or {}), "ws": json.dumps(workspace_ids or [])},
        )
        await session.commit()


async def _apply(
    session: AsyncSession,
    actor: str,
    manifest: dict[str, Any],
    archive: zipfile.ZipFile,
    rows: dict[str, list[dict[str, Any]]],
    target: str,
    into_tenant_id: str | None,
    *,
    rewrite_tenant: str | None = None,
) -> dict[str, Any]:
    pks = await _primary_keys(session)
    workspaces = rows.get("workspaces", [])
    keys: list[dict[str, Any]] = []
    if target == "increment":
        applied = await session.scalar(
            text("SELECT source_export_id FROM imports WHERE status = 'done' "
                 "AND source_export_id = :prev"),
            {"prev": manifest["prev_export_id"]},
        )
        if applied is None:
            raise ApiError(409, "increment_out_of_order",
                           "apply the previous export of this chain first",
                           details={"prev_export_id": manifest["prev_export_id"]})
        if rewrite_tenant:
            rows.pop("tenants", None)
            rows.pop("memberships", None)
            for table_rows in rows.values():
                for row in table_rows:
                    if "tenant_id" in row:
                        row["tenant_id"] = rewrite_tenant
                    if "root_tenant_id" in row:
                        row["root_tenant_id"] = rewrite_tenant
    else:
        if not workspaces:
            raise ApiError(422, "validation_error", "the archive holds no workspace",
                           details={"field": "archive"})
        ids = [row["id"] for row in workspaces]
        taken = (
            await session.execute(
                text("SELECT CAST(id AS text) FROM workspaces WHERE CAST(id AS text) = ANY(:ids)"),
                {"ids": ids},
            )
        ).scalars().all()
        if target == "new_tenant":
            tenant_ids = [row["id"] for row in rows.get("tenants", [])]
            taken += list(
                (
                    await session.execute(
                        text("SELECT id FROM tenants WHERE id = ANY(:ids)"), {"ids": tenant_ids}
                    )
                ).scalars().all()
            )
        if taken:
            raise ApiError(409, "id_collision", "the target already holds these objects; "
                           "imports never overwrite", details={"ids": sorted(taken)[:20]})
        for row in workspaces:
            key = generate_api_key()
            row["api_key_hash"] = hash_api_key(key)
            keys.append({"id": row["id"], "canonical_id": row.get("canonical_id"),
                         "name": row.get("name"), "api_key": key})
        if target == "new_tenant":
            _rebase_tenants(rows.get("tenants", []), manifest["tenant_id"])
        else:
            rows.pop("tenants", None)
            rows.pop("memberships", None)
            for table_rows in rows.values():
                for row in table_rows:
                    if "tenant_id" in row:
                        row["tenant_id"] = into_tenant_id
                    if "root_tenant_id" in row:
                        row["root_tenant_id"] = into_tenant_id
    for row in rows.get("agent_tokens", []):
        row["api_key_hash"] = hash_api_key(f"imported-{secrets.token_urlsafe(32)}")
    for row in rows.get("human_users", []):
        row["password_hash"] = "!imported-reset-required"
    for row in rows.get("webhook_rules", []):
        row["secret"] = secrets.token_urlsafe(32)

    ordered = [table for table in EXPORTED if rows.get(table)]
    disabled = await _disable_triggers(session, ordered)
    counts: dict[str, int] = {}
    try:
        for table in ordered:
            table_rows = rows[table]
            pk = pks[table]
            conflict = ""
            if target == "increment":
                columns = sorted(await _columns(session, table))
                updates = ", ".join(f"{column} = EXCLUDED.{column}" for column in columns
                                    if column not in pk)
                conflict = f" ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {updates}"
            for start in range(0, len(table_rows), 500):
                chunk = table_rows[start:start + 500]
                await session.execute(
                    text(f"INSERT INTO {table} SELECT * FROM "
                         f"jsonb_populate_recordset(NULL::{table}, CAST(:rows AS jsonb))"
                         f"{conflict}"),
                    {"rows": json.dumps(chunk, ensure_ascii=False)},
                )
            counts[table] = len(table_rows)
        if target == "increment" and "deleted.jsonl" in archive.namelist():
            for line in archive.read("deleted.jsonl").decode("utf-8").splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                where = " AND ".join(
                    f"CAST({column} AS text) = :k{index}"
                    for index, column in enumerate(entry["columns"])
                )
                await session.execute(
                    text(f"DELETE FROM {entry['table']} WHERE {where}"),
                    {f"k{index}": str(value) for index, value in enumerate(entry["pk"])},
                )
                counts["deleted"] = counts.get("deleted", 0) + 1
    except Exception as exc:
        if _is_unique_violation(exc):
            raise ApiError(409, "id_collision", "the target already holds an imported object",
                           details={"reason": str(getattr(exc, "orig", exc))[:500]}) from exc
        raise
    finally:
        for table in disabled:
            await session.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER USER"))
    documents = rows.get("shared_documents", [])
    if documents:
        # Derived state is rebuilt: the index worker embeds every imported version.
        await session.execute(
            text(
                "INSERT INTO document_index (document_id, workspace_id, tenant_id, state, "
                "pending_version, next_attempt_at, enqueued_at) "
                "SELECT id, workspace_id, tenant_id, 'pending', version, NOW(), NOW() "
                "FROM shared_documents WHERE CAST(id AS text) = ANY(:ids) "
                "ON CONFLICT (document_id) DO UPDATE SET state = 'pending', "
                "pending_version = EXCLUDED.pending_version, next_attempt_at = NOW()"
            ),
            {"ids": [row["id"] for row in documents]},
        )
    workspace_ids = [row["id"] for row in workspaces]
    for workspace_id in workspace_ids[:1] or []:
        await session.execute(
            text(
                # Triggers are off for this transaction: fill the tenant columns.
                "INSERT INTO state_audit_diffs (workspace_id, tenant_id, root_tenant_id, "
                "session_batch_id, agent_signature, target_table, target_record_id, operation, "
                "before_diff, after_diff) SELECT w.id, w.tenant_id, t.root_tenant_id, :batch, "
                ":by, 'imports', :rid, 'INSERT', '{}'::jsonb, CAST(:after AS jsonb) "
                "FROM workspaces w JOIN tenants t ON t.id = w.tenant_id "
                "WHERE w.id = CAST(:ws AS uuid)"
            ),
            {"ws": workspace_id, "batch": uuid.uuid4(), "by": actor,
             "rid": uuid.uuid4(),
             "after": json.dumps({"source_export_id": manifest["export_id"],
                                  "target": target, "counts": counts})},
        )
    return {
        "target": target,
        "source_export_id": manifest["export_id"],
        "counts": counts,
        "workspaces": keys,
        "warnings": [
            {"code": "secrets_not_imported",
             "message": "agent tokens must be rotated, human users must reset passwords, "
                        "webhook secrets were regenerated"},
        ] if target != "increment" else [],
    }


def _is_unique_violation(exc: Exception) -> bool:
    return "UniqueViolation" in type(getattr(exc, "orig", exc)).__name__ or (
        "duplicate key" in str(exc)
    )
