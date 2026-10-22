"""Backups, restores and restore drills (PRD AG-ST-10).

A backup run writes one archive per root tenant to ``BACKUP_DIR`` (another
disk or an object-storage mount), in the ``agentgraph-export/1`` format of
AG-ST-02, plus a ``manifest.json``:

    <BACKUP_DIR>/<backup_id>/manifest.json
    <BACKUP_DIR>/<backup_id>/<root_tenant_id>.zip

Runs form chains: a full backup, then incrementals that diff against the
previous run's per-tenant export. With incrementals every 15 minutes the
logical recovery point is at most 15 minutes old. Physical PITR (WAL
archiving) is configured on PostgreSQL itself; ``list_backups`` reports the
archiver's state when it is on.

A restore never overwrites production by default: ``staging`` restores into
``STAGING_DATABASE_URL`` (wiped first), ``new_workspace`` imports into an
existing tenant (ids must be free), and ``in_place`` replaces a tenant's
workspaces only with an explicit ``confirm`` and after a pre-restore export.
A restore drill restores the latest chain into staging and compares document
counts and a sha256 sample with the backup's own manifests.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import shlex
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.database import apply_schema, async_session_factory
from app.errors import ApiError, NotFound
from app.services import alerts
from app.services import exports as export_service

logger = logging.getLogger("agentgraph.backups")
settings = get_settings()

FORMAT = "agentgraph-backup/1"
TARGETS = ("staging", "new_workspace", "in_place")
_running: set[asyncio.Task] = set()
_LOCK_SQL = "SELECT pg_try_advisory_lock(hashtext('agentgraph_backup')::bigint)"
_UNLOCK_SQL = "SELECT pg_advisory_unlock(hashtext('agentgraph_backup')::bigint)"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def backup_dir() -> Path:
    if not settings.backup_dir:
        raise ApiError(409, "backups_not_configured",
                       "set BACKUP_DIR (storage separate from the database) to enable backups",
                       details={"setting": "BACKUP_DIR"})
    path = Path(settings.backup_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def backup_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "backup_id": row["id"],
        "type": row["type"],
        "trigger": row["trigger"],
        "status": row["status"],
        "base_backup_id": row["base_backup_id"],
        "prev_backup_id": row["prev_backup_id"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "as_of": row["as_of"],
        "bytes": row["bytes"],
        "sha256": row["sha256"],
        "verified_at": row["verified_at"],
        "documents": row["documents"],
        "tenants": len(row["entries"] or []),
        "error": row["error"],
    }


async def _load(session: AsyncSession, backup_id: str) -> dict[str, Any] | None:
    row = (
        await session.execute(text("SELECT * FROM backups WHERE id = :id"), {"id": backup_id})
    ).mappings().one_or_none()
    return dict(row) if row else None


async def _latest_done(session: AsyncSession) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text("SELECT * FROM backups WHERE status = 'done' ORDER BY finished_at DESC, id DESC "
                 "LIMIT 1")
        )
    ).mappings().one_or_none()
    return dict(row) if row else None


# ------------------------------------------------------------------ backup


async def run_backup(
    *, type: str | None = None, trigger: str = "manual", tenant_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Take a backup now; ``type=None`` picks full or incremental by schedule.

    ``tenant_ids`` limits a manual run to some root tenants (scheduled runs
    take every tenant).
    """
    if type not in (None, "full", "incremental"):
        raise ApiError(422, "validation_error", "type must be full or incremental",
                       details={"field": "type"})
    root = backup_dir()
    async with async_session_factory() as lock:
        if not await lock.scalar(text(_LOCK_SQL)):
            raise ApiError(409, "backup_in_progress", "another backup is running",
                           details={"retry_after_ms": 60_000})
        try:
            return await _run_backup(root, type, trigger, tenant_ids)
        finally:
            await lock.execute(text(_UNLOCK_SQL))
            await lock.commit()


async def _run_backup(
    root: Path, kind: str | None, trigger: str, tenant_ids: list[str] | None
) -> dict[str, Any]:
    async with async_session_factory() as session:
        previous = await _latest_done(session)
        base = None
        if previous is not None:
            base = previous if previous["type"] == "full" else await _load(
                session, previous["base_backup_id"])
        if kind is None:
            due = base is None or base["finished_at"] < _now() - timedelta(
                hours=settings.backup_full_interval_hours)
            kind = "full" if due else "incremental"
        if kind == "incremental" and (previous is None or base is None):
            kind = "full"
        row = (
            await session.execute(
                text("INSERT INTO backups (type, trigger, base_backup_id, prev_backup_id) "
                     "VALUES (:type, :trigger, :base, :prev) RETURNING *"),
                {"type": kind, "trigger": trigger,
                 "base": base["id"] if kind == "incremental" else None,
                 "prev": previous["id"] if kind == "incremental" else None},
            )
        ).mappings().one()
        backup_id = row["id"]
        tenants = (
            await session.execute(
                text("SELECT t.id, (SELECT w.id FROM workspaces w WHERE w.tenant_id = t.id "
                     "ORDER BY w.created_at LIMIT 1) AS workspace_id FROM tenants t "
                     "WHERE t.parent_tenant_id IS NULL AND t.status <> 'deleted' "
                     "AND (CAST(:only AS text[]) IS NULL OR t.id = ANY(:only)) ORDER BY t.id"),
                {"only": tenant_ids},
            )
        ).mappings().all()
        await session.commit()
    prev_entries = {
        entry["tenant_id"]: entry
        for entry in ((previous or {}).get("entries") or []) if kind == "incremental"
    }
    folder = root / backup_id
    folder.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    try:
        for tenant in tenants:
            if tenant["workspace_id"] is None:
                continue
            entries.append(await _backup_tenant(
                folder, backup_id, tenant["id"], tenant["workspace_id"],
                prev_entries.get(tenant["id"]),
            ))
        manifest = {
            "format": FORMAT,
            "backup_id": backup_id,
            "type": kind,
            "base_backup_id": row["base_backup_id"],
            "prev_backup_id": row["prev_backup_id"],
            "created_at": _now().isoformat(),
            "entries": entries,
        }
        manifest_bytes = json.dumps(manifest, indent=2, default=str).encode("utf-8")
        (folder / "manifest.json").write_bytes(manifest_bytes)
        verified = _verify_folder(folder, entries)
        as_of = min((datetime.fromisoformat(entry["as_of"]) for entry in entries),
                    default=row["started_at"])
        async with async_session_factory() as session:
            done = (
                await session.execute(
                    text("UPDATE backups SET status = 'done', finished_at = NOW(), "
                         "location = :location, entries = CAST(:entries AS jsonb), "
                         "documents = :documents, bytes = :bytes, sha256 = :sha, as_of = :as_of, "
                         "verified_at = CASE WHEN :verified THEN NOW() END "
                         "WHERE id = :id RETURNING *"),
                    {"id": backup_id, "location": str(folder),
                     "entries": json.dumps(entries, default=str),
                     "documents": sum(entry["documents"] for entry in entries),
                     "bytes": sum(entry["bytes"] for entry in entries) + len(manifest_bytes),
                     "sha": _sha(manifest_bytes), "as_of": as_of, "verified": verified},
                )
            ).mappings().one()
            await session.commit()
    except Exception as exc:
        logger.exception("backup %s failed", backup_id)
        async with async_session_factory() as session:
            await session.execute(
                text("UPDATE backups SET status = 'failed', finished_at = NOW(), error = :error "
                     "WHERE id = :id"),
                {"id": backup_id, "error": str(exc)[:2_000]},
            )
            await session.commit()
        await alerts.raise_alert(
            "backup.failed", f"backup {backup_id} failed: {str(exc)[:300]}",
            severity="critical", details={"backup_id": backup_id},
        )
        raise
    await _upload(folder, backup_id)
    return backup_out(dict(done))


async def _backup_tenant(
    folder: Path, backup_id: str, tenant_id: str, workspace_id: Any,
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    from app.auth import principal_for_admin
    from app.models import Workspace

    async with async_session_factory() as session:
        principal = principal_for_admin(await session.get(Workspace, workspace_id))
    export = await export_service.request_export(
        principal, scope="tenant", scope_ref=tenant_id,
        since_export_id=previous["export_id"] if previous else None,
        purpose="scheduled", requested_by=f"backup:{backup_id}", start=False,
    )
    export_id = export["export_id"]
    await export_service.run_export(export_id)
    async with async_session_factory() as session:
        job = (
            await session.execute(text("SELECT * FROM exports WHERE id = :id"), {"id": export_id})
        ).mappings().one()
        if job["status"] != "done":
            raise RuntimeError(f"export {export_id} for {tenant_id} failed: {job['error']}")
        data = await session.scalar(
            text("SELECT data FROM export_archives WHERE export_id = :id"), {"id": export_id}
        )
        # The archive now lives in backup storage; the export row keeps the
        # row-hash index the next incremental diffs against.
        await session.execute(
            text("DELETE FROM export_archives WHERE export_id = :id"), {"id": export_id}
        )
        await session.commit()
    name = f"{tenant_id}.zip"
    (folder / name).write_bytes(data)
    return {
        "tenant_id": tenant_id,
        "export_id": export_id,
        "kind": job["kind"],
        "file": name,
        "sha256": _sha(data),
        "bytes": len(data),
        "documents": int((job["counts"] or {}).get("documents", 0)),
        "as_of": job["as_of"].isoformat(),
    }


def _verify_folder(folder: Path, entries: list[dict[str, Any]]) -> bool:
    """Re-read every archive: file hash and the archive's own manifest hashes."""
    for entry in entries:
        data = (folder / entry["file"]).read_bytes()
        if _sha(data) != entry["sha256"]:
            return False
        manifest, archive = export_service._read_archive(data)
        try:
            export_service._verify(manifest, archive)
        except ApiError:
            return False
    return True


async def _upload(folder: Path, backup_id: str) -> None:
    command = settings.backup_upload_command
    if not command:
        return
    args = [part.replace("{path}", str(folder)) for part in shlex.split(command)]
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=1_800)
        if process.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", "replace")[-500:])
    except Exception as exc:  # noqa: BLE001 - the local copy is still valid
        await alerts.raise_alert(
            "backup.upload_failed", f"uploading backup {backup_id} failed",
            details={"backup_id": backup_id, "error": str(exc)[:500]},
        )


async def prune_backups() -> int:
    """Expire chains whose newest member is older than the retention window."""
    if not settings.backup_dir:
        return 0
    cutoff = _now() - timedelta(days=settings.backup_retention_days)
    async with async_session_factory() as session:
        latest = await _latest_done(session)
        rows = (
            await session.execute(
                text("SELECT COALESCE(base_backup_id, id) AS chain, MAX(finished_at) AS newest, "
                     "array_agg(id) AS ids, array_agg(location) AS locations FROM backups "
                     "WHERE status IN ('done', 'failed') GROUP BY 1")
            )
        ).mappings().all()
        expired = 0
        keep = None if latest is None else (latest["base_backup_id"] or latest["id"])
        for row in rows:
            if row["chain"] == keep or row["newest"] is None or row["newest"] >= cutoff:
                continue
            for location in row["locations"]:
                if location:
                    shutil.rmtree(location, ignore_errors=True)
            await session.execute(
                text("UPDATE backups SET status = 'expired' WHERE id = ANY(:ids)"),
                {"ids": list(row["ids"])},
            )
            expired += len(row["ids"])
        await session.commit()
    return expired


async def wal_status(session: AsyncSession) -> dict[str, Any] | None:
    mode = await session.scalar(text("SELECT current_setting('archive_mode')"))
    if mode not in ("on", "always"):
        return None
    row = (
        await session.execute(
            text("SELECT archived_count, last_archived_wal, last_archived_time, failed_count, "
                 "last_failed_time FROM pg_stat_archiver")
        )
    ).mappings().one()
    return {"type": "wal", "archive_mode": mode, **dict(row)}


async def list_backups(limit: int = 100) -> dict[str, Any]:
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                text("SELECT * FROM backups ORDER BY started_at DESC, id DESC LIMIT :limit"),
                {"limit": limit},
            )
        ).mappings().all()
        wal = await wal_status(session)
    return {
        "items": [backup_out(dict(row)) for row in rows],
        "wal": wal,
        "configured": bool(settings.backup_dir),
        "rpo_minutes": settings.backup_interval_minutes,
    }


async def get_backup(backup_id: str) -> dict[str, Any]:
    async with async_session_factory() as session:
        row = await _load(session, backup_id)
    if row is None:
        raise NotFound("backup", f"backup '{backup_id}' not found")
    return {**backup_out(row), "entries": row["entries"], "location": row["location"]}


async def health_timestamps() -> dict[str, Any]:
    """Timestamps only: safe for the unauthenticated /healthz."""
    async with async_session_factory() as session:
        row = (
            await session.execute(
                text("SELECT (SELECT MAX(finished_at) FROM backups WHERE status = 'done') AS b, "
                     "(SELECT MAX(finished_at) FROM restore_drills) AS d")
            )
        ).one()
    return {"last_backup_at": row[0], "last_restore_drill_at": row[1]}


# ----------------------------------------------------------------- restore


async def _chain(session: AsyncSession, backup: dict[str, Any]) -> list[dict[str, Any]]:
    chain = [backup]
    while chain[0]["type"] == "incremental":
        previous = await _load(session, chain[0]["prev_backup_id"])
        if previous is None or previous["status"] != "done":
            raise ApiError(409, "backup_chain_broken",
                           "a backup this one builds on is missing or expired",
                           details={"backup_id": chain[0]["prev_backup_id"]})
        chain.insert(0, previous)
    return chain


def _tenant_chains(chain: list[dict[str, Any]]) -> dict[str, list[tuple[Path, dict]]]:
    """Per root tenant: its latest full archive and the increments after it.

    Only tenants present in the last backup are restored, so a tenant deleted
    before the restore point does not come back.
    """
    plans: dict[str, list[tuple[Path, dict]]] = {}
    for backup in chain:
        folder = Path(backup["location"])
        for entry in backup["entries"] or []:
            if entry["kind"] == "full":
                plans[entry["tenant_id"]] = [(folder, entry)]
            else:
                plans.setdefault(entry["tenant_id"], []).append((folder, entry))
    last = {entry["tenant_id"] for entry in chain[-1]["entries"] or []}
    return {tenant: steps for tenant, steps in plans.items() if tenant in last}


def _read(folder: Path, entry: dict[str, Any]) -> bytes:
    data = (folder / entry["file"]).read_bytes()
    if _sha(data) != entry["sha256"]:
        raise ApiError(422, "checksum_mismatch", "a backup archive does not match its hash",
                       details={"file": str(folder / entry["file"])})
    return data


async def _resolve(
    session: AsyncSession, backup_id: str | None, point_in_time: datetime | None
) -> dict[str, Any]:
    if backup_id:
        backup = await _load(session, backup_id)
        if backup is None or backup["status"] != "done":
            raise NotFound("backup", f"backup '{backup_id}' not found")
        return backup
    if point_in_time is None:
        raise ApiError(422, "validation_error", "name backup_id or point_in_time",
                       details={"fields": ["backup_id", "point_in_time"]})
    row = (
        await session.execute(
            text("SELECT * FROM backups WHERE status = 'done' AND as_of <= :t "
                 "ORDER BY as_of DESC, id DESC LIMIT 1"),
            {"t": point_in_time},
        )
    ).mappings().one_or_none()
    if row is None:
        raise ApiError(404, "not_found", "no backup was taken before that point in time",
                       details={"point_in_time": point_in_time.isoformat()})
    return dict(row)


def _same_database(first: str, second: str) -> bool:
    a, b = make_url(first), make_url(second)
    return (a.host, a.port or 5432, a.database) == (b.host, b.port or 5432, b.database)


def staging_factory() -> tuple[Any, Any]:
    url = settings.staging_database_url
    if not url:
        raise ApiError(409, "staging_not_configured",
                       "set STAGING_DATABASE_URL to restore into staging",
                       details={"setting": "STAGING_DATABASE_URL"})
    if _same_database(url, settings.database_url):
        raise ApiError(409, "staging_not_configured",
                       "STAGING_DATABASE_URL must not point at the production database",
                       details={"setting": "STAGING_DATABASE_URL"})
    engine = create_async_engine(url, poolclass=NullPool)
    return engine, async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


async def _wipe_staging(engine: Any) -> None:
    await apply_schema(settings.staging_database_url)
    async with engine.begin() as conn:
        tables = (
            await conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                     "AND tablename <> 'schema_migrations'")
            )
        ).scalars().all()
        if tables:
            await conn.execute(text(
                "TRUNCATE " + ", ".join(f'"{name}"' for name in tables)
                + " RESTART IDENTITY CASCADE"
            ))
    # Re-seed whatever the schema bootstrap inserts.
    await apply_schema(settings.staging_database_url)


def restore_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "restore_id": row["id"],
        "status": row["status"],
        "backup_id": row["backup_id"],
        "point_in_time": row["point_in_time"],
        "target": row["target"],
        "tenant_id": row["target_tenant_id"],
        "chain": row["chain"],
        "result": row["result"],
        "error": row["error"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


async def request_restore(
    *,
    backup_id: str | None = None,
    point_in_time: datetime | None = None,
    target: str = "staging",
    tenant_id: str | None = None,
    confirm: str | None = None,
    requested_by: str = "admin",
    wait: bool = False,
) -> dict[str, Any]:
    if target not in TARGETS:
        raise ApiError(422, "validation_error", "target must be staging, new_workspace or "
                       "in_place", details={"field": "target", "allowed": list(TARGETS)})
    if target in ("new_workspace", "in_place") and not tenant_id:
        raise ApiError(422, "validation_error", f"target={target} restores one tenant: name it",
                       details={"field": "tenant_id"})
    if target == "in_place" and confirm != tenant_id:
        raise ApiError(409, "confirmation_required",
                       "an in-place restore replaces the tenant's current data; pass "
                       "confirm=<tenant_id>", details={"confirm_with": tenant_id})
    if target == "staging":
        staging_factory()[0].sync_engine.dispose()  # fail fast when not configured
    async with async_session_factory() as session:
        backup = await _resolve(session, backup_id, point_in_time)
        chain = await _chain(session, backup)
        plans = _tenant_chains(chain)
        if tenant_id and tenant_id not in plans:
            raise NotFound("tenant", f"tenant '{tenant_id}' is not in that backup")
        if target == "in_place":
            children = await session.scalar(
                text("SELECT count(*) FROM tenants WHERE parent_tenant_id = :id"),
                {"id": tenant_id},
            )
            if children:
                raise ApiError(422, "validation_error",
                               "in-place restore of a tenant tree is not supported; restore "
                               "to staging", details={"field": "target"})
        row = (
            await session.execute(
                text("INSERT INTO restore_runs (backup_id, point_in_time, target, "
                     "target_tenant_id, requested_by, chain) VALUES (:backup, :pit, :target, "
                     ":tenant, :by, CAST(:chain AS jsonb)) RETURNING *"),
                {"backup": backup["id"], "pit": point_in_time, "target": target,
                 "tenant": tenant_id, "by": requested_by,
                 "chain": json.dumps([item["id"] for item in chain])},
            )
        ).mappings().one()
        await session.commit()
    job = asyncio.create_task(_run_restore(row["id"], chain, plans, target, tenant_id))
    _running.add(job)
    job.add_done_callback(_running.discard)
    if wait:
        await job
        return await get_restore(row["id"])
    return restore_out(dict(row))


async def get_restore(restore_id: str) -> dict[str, Any]:
    async with async_session_factory() as session:
        row = (
            await session.execute(text("SELECT * FROM restore_runs WHERE id = :id"),
                                  {"id": restore_id})
        ).mappings().one_or_none()
    if row is None:
        raise NotFound("restore", f"restore '{restore_id}' not found")
    return restore_out(dict(row))


async def _run_restore(
    restore_id: str, chain: list[dict], plans: dict, target: str, tenant_id: str | None
) -> None:
    try:
        result = await _restore(restore_id, plans, target, tenant_id)
        status, error = "done", None
    except ApiError as exc:
        result, status, error = {}, "failed", exc.body()["error"]
    except Exception as exc:  # noqa: BLE001 - recorded on the run
        logger.exception("restore %s failed", restore_id)
        result, status, error = {}, "failed", {"message": str(exc)[:2_000]}
    async with async_session_factory() as session:
        await session.execute(
            text("UPDATE restore_runs SET status = :status, result = CAST(:result AS jsonb), "
                 "error = CAST(:error AS jsonb), finished_at = NOW() WHERE id = :id"),
            {"id": restore_id, "status": status, "result": json.dumps(result, default=str),
             "error": json.dumps(error) if error else None},
        )
        await session.commit()


async def _restore(
    restore_id: str, plans: dict[str, list[tuple[Path, dict]]], target: str,
    tenant_id: str | None,
) -> dict[str, Any]:
    actor = f"restore:{restore_id}"
    selected = {tenant_id: plans[tenant_id]} if tenant_id else plans
    engine = None
    factory = async_session_factory
    pre_restore_export = None
    saved_passwords: dict[str, str] = {}
    if target == "staging":
        engine, factory = staging_factory()
        await _wipe_staging(engine)
    elif target == "in_place":
        pre_restore_export, saved_passwords = await _clear_tenant(tenant_id, actor)
    tenants_out = []
    try:
        for tenant, steps in selected.items():
            imports = []
            for position, (folder, entry) in enumerate(steps):
                data = _read(folder, entry)
                if position == 0:
                    mode = "new_tenant" if target == "staging" else "new_workspace"
                    into = None if target == "staging" else tenant
                else:
                    mode, into = "increment", (None if target == "staging" else tenant)
                done = await export_service.import_archive(
                    None, data, target=mode, into_tenant_id=into,
                    session_factory=factory, actor=actor,
                )
                imports.append({"import_id": done["import_id"], "export_id": entry["export_id"],
                                "kind": entry["kind"]})
            tenants_out.append({"tenant_id": tenant, "imports": imports})
        if target == "in_place":
            await _after_in_place(tenant_id, saved_passwords)
    finally:
        if engine is not None:
            await engine.dispose()
    return {
        "tenants": tenants_out,
        "pre_restore_export_id": pre_restore_export,
        "warnings": [] if target == "staging" else [
            {"code": "keys_rotated",
             "message": "restored workspaces have new keys; rotate them from the admin "
                        "console. Agent tokens must be re-issued."},
        ],
    }


async def _clear_tenant(tenant_id: str, actor: str) -> tuple[str | None, dict[str, str]]:
    """Before an in-place restore: export the current state, then remove it."""
    from app.auth import principal_for_admin
    from app.models import Workspace

    async with async_session_factory() as session:
        workspace_id = await session.scalar(
            text("SELECT id FROM workspaces WHERE tenant_id = :t ORDER BY created_at LIMIT 1"),
            {"t": tenant_id},
        )
        export_id = None
        if workspace_id is not None:
            principal = principal_for_admin(await session.get(Workspace, workspace_id))
            export = await export_service.request_export(
                principal, scope="tenant", scope_ref=tenant_id, purpose="scheduled",
                requested_by=f"{actor}:pre-restore", start=False,
            )
            export_id = export["export_id"]
            await export_service.run_export(export_id)
        passwords = {
            str(row[0]): row[1]
            for row in (
                await session.execute(
                    text("SELECT u.id, u.password_hash FROM human_users u JOIN workspaces w "
                         "ON w.id = u.workspace_id WHERE w.tenant_id = :t"),
                    {"t": tenant_id},
                )
            ).all()
        }
        await session.execute(text("DELETE FROM workspaces WHERE tenant_id = :t"),
                              {"t": tenant_id})
        await session.commit()
    return export_id, passwords


async def _after_in_place(tenant_id: str, passwords: dict[str, str]) -> None:
    """Keep existing sign-ins working and re-create memberships."""
    from app.models import HumanUser
    from app.services.tenants import sync_membership

    async with async_session_factory() as session:
        for user_id, password_hash in passwords.items():
            await session.execute(
                text("UPDATE human_users SET password_hash = :hash WHERE id = CAST(:id AS uuid)"),
                {"id": user_id, "hash": password_hash},
            )
        users = (
            await session.execute(
                text("SELECT u.id FROM human_users u JOIN workspaces w ON w.id = u.workspace_id "
                     "WHERE w.tenant_id = :t"),
                {"t": tenant_id},
            )
        ).scalars().all()
        for user_id in users:
            user = await session.get(HumanUser, user_id)
            await sync_membership(session, user)
        await session.commit()


# ------------------------------------------------------------------ drills


def _expected_documents(steps: list[tuple[Path, dict]]) -> dict[str, str]:
    """Document id -> content_hash after applying a tenant's chain, from the archives."""
    documents: dict[str, str] = {}
    for folder, entry in steps:
        manifest, archive = export_service._read_archive(_read(folder, entry))
        del manifest
        names = archive.namelist()
        if "objects.jsonl" in names:
            for line in archive.read("objects.jsonl").decode("utf-8").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                if item.get("table") == "shared_documents":
                    documents[str(item["row"]["id"])] = item["row"]["content_hash"]
        if "deleted.jsonl" in names:
            for line in archive.read("deleted.jsonl").decode("utf-8").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                if item.get("table") == "shared_documents":
                    documents.pop(str(item["pk"][0]), None)
    return documents


async def run_drill(*, trigger: str = "manual") -> dict[str, Any]:
    """Restore the latest backup chain into staging and check it."""
    started = time.monotonic()
    async with async_session_factory() as session:
        backup = await _latest_done(session)
        drill_id = await session.scalar(
            text("INSERT INTO restore_drills (backup_id, trigger) VALUES (:b, :t) RETURNING id"),
            {"b": backup["id"] if backup else None, "t": trigger},
        )
        await session.commit()
    status, error, expected_count, restored_count, sampled = "failed", None, None, None, 0
    mismatches: list[dict[str, Any]] = []
    try:
        if backup is None:
            raise RuntimeError("there is no finished backup to restore")
        restore = await request_restore(backup_id=backup["id"], target="staging",
                                        requested_by=f"drill:{drill_id}", wait=True)
        if restore["status"] != "done":
            raise RuntimeError(f"restore failed: {json.dumps(restore['error'])[:500]}")
        async with async_session_factory() as session:
            plans = _tenant_chains(await _chain(session, backup))
        expected: dict[str, str] = {}
        for steps in plans.values():
            expected.update(_expected_documents(steps))
        engine, factory = staging_factory()
        try:
            async with factory() as staging:
                restored = {
                    str(row[0]): row[1]
                    for row in (
                        await staging.execute(text("SELECT id, content_hash FROM shared_documents"))
                    ).all()
                }
                expected_count, restored_count = len(expected), len(restored)
                if set(restored) != set(expected):
                    missing = sorted(set(expected) - set(restored))[:20]
                    extra = sorted(set(restored) - set(expected))[:20]
                    mismatches.append({"kind": "document_set", "missing": missing,
                                       "unexpected": extra})
                sample = random.sample(sorted(expected),
                                       min(settings.restore_drill_sample, len(expected)))
                sampled = len(sample)
                for chunk_start in range(0, len(sample), 500):
                    ids = sample[chunk_start:chunk_start + 500]
                    rows = (
                        await staging.execute(
                            text("SELECT CAST(id AS text), content FROM shared_documents "
                                 "WHERE CAST(id AS text) = ANY(:ids)"),
                            {"ids": ids},
                        )
                    ).all()
                    for doc_id, content in rows:
                        actual = _sha((content or "").encode("utf-8"))
                        if actual != expected[doc_id]:
                            mismatches.append({"kind": "sha256", "document_id": doc_id})
            await _wipe_staging(engine)
        finally:
            await engine.dispose()
        status = "passed" if not mismatches else "failed"
    except ApiError as exc:
        error = json.dumps(exc.body()["error"])[:2_000]
    except Exception as exc:  # noqa: BLE001 - recorded on the drill
        logger.exception("restore drill %s failed", drill_id)
        error = str(exc)[:2_000]
    async with async_session_factory() as session:
        row = (
            await session.execute(
                text("UPDATE restore_drills SET status = :status, documents_expected = :e, "
                     "documents_restored = :r, sampled = :s, mismatches = CAST(:m AS jsonb), "
                     "duration_ms = :ms, error = :error, finished_at = NOW() WHERE id = :id "
                     "RETURNING *"),
                {"id": drill_id, "status": status, "e": expected_count, "r": restored_count,
                 "s": sampled, "m": json.dumps(mismatches[:100]),
                 "ms": int((time.monotonic() - started) * 1000), "error": error},
            )
        ).mappings().one()
        await session.commit()
    if status != "passed":
        await alerts.raise_alert(
            "restore_drill.failed", f"restore drill {drill_id} failed",
            severity="critical",
            details={"drill_id": drill_id, "error": error, "mismatches": mismatches[:10]},
        )
    return drill_out(dict(row))


def drill_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "drill_id": row["id"],
        "backup_id": row["backup_id"],
        "trigger": row["trigger"],
        "status": row["status"],
        "documents_expected": row["documents_expected"],
        "documents_restored": row["documents_restored"],
        "sampled": row["sampled"],
        "mismatches": row["mismatches"],
        "duration_ms": row["duration_ms"],
        "error": row["error"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


async def list_drills(limit: int = 50) -> dict[str, Any]:
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                text("SELECT * FROM restore_drills ORDER BY started_at DESC LIMIT :limit"),
                {"limit": limit},
            )
        ).mappings().all()
    return {"items": [drill_out(dict(row)) for row in rows]}


# --------------------------------------------------------------- scheduler


async def scheduler_tick() -> None:
    if not settings.backup_dir or not settings.backup_scheduler_enabled:
        return
    async with async_session_factory() as session:
        latest = await _latest_done(session)
        last_drill = await session.scalar(text("SELECT MAX(started_at) FROM restore_drills"))
    due = latest is None or latest["finished_at"] < _now() - timedelta(
        minutes=settings.backup_interval_minutes)
    if due:
        try:
            await run_backup(trigger="schedule")
        except ApiError as exc:
            if exc.code != "backup_in_progress":
                raise
    await prune_backups()
    if settings.staging_database_url and (
        last_drill is None
        or last_drill < _now() - timedelta(days=settings.restore_drill_interval_days)
    ):
        await run_drill(trigger="schedule")


async def backup_scheduler_loop() -> None:
    while True:
        try:
            await scheduler_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - background resilience
            logger.exception("backup scheduler iteration failed")
            hour = _now().strftime("%Y%m%d%H")
            await alerts.raise_alert(
                "backup.scheduler_failed", f"backup scheduler failed: {str(exc)[:300]}",
                severity="critical", dedupe_key=f"backup.scheduler_failed:{hour}",
            )
        await asyncio.sleep(60)

