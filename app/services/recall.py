"""Data recall along derivation edges (PRD AG-TC-02).

A recall starts at versions of one document and walks ``doc_derivations``
breadth-first, at most ``max_depth`` hops. Every version it reaches is marked
tainted (``{kind: "recall", ref: recall_id}``), tasks that pinned it get a
``recall_flags`` entry, and the version's author, the document's maintainer
and the tasks' holders get ``doc.recalled`` with the hop count and the
previous hop. Nothing is changed or deleted. A downstream author resolves a
node by writing a version that no longer depends on recalled versions with
``resolves_recall``; a person may dismiss a node. When no node is open the
recall closes (``recall.closed``).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, ensure_namespace
from app.errors import ApiError, Forbidden, NotFound
from app.models import CollabTask, DocumentVersion, SharedDocument
from app.services.common import append_event
from app.warnings_context import add_warning

MAX_DEPTH = 10
NODE_LIMIT = 10_000


def _ref(document: dict[str, Any] | SharedDocument, version: int) -> dict[str, Any]:
    if isinstance(document, SharedDocument):
        return {"namespace": document.namespace, "doc_key": document.doc_key,
                "version": version}
    return {"namespace": document["namespace"], "doc_key": document["doc_key"],
            "version": version}


def _actor_ref(label: str | None) -> str | None:
    if not label:
        return None
    if label.startswith(("agent:", "human:")):
        return label
    return f"agent:{label}"


async def _document(session: AsyncSession, principal: Principal, namespace: str,
                    doc_key: str) -> SharedDocument:
    document = await session.scalar(
        select(SharedDocument).where(SharedDocument.workspace_id == principal.workspace.id,
                                     SharedDocument.namespace == namespace,
                                     SharedDocument.doc_key == doc_key))
    if document is None:
        raise NotFound("shared_document", f"document '{namespace}/{doc_key}' not found")
    ensure_namespace(principal, namespace)
    return document


async def recall_document(
    session: AsyncSession,
    principal: Principal,
    *,
    namespace: str,
    doc_key: str,
    versions: list[int] | str,
    reason: str,
    severity: str,
    max_depth: int,
) -> dict[str, Any]:
    document = await _document(session, principal, namespace, doc_key)
    authors = set((await session.execute(
        select(DocumentVersion.author).where(DocumentVersion.document_id == document.id)
    )).scalars().all())
    if not principal.is_human and principal.agent_signature not in authors | {
            document.created_by}:
        raise Forbidden("only the document's author or a person can recall it")
    all_versions = sorted((await session.execute(
        select(DocumentVersion.version).where(DocumentVersion.document_id == document.id)
    )).scalars().all())
    chosen = all_versions if versions == "all" else sorted(set(versions))
    unknown = [version for version in chosen if version not in all_versions]
    if unknown or not chosen:
        raise ApiError(422, "validation_error", "versions must exist",
                       details={"field": "versions", "unknown": unknown,
                                "available": all_versions[-20:]})
    max_depth = max(1, min(max_depth, MAX_DEPTH))

    recall_id = await session.scalar(
        text("INSERT INTO recalls (workspace_id, document_id, versions, reason, severity, "
             "max_depth, created_by) VALUES (:ws, :doc, :versions, :reason, :severity, "
             ":depth, :by) RETURNING id"),
        {"ws": principal.workspace.id, "doc": document.id, "versions": chosen,
         "reason": reason, "severity": severity, "depth": max_depth,
         "by": principal.agent_signature})

    visited: set[tuple[uuid.UUID, int]] = {(document.id, version) for version in chosen}
    frontier = sorted(visited)
    nodes: list[dict[str, Any]] = []
    depth = 0
    truncated = False
    boundary: list[dict[str, Any]] = []
    while frontier:
        rows = (
            await session.execute(
                text("SELECT e.from_document_id, e.from_version, e.to_document_id, "
                     "e.to_version FROM doc_derivations e JOIN unnest(CAST(:ids AS uuid[]), "
                     "CAST(:versions AS int[])) AS f(doc, version) ON e.from_document_id = "
                     "f.doc AND e.from_version = f.version WHERE e.workspace_id = :ws"),
                {"ids": [str(doc) for doc, _ in frontier],
                 "versions": [version for _, version in frontier],
                 "ws": principal.workspace.id},
            )
        ).all()
        fresh = [(row[0], row[1], row[2], row[3]) for row in rows
                 if (row[2], row[3]) not in visited]
        if not fresh:
            break
        if depth >= max_depth or len(nodes) >= NODE_LIMIT:
            truncated = True
            boundary_keys = sorted({(row[0], row[1]) for row in fresh})
            boundary = [{"document_id": str(doc), "version": version}
                        for doc, version in boundary_keys]
            break
        depth += 1
        next_frontier = []
        for from_doc, from_version, to_doc, to_version in fresh:
            if (to_doc, to_version) in visited:
                continue
            visited.add((to_doc, to_version))
            next_frontier.append((to_doc, to_version))
            nodes.append({"document_id": to_doc, "version": to_version, "hop": depth,
                          "via_document_id": from_doc, "via_version": from_version})
        frontier = next_frontier

    documents = {}
    ids = {node["document_id"] for node in nodes} | {node["via_document_id"] for node in nodes}
    ids.add(document.id)
    for row in (await session.execute(
            select(SharedDocument).where(SharedDocument.id.in_(ids)))).scalars().all():
        documents[row.id] = row
    for item in boundary:
        doc = documents.get(uuid.UUID(item["document_id"]))
        if doc is not None:
            item.update(namespace=doc.namespace, doc_key=doc.doc_key)

    entry = {"kind": "recall", "ref": recall_id}
    affected_keys = {(documents[node["document_id"]].namespace,
                      documents[node["document_id"]].doc_key): node for node in nodes
                     if node["document_id"] in documents}
    tasks_flagged: set[uuid.UUID] = set()
    node_holders: dict[tuple[Any, int], list[str]] = {}
    for task in (await session.execute(
            select(CollabTask).where(CollabTask.workspace_id == principal.workspace.id,
                                     CollabTask.context_versions_pinned.is_not(None))
    )).scalars().all():
        flags = list(task.recall_flags or [])
        for key, version in (task.context_versions_pinned or {}).items():
            pinned_namespace, _, pinned_key = key.partition(":")
            node = affected_keys.get((pinned_namespace, pinned_key))
            if node is not None and node["version"] == int(version):
                flags.append({"recall_id": recall_id, "namespace": pinned_namespace,
                              "doc_key": pinned_key, "version": int(version)})
                holder = task.lease_owner or task.claimed_by
                if holder:
                    node_holders.setdefault(
                        (node["document_id"], node["version"]), []).append(holder)
        if len(flags) != len(task.recall_flags or []):
            task.recall_flags = flags
            tasks_flagged.add(task.id)

    principals: set[str] = set()
    for node in nodes:
        target = documents.get(node["document_id"])
        via = documents.get(node["via_document_id"])
        if target is None:
            continue
        await session.execute(
            text("INSERT INTO recall_nodes (recall_id, workspace_id, document_id, version, hop, "
                 "via_document_id, via_version) VALUES (:id, :ws, :doc, :version, :hop, "
                 ":via, :via_version) ON CONFLICT DO NOTHING"),
            {"id": recall_id, "ws": principal.workspace.id, "doc": target.id,
             "version": node["version"], "hop": node["hop"], "via": node["via_document_id"],
             "via_version": node["via_version"]})
        await session.execute(
            text("UPDATE document_versions SET tainted = TRUE, trust_level = 'UNTRUSTED', "
                 "taint_sources = taint_sources || CAST(:entry AS jsonb) "
                 "WHERE document_id = :doc AND version = :version"),
            {"entry": json.dumps([entry]), "doc": target.id, "version": node["version"]})
        if node["version"] == target.version:
            _mark_current(target, entry, severity)
        author = await session.scalar(
            select(DocumentVersion.author).where(DocumentVersion.document_id == target.id,
                                                 DocumentVersion.version == node["version"]))
        maintainer = (target.frontmatter or {}).get("maintainer")
        holders = node_holders.get((target.id, node["version"]), [])
        targets = [ref for ref in dict.fromkeys(
            [_actor_ref(author), _actor_ref(maintainer) if isinstance(maintainer, str) else None,
             *[_actor_ref(holder) for holder in holders]]) if ref]
        principals.update(targets)
        await append_event(
            session, workspace_id=principal.workspace.id, agent=principal.agent_signature,
            event_type="DOC_RECALLED",
            payload={"doc_key": target.doc_key, "id": target.canonical_id,
                     "version": node["version"], "targets": targets,
                     "details": {"recall_id": recall_id, "hop": node["hop"],
                                 "via": _ref(via, node["via_version"]) if via else None,
                                 "severity": severity, "reason": reason}},
            namespace=target.namespace)
    if severity == "block" and document.version in chosen:
        document.recall_severity = "block"
    await session.execute(
        text("UPDATE recalls SET depth_reached = :depth, truncated = :truncated, "
             "boundary = CAST(:boundary AS jsonb) WHERE id = :id"),
        {"depth": depth, "truncated": truncated, "boundary": json.dumps(boundary),
         "id": recall_id})
    if not nodes:
        await _close(session, recall_id, principal.workspace.id)
    await session.commit()
    return {"recall_id": recall_id,
            "affected": {"documents": len(nodes), "tasks": len(tasks_flagged),
                         "principals": sorted(principals)},
            "depth_reached": depth, "truncated": truncated, "boundary": boundary}


def _mark_current(document: SharedDocument, entry: dict[str, Any], severity: str) -> None:
    provenance = list(document.provenance_taint or [])
    if entry not in provenance:
        provenance.append(entry)
    document.provenance_taint = provenance
    sources = list(document.taint_sources or [])
    if entry not in sources:
        sources.append(entry)
    document.taint_sources = sources
    document.tainted = True
    document.trust_level = "UNTRUSTED"
    if severity == "block" or document.recall_severity is None:
        document.recall_severity = severity


async def _close(session: AsyncSession, recall_id: str, workspace_id: Any) -> None:
    still_open = await session.scalar(
        text("SELECT count(*) FROM recall_nodes WHERE recall_id = :id AND status = 'open'"),
        {"id": recall_id})
    if still_open:
        return
    closed = await session.scalar(
        text("UPDATE recalls SET status = 'closed', closed_at = NOW() WHERE id = :id AND "
             "status = 'open' RETURNING created_by"), {"id": recall_id})
    if closed is not None:
        await append_event(session, workspace_id=workspace_id, agent="recall",
                           event_type="RECALL_CLOSED",
                           payload={"recall_id": recall_id, "targets": [_actor_ref(closed)]})


async def get_recall(session: AsyncSession, principal: Principal,
                     recall_id: str) -> dict[str, Any]:
    row = (await session.execute(
        text("SELECT r.*, d.namespace, d.doc_key FROM recalls r JOIN shared_documents d ON "
             "d.id = r.document_id WHERE r.id = :id AND r.workspace_id = :ws"),
        {"id": recall_id, "ws": principal.workspace.id})).mappings().one_or_none()
    if row is None:
        raise NotFound("recall", f"recall '{recall_id}' not found")
    nodes = (await session.execute(
        text("SELECT n.*, d.namespace, d.doc_key, v.namespace AS via_namespace, "
             "v.doc_key AS via_doc_key FROM recall_nodes n JOIN shared_documents d ON "
             "d.id = n.document_id LEFT JOIN shared_documents v ON v.id = n.via_document_id "
             "WHERE n.recall_id = :id ORDER BY n.hop, d.doc_key"), {"id": recall_id}
    )).mappings().all()
    return {
        "recall_id": row["id"], "status": row["status"],
        "source": {"namespace": row["namespace"], "doc_key": row["doc_key"],
                   "versions": list(row["versions"])},
        "reason": row["reason"], "severity": row["severity"], "max_depth": row["max_depth"],
        "depth_reached": row["depth_reached"], "truncated": row["truncated"],
        "boundary": row["boundary"], "created_by": row["created_by"],
        "created_at": row["created_at"].isoformat(),
        "closed_at": row["closed_at"].isoformat() if row["closed_at"] else None,
        "nodes": [{"namespace": node["namespace"], "doc_key": node["doc_key"],
                   "version": node["version"], "hop": node["hop"],
                   "via": {"namespace": node["via_namespace"], "doc_key": node["via_doc_key"],
                           "version": node["via_version"]} if node["via_doc_key"] else None,
                   "status": node["status"], "resolved_by": node["resolved_by"]}
                  for node in nodes],
    }


async def _recalled_versions(session: AsyncSession, recall_id: str) -> set[tuple[Any, int]]:
    row = (await session.execute(
        text("SELECT document_id, versions FROM recalls WHERE id = :id"), {"id": recall_id}
    )).one_or_none()
    if row is None:
        return set()
    found = {(row[0], version) for version in row[1]}
    found |= {(doc, version) for doc, version in (await session.execute(
        text("SELECT document_id, version FROM recall_nodes WHERE recall_id = :id "
             "AND status = 'open'"), {"id": recall_id})).all()}
    return found


async def resolve_by_write(session: AsyncSession, principal: Principal,
                           document: SharedDocument, provenance: Any, recall_id: str) -> None:
    """A new version written with ``resolves_recall`` resolves this document's nodes."""
    exists = await session.scalar(
        text("SELECT 1 FROM recalls WHERE id = :id AND workspace_id = :ws"),
        {"id": recall_id, "ws": principal.workspace.id})
    if not exists:
        raise NotFound("recall", f"recall '{recall_id}' not found")
    recalled = await _recalled_versions(session, recall_id)
    still = [key for key in provenance.edges if key in recalled
             and key[0] != document.id]
    if still:
        add_warning("recall_still_depends",
                    "this version still derives from recalled versions; the node stays open",
                    recall_id=recall_id, count=len(still))
        return
    await session.execute(
        text("UPDATE recall_nodes SET status = 'resolved', resolved_by = :by, "
             "resolved_at = NOW() WHERE recall_id = :id AND document_id = :doc AND "
             "status = 'open'"),
        {"by": principal.agent_signature, "id": recall_id, "doc": document.id})
    await _close(session, recall_id, principal.workspace.id)


async def dismiss_node(session: AsyncSession, principal: Principal, recall_id: str, *,
                       namespace: str, doc_key: str, version: int,
                       note: str | None) -> dict[str, Any]:
    """A person confirms a node is not affected."""
    if not principal.is_human:
        raise Forbidden("only a person can dismiss a recall node")
    document = await _document(session, principal, namespace, doc_key)
    updated = await session.scalar(
        text("UPDATE recall_nodes SET status = 'dismissed', resolved_by = :by, "
             "resolved_at = NOW() WHERE recall_id = :id AND document_id = :doc AND "
             "version = :version AND workspace_id = :ws RETURNING version"),
        {"by": principal.agent_signature, "id": recall_id, "doc": document.id,
         "version": version, "ws": principal.workspace.id})
    if updated is None:
        raise NotFound("recall_node", "no such node in this recall")
    if version == document.version:
        from app.services.taint import refresh_document_taint

        entry = {"kind": "recall", "ref": recall_id}
        document.provenance_taint = [item for item in document.provenance_taint or []
                                     if item != entry]
        document.recall_severity = None
        await refresh_document_taint(session, document)
    await _close(session, recall_id, principal.workspace.id)
    await session.commit()
    return await get_recall(session, principal, recall_id) | {"note": note}
