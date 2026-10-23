"""Release discipline: every deploy is visible to agents (2026-10-23 retest, finding 1).

Each build has a version (``settings.app_version``) with notes in
``RELEASE_NOTES``. On start the first replica to see a new version records it
in ``platform_releases`` (with the root primitive list, so additions and
removals are computed, not written by hand), emits ``platform.released`` in
every workspace and rewrites ``system/changelog``. ``changes_since`` serves
orientation's ``platform_changes`` for an agent coming back after a release.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text

from app.config import get_settings
from app.database import async_session_factory

logger = logging.getLogger(__name__)
settings = get_settings()

CHANGELOG_KEY = "system/changelog"

# Newest first. Every release that changes behaviour gets an entry; the
# version must match settings.app_version when it ships.
RELEASE_NOTES: list[dict[str, Any]] = [
    {
        "version": "3.5.0",
        "date": "2026-10-23",
        "summary": "Phase 3: lifecycle, digests, peer reviews, scarce resources, retrieval "
                   "baseline, lineage attestations; partitioned event log; release "
                   "notices, capabilities and incremental orientation.",
        "highlights": [
            "Documents can expire or be superseded; 2026-11 queries hide superseded, "
            "expired and cold documents unless include_lifecycle names them.",
            "Named watermarks and get_digest summarise what happened while you were away.",
            "Peer reviews (request_peer_review / submit_peer_review) and scarce resources "
            "(acquire_resource / release_resource) are first-class.",
            "get_lineage_attestation signs a pseudonymous grant chain another tenant can "
            "verify with the public key.",
            "Applying a proposal no longer waits on embeddings; index repair only touches "
            "documents whose index is wrong.",
            "agent_patch_shared_state with expected_version=0 creates a slot; renaming a "
            "document into another namespace's prefix moves it there.",
            "Every release emits platform.released and updates system/changelog; the root "
            "lists capabilities and feature flags; get_orientation(since=...) is incremental.",
            "Every change reaches the people and agents it concerns: list_inbox / ack_inbox.",
            "Agent tokens take a permission delta on their role (scope_add / scope_remove); "
            "children inherit removals and default to a 12-hour lifetime.",
            "Quality control: acceptance items, machine gates (record_gate_result), audit "
            "views (get_audit_view), change claims (report_change_claim / "
            "verify_change_claim), autonomy tiers, sampling, escapes and a per-domain "
            "circuit; enforced when QUALITY_CONTROL=true.",
            "State-slot locks are leases (list_leases, heartbeat, release_lease).",
            "Cheaper calls: batch runs up to 20 tool calls in one request; list, sync and "
            "orientation default to a summary detail level on MCP.",
            "Addressed events always reach their target; bare mentions are refused; failing "
            "webhooks retry three times, then go DEAD.",
            "get_orientation carries a memory_seed (ack_memory_seed); onboarding step 4 is "
            "register_agent, and every token is in the directory.",
        ],
    },
    {
        "version": "3.4.0",
        "date": "2026-10-22",
        "summary": "Phase 2: task collaboration, provenance and recall, token budgets, "
                   "log documents, situation awareness and the tenant tree.",
        "highlights": [],
    },
    {
        "version": "3.3.0",
        "date": "2026-10-22",
        "summary": "Phase 1: typed events and subscriptions, async indexing, census "
                   "listing, export/import, unified leases, token tree, orientation.",
        "highlights": [],
    },
]


def notes_for(version: str) -> dict[str, Any]:
    return next((item for item in RELEASE_NOTES if item["version"] == version),
                {"version": version, "date": None, "summary": "", "highlights": []})


def render_changelog(rows: list[dict[str, Any]]) -> str:
    lines = ["# Platform changelog / 平台变更记录", "",
             "Written by the platform on every release. Newest first.", ""]
    for row in rows:
        notes = notes_for(row["version"])
        lines += [f"## {row['version']} ({str(row['released_at'])[:10]})", "",
                  notes["summary"] or "-", ""]
        lines += [f"- {item}" for item in notes["highlights"]]
        if row.get("added"):
            lines.append(f"- New primitives: {', '.join(row['added'])}")
        if row.get("removed"):
            lines.append(f"- Removed primitives: {', '.join(row['removed'])}")
        lines.append("")
    return "\n".join(lines)


async def history(limit: int = 20) -> list[dict[str, Any]]:
    async with async_session_factory() as session:
        rows = (await session.execute(text(
            "SELECT version, previous_version, schema_version, released_at, added, removed "
            "FROM platform_releases ORDER BY released_at DESC LIMIT :l"), {"l": limit})
        ).mappings().all()
    return [dict(row) for row in rows]


def _public(row: dict[str, Any]) -> dict[str, Any]:
    notes = notes_for(row["version"])
    return {"version": row["version"], "from": row.get("previous_version"),
            "released_at": row["released_at"].isoformat()
            if isinstance(row["released_at"], datetime) else row["released_at"],
            "schema_version": row.get("schema_version"),
            "added_primitives": row.get("added") or [],
            "removed_primitives": row.get("removed") or [],
            "summary": notes["summary"], "highlights": notes["highlights"],
            "changelog": {"namespace": "system", "doc_key": CHANGELOG_KEY}}


async def released_at(version: str) -> datetime | None:
    async with async_session_factory() as session:
        return await session.scalar(
            text("SELECT released_at FROM platform_releases WHERE version = :v"),
            {"v": version})


async def current() -> dict[str, Any] | None:
    rows = await history(1)
    return _public(rows[0]) if rows else None


async def changes_since(moment: datetime | None) -> list[dict[str, Any]]:
    """Releases after ``moment`` (all of them when None), newest first."""
    rows = await history(50)
    return [_public(row) for row in rows if moment is None or row["released_at"] > moment]


async def write_changelog(session: Any, workspace_id: Any) -> None:
    from app.services.onboarding import write_platform_document

    rows = await history(50)
    if not rows:
        return
    await write_platform_document(
        session, workspace_id, namespace="system", doc_key=CHANGELOG_KEY,
        title="Platform changelog", content=render_changelog(rows),
        frontmatter={"kind": "changelog", "version": rows[0]["version"]},
        author="system", change_summary=f"release {rows[0]['version']}")


async def record_release(primitives: list[str]) -> bool:
    """Record this build once; announce it everywhere. True when it was new."""
    from app.database import CURRENT_SCHEMA_VERSION
    from app.services.common import append_event

    version = settings.app_version
    async with async_session_factory() as session:
        previous = (await session.execute(text(
            "SELECT version, primitives FROM platform_releases ORDER BY released_at DESC "
            "LIMIT 1"))).mappings().one_or_none()
        if previous is not None and previous["version"] == version:
            return False
        before = set(previous["primitives"] or []) if previous else set()
        added = sorted(set(primitives) - before) if previous else []
        removed = sorted(before - set(primitives)) if previous else []
        inserted = await session.scalar(text(
            "INSERT INTO platform_releases (version, previous_version, schema_version, "
            "primitives, added, removed) VALUES (:v, :p, :s, CAST(:prims AS jsonb), "
            "CAST(:a AS jsonb), CAST(:r AS jsonb)) ON CONFLICT (version) DO NOTHING "
            "RETURNING version"),
            {"v": version, "p": previous["version"] if previous else None,
             "s": CURRENT_SCHEMA_VERSION, "prims": json.dumps(sorted(primitives)),
             "a": json.dumps(added), "r": json.dumps(removed)})
        await session.commit()
        if inserted is None:
            return False  # another replica recorded it first
        workspaces = (await session.execute(text("SELECT id FROM workspaces"))).scalars().all()
    notes = notes_for(version)
    for workspace_id in workspaces:
        try:
            async with async_session_factory() as session:
                await write_changelog(session, workspace_id)
                await append_event(
                    session, workspace_id=workspace_id, agent="platform",
                    event_type="PLATFORM_RELEASED",
                    payload={"details": {
                        "from": previous["version"] if previous else None, "to": version,
                        "schema_version": CURRENT_SCHEMA_VERSION,
                        "added_primitives": added, "removed_primitives": removed,
                        "summary": notes["summary"],
                        "changelog": {"namespace": "system", "doc_key": CHANGELOG_KEY}}},
                    namespace="system")
                await session.commit()
        except Exception:  # noqa: BLE001 - one workspace must not stop the others
            logger.exception("could not announce the release in a workspace")
    logger.info("release %s recorded and announced in %s workspace(s)", version,
                len(workspaces))
    return True
