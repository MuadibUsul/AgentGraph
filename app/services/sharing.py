"""Cross-node visibility inside one tree (PRD AG-ID-12).

* Nodes are isolated by default: another node's objects answer 404.
* A share grants read or write on a document, namespace, workspace or task
  to a node or a principal of the same tree (422 ``share_cross_root_not_allowed``
  otherwise). Shared objects stay where they are; a grantee's writes carry the
  grantee's own lineage stamp. Revocation applies to the next request.
* Owners and admins of an ancestor see a subtree's metadata, never its
  content, unless they open an admin access session: time-boxed, with a
  reason, audited per read and announced to the node's owners at once.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.database import async_session_factory
from app.errors import ApiError, Forbidden, NotFound
from app.models import SharedDocument, Workspace
from app.services.common import append_event, utcnow
from app.services.tree import node, role_on

ADMIN_ACCESS_MAX_SECONDS = 86_400


# ------------------------------------------------------------------ shares


async def _grantee_refs(principal: Principal) -> list[str]:
    refs = [f"node:{principal.tenant_id}"]
    if principal.user_id is not None:
        refs.append(f"principal:human:{principal.user_id}")
    if not principal.is_human:
        refs += [f"principal:agent:{principal.agent_signature}"]
        async with async_session_factory() as session:
            entry = await session.scalar(
                text("SELECT agent_id FROM agent_directory WHERE :t = ANY(token_ids)"),
                {"t": principal.token_cid or ""})
        if entry:
            refs.append(f"principal:agent:{entry}")
    return refs


async def share(session: AsyncSession, principal: Principal, *, resource_kind: str,
                resource_ref: str, grantee: str, access: str,
                expires_at: Any | None) -> dict[str, Any]:
    if not principal.is_human or principal.user_id is None:
        raise Forbidden("people share resources; agent tokens cannot")
    async with async_session_factory() as platform:
        if await role_on(platform, principal, principal.tenant_id) not in ("owner", "admin"):
            raise Forbidden("only the node's owners and admins share its resources")
        owner = await node(platform, principal, principal.tenant_id)
        resolved = await _resolve_resource(platform, principal.workspace.id, resource_kind,
                                           resource_ref)
        grantee_kind, grantee_ref, grantee_root = await _resolve_grantee(platform, grantee)
        if grantee_root != owner["root_tenant_id"]:
            raise ApiError(422, "share_cross_root_not_allowed",
                           "shares stay inside one tree; across trees use federation",
                           details={"owner_root": owner["root_tenant_id"],
                                    "grantee_root": grantee_root})
        share_id = await platform.scalar(
            text("INSERT INTO tenant_shares (root_tenant_id, workspace_id, resource_kind, "
                 "resource_ref, grantee_kind, grantee_ref, access, expires_at, created_by) "
                 "VALUES (:root, :ws, :kind, :ref, :gkind, :gref, :access, :exp, :by) "
                 "RETURNING id"),
            {"root": owner["root_tenant_id"], "ws": principal.workspace.id,
             "kind": resource_kind, "ref": resolved, "gkind": grantee_kind,
             "gref": grantee_ref, "access": access, "exp": expires_at,
             "by": principal.agent_signature})
        await platform.commit()
    return {"share_id": share_id, "resource_kind": resource_kind, "resource_ref": resolved,
            "grantee": f"{grantee_kind}:{grantee_ref}", "access": access,
            "owner_tenant": owner["alias"]}


async def _resolve_resource(session: AsyncSession, workspace_id: Any, kind: str,
                            ref: str) -> str:
    if kind == "document":
        found = await session.scalar(
            text("SELECT canonical_id FROM shared_documents WHERE workspace_id = :w AND "
                 "(canonical_id = :r OR doc_key = :r)"), {"w": workspace_id, "r": ref})
    elif kind == "task":
        found = await session.scalar(
            text("SELECT canonical_id FROM collab_tasks WHERE workspace_id = :w AND "
                 "(canonical_id = :r OR task_key = :r)"), {"w": workspace_id, "r": ref})
    elif kind == "namespace":
        found = ref
    elif kind == "workspace":
        found = str(workspace_id) if ref in ("current", str(workspace_id)) else None
    else:
        raise ApiError(422, "validation_error", "resource_kind is document, namespace, "
                       "workspace or task", details={"field": "resource_kind"})
    if not found:
        raise NotFound(kind, f"{kind} '{ref}' not found")
    return found


async def _resolve_grantee(session: AsyncSession, grantee: str) -> tuple[str, str, str]:
    """``node:<id|alias>``, ``human:<uuid>`` or ``agent:<agent_id>`` → kind, ref, root."""
    kind, _, ident = grantee.partition(":")
    if kind == "node":
        target = await node(session, None, ident)
        return "node", target["id"], target["root_tenant_id"]
    if kind == "human":
        root = await session.scalar(
            text("SELECT t.root_tenant_id FROM memberships m JOIN tenants t ON "
                 "t.id = m.tenant_id WHERE CAST(m.principal_id AS text) = :u LIMIT 1"),
            {"u": ident})
        if root is None:
            raise NotFound("principal", f"principal '{grantee}' not found")
        return "principal", f"human:{ident}", root
    if kind == "agent":
        root = await session.scalar(
            text("SELECT t.root_tenant_id FROM agent_directory d JOIN tenants t ON "
                 "t.id = d.tenant_id WHERE d.agent_id = :a OR d.principal_id = :a LIMIT 1"),
            {"a": ident})
        if root is None:
            raise NotFound("principal", f"principal '{grantee}' not found")
        return "principal", f"agent:{ident}", root
    raise ApiError(422, "validation_error", "grantee is node:<id>, human:<id> or agent:<id>",
                   details={"field": "grantee"})


async def revoke_share(session: AsyncSession, principal: Principal,
                       share_id: str) -> dict[str, Any]:
    if not principal.is_human:
        raise Forbidden("people revoke shares; agent tokens cannot")
    async with async_session_factory() as platform:
        row = (await platform.execute(
            text("SELECT * FROM tenant_shares WHERE id = :id"), {"id": share_id}
        )).mappings().one_or_none()
        if row is None or await role_on(platform, principal, row["tenant_id"]) not in (
                "owner", "admin"):
            raise NotFound("share", f"share '{share_id}' not found")
        await platform.execute(
            text("UPDATE tenant_shares SET revoked_at = NOW() WHERE id = :id"),
            {"id": share_id})
        await platform.commit()
    return {"share_id": share_id, "revoked": True}


async def list_shares(session: AsyncSession, principal: Principal) -> dict[str, Any]:
    """Shares made by this node and shares granted to the caller or its node."""
    refs = await _grantee_refs(principal)
    async with async_session_factory() as platform:
        rows = (await platform.execute(
            text("SELECT s.*, t.alias AS owner_alias FROM tenant_shares s JOIN tenants t ON "
                 "t.id = s.tenant_id WHERE s.revoked_at IS NULL AND (s.tenant_id = :me OR "
                 "(s.grantee_kind || ':' || s.grantee_ref) = ANY(:refs)) "
                 "ORDER BY s.created_at DESC LIMIT 500"),
            {"me": principal.tenant_id, "refs": refs})).mappings().all()
    return {"items": [{"share_id": row["id"], "owner_tenant": row["owner_alias"],
                       "resource_kind": row["resource_kind"],
                       "resource_ref": row["resource_ref"],
                       "grantee": f"{row['grantee_kind']}:{row['grantee_ref']}",
                       "access": row["access"],
                       "expires_at": row["expires_at"].isoformat() if row["expires_at"]
                       else None, "direction": "outgoing"
                       if row["tenant_id"] == principal.tenant_id else "incoming"}
                      for row in rows]}


async def _grants(platform: AsyncSession, principal: Principal) -> list[dict[str, Any]]:
    refs = await _grantee_refs(principal)
    rows = (await platform.execute(
        text("SELECT s.*, t.alias AS owner_alias FROM tenant_shares s JOIN tenants t ON "
             "t.id = s.tenant_id WHERE s.revoked_at IS NULL AND (s.expires_at IS NULL OR "
             "s.expires_at > NOW()) AND (s.grantee_kind || ':' || s.grantee_ref) = ANY(:refs) "
             "AND s.root_tenant_id = (SELECT root_tenant_id FROM tenants WHERE id = :me)"),
        {"refs": refs, "me": principal.tenant_id})).mappings().all()
    return [dict(row) for row in rows]


def _covers(grant: dict[str, Any], document: SharedDocument) -> bool:
    if str(grant["workspace_id"]) != str(document.workspace_id):
        return False
    kind = grant["resource_kind"]
    return (kind == "workspace" or (kind == "document" and grant["resource_ref"]
                                    == document.canonical_id)
            or (kind == "namespace" and grant["resource_ref"] == document.namespace))


async def shared_document(principal: Principal, ref: str, *,
                          write: bool = False) -> tuple[SharedDocument, dict[str, Any]] | None:
    """A document of another node that a share opens to the caller (or None)."""
    async with async_session_factory() as platform:
        grants = await _grants(platform, principal)
        if not grants:
            return None
        workspaces = list({grant["workspace_id"] for grant in grants})
        candidates = (await platform.execute(
            select(SharedDocument).where(
                SharedDocument.workspace_id.in_(workspaces),
                (SharedDocument.canonical_id == ref) | (SharedDocument.doc_key == ref),
                SharedDocument.archived_at.is_(None),
            ))).scalars().all()
        for document in candidates:
            matching = [grant for grant in grants if _covers(grant, document)]
            if not matching:
                continue
            if write and not any(grant["access"] == "write" for grant in matching):
                raise Forbidden("this document is shared with you read-only")
            return document, matching[0]
    return None


async def read_shared(principal: Principal, ref: str) -> Any | None:
    from app.services.common import document_out

    found = await shared_document(principal, ref)
    if found is None:
        return None
    document, grant = found
    out = document_out(document)
    return out.model_copy(update={"tenant_alias": grant["owner_alias"]})


async def write_shared(principal: Principal, ref: str, request: Any) -> Any | None:
    """Write a document shared with write access, in its own workspace; the
    lineage stamp stays the grantee's (AG-ID-12 point 3)."""
    from app.services.documents import upsert_document

    found = await shared_document(principal, ref, write=True)
    if found is None:
        return None
    document, grant = found
    async with async_session_factory() as platform:
        owner_ws = await platform.get(Workspace, document.workspace_id)
        acting = dataclasses.replace(principal, workspace=owner_ws,
                                     allowed_namespaces=[document.namespace],
                                     write_namespaces=[document.namespace])
        result = await upsert_document(platform, acting, document.doc_key, request)
    return result


async def alias_of(tenant_id: str) -> str | None:
    async with async_session_factory() as platform:
        return await platform.scalar(text("SELECT alias FROM tenants WHERE id = :t"),
                                     {"t": tenant_id})


async def shared_hits(principal: Principal, query: str | None,
                      limit: int) -> list[tuple[SharedDocument, str]]:
    """Documents shared with the caller that match a query (``include_shared``)."""
    async with async_session_factory() as platform:
        grants = await _grants(platform, principal)
        if not grants:
            return []
        workspaces = list({grant["workspace_id"] for grant in grants})
        statement = select(SharedDocument).where(
            SharedDocument.workspace_id.in_(workspaces),
            SharedDocument.archived_at.is_(None))
        if query:
            needle = f"%{query.strip()[:200]}%"
            statement = statement.where(SharedDocument.content.ilike(needle)
                                        | SharedDocument.title.ilike(needle))
        rows = (await platform.execute(statement.limit(limit * 5))).scalars().all()
    hits = []
    for document in rows:
        matching = [grant for grant in grants if _covers(grant, document)]
        if matching:
            hits.append((document, matching[0]["owner_alias"]))
    return hits[:limit]


# ------------------------------------------------------------------ admin access


async def open_admin_access(session: AsyncSession, principal: Principal, node_ref: str, *,
                            reason: str, duration_s: int) -> dict[str, Any]:
    if not principal.is_human or principal.user_id is None:
        raise Forbidden("admin access is for people")
    if not 1 <= duration_s <= ADMIN_ACCESS_MAX_SECONDS:
        raise ApiError(422, "validation_error", "duration_s is 1-86400",
                       details={"field": "duration_s", "limit": ADMIN_ACCESS_MAX_SECONDS})
    async with async_session_factory() as platform:
        target = await node(platform, principal, node_ref)
        role = await role_on(platform, principal, target["id"])
        if role not in ("owner", "admin"):
            raise NotFound("tenant", f"tenant '{node_ref}' not found")
        session_id = await platform.scalar(
            text("INSERT INTO admin_access_sessions (tenant_id, opened_by, opened_by_label, "
                 "reason, expires_at) VALUES (:t, :u, :label, :reason, :exp) RETURNING id"),
            {"t": target["id"], "u": principal.user_id, "label": principal.agent_signature,
             "reason": reason, "exp": utcnow() + timedelta(seconds=duration_s)})
        await _announce(platform, target, principal, session_id, reason)
        await platform.commit()
    return {"access_session_id": session_id, "tenant_id": target["id"],
            "expires_at": (utcnow() + timedelta(seconds=duration_s)).isoformat()}


async def _announce(session: AsyncSession, target: dict[str, Any], principal: Principal,
                    session_id: str, reason: str) -> None:
    """Tell the node's owners (and a managed personal space's person) right away."""
    owners = (await session.execute(
        text("SELECT DISTINCT m.principal_id FROM memberships m JOIN tenants t ON "
             "t.id = m.tenant_id WHERE :n = ANY(t.path) AND (m.role = 'owner' OR "
             "t.tenant_type = 'personal') AND (m.expires_at IS NULL OR m.expires_at > NOW())"),
        {"n": target["id"]})).scalars().all()
    targets = [f"human:{owner}" for owner in owners if owner != principal.user_id]
    workspaces = (await session.execute(
        text("SELECT w.id FROM workspaces w JOIN tenants t ON t.id = w.tenant_id "
             "WHERE :n = ANY(t.path)"), {"n": target["id"]})).scalars().all()
    for workspace_id in workspaces:
        await append_event(session, workspace_id=workspace_id, agent=principal.agent_signature,
                           event_type="TENANT_ADMIN_ACCESS",
                           payload={"targets": targets,
                                    "details": {"access_session_id": session_id,
                                                "opened_by": principal.agent_signature,
                                                "reason": reason, "tenant_id": target["id"]}})
        await session.execute(
            text("INSERT INTO access_audit (workspace_id, action, details) VALUES (:w, "
                 "'admin_access.open', CAST(:d AS jsonb))"),
            {"w": workspace_id, "d": json.dumps({"access_session_id": session_id,
                                                 "by": principal.agent_signature,
                                                 "reason": reason})})


async def close_admin_access(session: AsyncSession, principal: Principal,
                             access_session_id: str) -> dict[str, Any]:
    async with async_session_factory() as platform:
        closed = await platform.scalar(
            text("UPDATE admin_access_sessions SET closed_at = NOW() WHERE id = :id AND "
                 "opened_by = :u AND closed_at IS NULL RETURNING id"),
            {"id": access_session_id, "u": principal.user_id})
        await platform.commit()
    if closed is None:
        raise NotFound("access_session", f"no open access session '{access_session_id}'")
    return {"access_session_id": access_session_id, "closed": True}


async def require_content_access(principal: Principal, what: str) -> None:
    """Ancestor owners/admins read content only inside an open access session."""
    if not principal.inherited_access:
        return
    async with async_session_factory() as platform:
        session_id = await platform.scalar(
            text("UPDATE admin_access_sessions SET reads = reads + 1 WHERE id = (SELECT a.id "
                 "FROM admin_access_sessions a JOIN tenants t ON t.id = :n WHERE "
                 "a.opened_by = :u AND a.closed_at IS NULL AND a.expires_at > NOW() AND "
                 "a.tenant_id = ANY(t.path) ORDER BY a.opened_at DESC LIMIT 1) RETURNING id"),
            {"n": principal.tenant_id, "u": principal.user_id})
        if session_id is None:
            raise ApiError(409, "admin_access_required",
                           "reading this node's content needs an open admin access session",
                           details={"open_with": "open_admin_access",
                                    "tenant_id": principal.tenant_id})
        await platform.execute(
            text("INSERT INTO access_audit (workspace_id, action, details) VALUES (:w, "
                 "'admin_access.read', CAST(:d AS jsonb))"),
            {"w": principal.workspace.id,
             "d": json.dumps({"access_session_id": session_id, "what": what,
                              "by": principal.agent_signature})})
        await platform.commit()
