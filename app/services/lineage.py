"""Lineage stamps, grant snapshots and accountability queries (PRD AG-ID-03).

Every authenticated request computes a lineage stamp: which token wrote, for
which principal, along which chain from the root token, under which grant
snapshot. The stamp is published as the transaction-local setting
``app.lineage_stamp`` and database triggers copy it onto every write, so no
write path can forget it. Stamps never change afterwards.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import PERMISSIONS, AuthKind, Principal, RoleScope
from app.database import async_session_factory
from app.errors import Forbidden, NotFound
from app.models import AgentToken, DocumentVersion, GrantSnapshot, HumanUser, Workspace
from app.stamp_context import current_stamp, set_current_stamp

ADMIN_STAMP: dict[str, Any] = {
    "kind": "admin",
    "principal_id": "admin",
    "token_id": None,
    "chain": ["admin"],
}


def _scopes(role: str) -> list[str]:
    try:
        return sorted(PERMISSIONS.get(RoleScope(role), set()))
    except ValueError:
        return []


def grant_hash(grant: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(grant, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _effective_grant(
    *, role: str, namespaces: list[str], depth: int, budget: dict[str, Any]
) -> dict[str, Any]:
    return {
        "role_scope": role,
        "scopes": _scopes(role),
        "namespaces": sorted(namespaces),
        "depth": depth,
        "can_issue": False,
        "max_descendants": 0,
        "budget_limits": budget,
    }


def _token_budget(token: AgentToken) -> dict[str, Any]:
    return {
        "per_minute_limit": token.per_minute_limit,
        "monthly_token_budget": token.monthly_token_budget,
    }


def _root_grant() -> dict[str, Any]:
    return _effective_grant(role="COMMANDER", namespaces=["*"], depth=0, budget={})


async def _latest_snapshot(session: AsyncSession, token_id: str) -> GrantSnapshot | None:
    return await session.scalar(
        select(GrantSnapshot)
        .where(GrantSnapshot.token_id == token_id)
        .order_by(GrantSnapshot.issued_at.desc(), GrantSnapshot.id.desc())
        .limit(1)
    )


def _new_snapshot(
    *,
    workspace_id,
    token_id: str,
    kind: str,
    parent_token_id: str | None,
    principal_id: str,
    grant: dict[str, Any],
    parent_hash: str | None,
    purpose: str | None,
    ttl_seconds: int | None,
    issued_at: datetime | None,
    issuer_stamp: dict[str, Any] | None,
    backfilled: bool,
) -> GrantSnapshot:
    snapshot = GrantSnapshot(
        workspace_id=workspace_id,
        token_id=token_id,
        token_kind=kind,
        parent_token_id=parent_token_id,
        principal_id=principal_id,
        role_scope=grant["role_scope"],
        scopes=grant["scopes"],
        namespaces=grant["namespaces"],
        depth=grant["depth"],
        can_issue=grant["can_issue"],
        max_descendants=grant["max_descendants"],
        budget_limits=grant["budget_limits"],
        purpose=purpose,
        ttl_seconds=ttl_seconds,
        issuer_stamp=issuer_stamp,
        parent_effective_grant_hash=parent_hash,
        grant_hash=grant_hash(grant),
        backfilled=backfilled,
    )
    if issued_at is not None:
        snapshot.issued_at = issued_at
    return snapshot


async def snapshot_root(
    session: AsyncSession, workspace: Workspace, *, backfilled: bool
) -> GrantSnapshot:
    existing = await _latest_snapshot(session, workspace.root_token_id)
    if existing is not None:
        return existing
    snapshot = _new_snapshot(
        workspace_id=workspace.id,
        token_id=workspace.root_token_id,
        kind="root",
        parent_token_id=None,
        principal_id="workspace_root",
        grant=_root_grant(),
        parent_hash=None,
        purpose="workspace root key",
        ttl_seconds=None,
        issued_at=workspace.created_at if backfilled else None,
        issuer_stamp=ADMIN_STAMP if not backfilled else None,
        backfilled=backfilled,
    )
    session.add(snapshot)
    await session.flush()
    return snapshot


async def snapshot_token(
    session: AsyncSession,
    workspace: Workspace,
    token: AgentToken,
    *,
    backfilled: bool = False,
    issuer_stamp: dict[str, Any] | None = None,
) -> GrantSnapshot:
    """Record the token's grant as issued (or as changed by an update)."""
    root = await snapshot_root(session, workspace, backfilled=True)
    ttl = (
        int((token.expires_at - token.created_at).total_seconds())
        if token.expires_at and token.created_at
        else None
    )
    snapshot = _new_snapshot(
        workspace_id=workspace.id,
        token_id=token.canonical_id,
        kind="branch" if token.depth <= 1 else "child",
        parent_token_id=token.parent_token_id or workspace.root_token_id,
        principal_id=token.agent_signature,
        grant=_effective_grant(
            role=token.role_scope.value,
            namespaces=list(token.allowed_namespaces or ["*"]),
            depth=token.depth,
            budget=_token_budget(token),
        ),
        parent_hash=root.grant_hash,
        purpose=token.purpose,
        ttl_seconds=ttl,
        issued_at=token.created_at if backfilled else None,
        issuer_stamp=issuer_stamp if issuer_stamp is not None else current_stamp(),
        backfilled=backfilled,
    )
    session.add(snapshot)
    await session.flush()
    return snapshot


async def _ensure_snapshot_id(principal: Principal) -> str | None:
    """Latest snapshot for the presenting token; backfills legacy tokens once."""
    workspace = principal.workspace
    async with async_session_factory() as session:
        if principal.auth_kind is AuthKind.WORKSPACE:
            snapshot = await _latest_snapshot(session, workspace.root_token_id)
            if snapshot is None:
                fresh = await session.get(Workspace, workspace.id)
                snapshot = await snapshot_root(session, fresh, backfilled=True)
                await session.commit()
            return snapshot.id
        if principal.auth_kind is AuthKind.AGENT and principal.token_id is not None:
            token = await session.get(AgentToken, principal.token_id)
            if token is None:
                return None
            snapshot = await _latest_snapshot(session, token.canonical_id)
            if snapshot is None:
                fresh = await session.get(Workspace, workspace.id)
                snapshot = await snapshot_token(
                    session, fresh, token, backfilled=True, issuer_stamp=None
                )
                await session.commit()
            return snapshot.id
    return None


async def stamp_for(principal: Principal) -> dict[str, Any]:
    workspace = principal.workspace
    if principal.auth_kind is AuthKind.HUMAN_ADMIN:
        return dict(ADMIN_STAMP)
    if principal.auth_kind is AuthKind.HUMAN:
        async with async_session_factory() as session:
            user = await session.get(HumanUser, principal.user_id)
        principal_id = user.canonical_id if user else str(principal.user_id)
        return {
            "kind": "human",
            "principal_id": principal_id,
            "principal": principal.agent_signature,
            "token_id": None,
            "session_id": str(principal.token_id) if principal.token_id else None,
            "chain": [principal_id],
            "grant_snapshot_id": None,
        }
    snapshot_id = await _ensure_snapshot_id(principal)
    if principal.auth_kind is AuthKind.WORKSPACE:
        return {
            "kind": "root",
            "principal_id": "workspace_root",
            "principal": principal.agent_signature,
            "token_id": workspace.root_token_id,
            "chain": [workspace.root_token_id],
            "grant_snapshot_id": snapshot_id,
        }
    async with async_session_factory() as session:
        token = await session.get(AgentToken, principal.token_id)
    token_cid = token.canonical_id if token else None
    chain = [token.root_token_id or workspace.root_token_id] if token else []
    if token is not None and token.parent_token_id and token.parent_token_id not in chain:
        chain.append(token.parent_token_id)
    if token_cid:
        chain.append(token_cid)
    return {
        "kind": "branch" if (token is None or token.depth <= 1) else "child",
        "principal_id": principal.agent_signature,
        "principal": principal.agent_signature,
        "token_id": token_cid,
        "chain": chain,
        "grant_snapshot_id": snapshot_id,
    }


# --------------------------------------------------------------- queries


def _token_view(token: AgentToken, *, active_descendants: int = 0) -> dict[str, Any]:
    return {
        "token_id": token.canonical_id,
        "kind": "branch" if token.depth <= 1 else "child",
        "depth": token.depth,
        "principal_id": token.agent_signature,
        "scopes": _scopes(token.role_scope.value),
        "role_scope": token.role_scope.value,
        "namespaces": list(token.allowed_namespaces or ["*"]),
        "expires_at": token.expires_at,
        "revoked_at": token.revoked_at,
        "is_active": token.is_active,
        "parent_token_id": token.parent_token_id,
        "root_token_id": token.root_token_id,
        "can_issue": False,
        "max_descendants": 0,
        "active_descendants": active_descendants,
        "budget_state": _token_budget(token),
        "purpose": token.purpose,
        "issued_at": token.created_at,
    }


def _root_view(workspace: Workspace) -> dict[str, Any]:
    return {
        "token_id": workspace.root_token_id,
        "kind": "root",
        "depth": 0,
        "principal_id": "workspace_root",
        "scopes": ["*"],
        "role_scope": "COMMANDER",
        "namespaces": ["*"],
        "expires_at": None,
        "revoked_at": None,
        "is_active": True,
        "parent_token_id": None,
        "root_token_id": workspace.root_token_id,
        "can_issue": True,
        "issued_at": workspace.created_at,
    }


async def _workspace_tokens(session: AsyncSession, workspace_id) -> list[AgentToken]:
    return list(
        (
            await session.execute(
                select(AgentToken).where(AgentToken.workspace_id == workspace_id)
            )
        ).scalars()
    )


def _descendants(tokens: list[AgentToken], token_id: str) -> list[AgentToken]:
    children: dict[str, list[AgentToken]] = {}
    for token in tokens:
        if token.parent_token_id:
            children.setdefault(token.parent_token_id, []).append(token)
    found: list[AgentToken] = []
    frontier = [token_id]
    while frontier:
        current = frontier.pop()
        for child in children.get(current, []):
            found.append(child)
            frontier.append(child.canonical_id)
    return found


async def _self_token_id(session: AsyncSession, principal: Principal) -> str | None:
    if principal.auth_kind is AuthKind.WORKSPACE:
        return principal.workspace.root_token_id
    if principal.auth_kind is AuthKind.AGENT and principal.token_id is not None:
        token = await session.get(AgentToken, principal.token_id)
        return token.canonical_id if token else None
    return None


def _ancestors(tokens: list[AgentToken], workspace: Workspace, token_id: str) -> list[str]:
    by_id = {token.canonical_id: token for token in tokens}
    chain: list[str] = []
    current: str | None = token_id
    while current is not None and current not in chain:
        chain.append(current)
        token = by_id.get(current)
        current = token.parent_token_id if token else None
    if workspace.root_token_id not in chain:
        chain.append(workspace.root_token_id)
    return list(reversed(chain))


async def _visible(
    session: AsyncSession, principal: Principal, tokens: list[AgentToken], target: str
) -> bool:
    """A token sees itself, its ancestors and its own subtree; humans see all."""
    if principal.is_human or principal.auth_kind is AuthKind.WORKSPACE:
        return True
    own = await _self_token_id(session, principal)
    if own is None:
        return False
    if target == own or target in _ancestors(tokens, principal.workspace, own):
        return True
    return target in {token.canonical_id for token in _descendants(tokens, own)}


async def get_token(session: AsyncSession, principal: Principal, ref: str) -> dict[str, Any]:
    tokens = await _workspace_tokens(session, principal.workspace.id)
    target = await _self_token_id(session, principal) if ref == "self" else ref
    if target is None:
        raise NotFound("token", "this credential is a human session, not a token")
    if not await _visible(session, principal, tokens, target):
        raise Forbidden("this token may only inspect itself, its ancestors and its subtree")
    if target == principal.workspace.root_token_id:
        view = _root_view(principal.workspace)
        view["active_descendants"] = sum(1 for token in tokens if token.is_active)
        return view
    token = next((item for item in tokens if item.canonical_id == target), None)
    if token is None:
        raise NotFound("token")
    descendants = _descendants(tokens, token.canonical_id)
    return _token_view(
        token, active_descendants=sum(1 for item in descendants if item.is_active)
    )


async def get_lineage(session: AsyncSession, principal: Principal, ref: str) -> dict[str, Any]:
    tokens = await _workspace_tokens(session, principal.workspace.id)
    target = await _self_token_id(session, principal) if ref == "self" else ref
    if target is None:
        raise NotFound("token")
    if not await _visible(session, principal, tokens, target):
        raise Forbidden("this token may only inspect itself, its ancestors and its subtree")
    by_id = {token.canonical_id: token for token in tokens}
    if target != principal.workspace.root_token_id and target not in by_id:
        raise NotFound("token")
    chain = []
    for token_id in _ancestors(tokens, principal.workspace, target):
        if token_id == principal.workspace.root_token_id:
            chain.append(
                {
                    "token_id": token_id,
                    "kind": "root",
                    "principal_id": "workspace_root",
                    "display_name": principal.workspace.name,
                    "issued_at": principal.workspace.created_at,
                    "expires_at": None,
                    "revoked_at": None,
                }
            )
            continue
        token = by_id[token_id]
        chain.append(
            {
                "token_id": token_id,
                "kind": "branch" if token.depth <= 1 else "child",
                "principal_id": token.agent_signature,
                "display_name": token.agent_signature,
                "issued_at": token.created_at,
                "expires_at": token.expires_at,
                "revoked_at": token.revoked_at,
            }
        )
    return {"chain": chain}


async def list_token_tree(
    session: AsyncSession,
    principal: Principal,
    ref: str,
    *,
    include_inactive: bool = False,
    limit: int = 100,
    cursor: str | None = None,
) -> dict[str, Any]:
    tokens = await _workspace_tokens(session, principal.workspace.id)
    target = await _self_token_id(session, principal) if ref == "self" else ref
    if target is None:
        raise NotFound("token")
    own = await _self_token_id(session, principal)
    allowed = principal.is_human or principal.auth_kind is AuthKind.WORKSPACE
    if not allowed and own is not None:
        # A token may list only its own subtree, never an ancestor's.
        allowed = target == own or target in {
            token.canonical_id for token in _descendants(tokens, own)
        }
    if not allowed:
        raise Forbidden("a token may only list its own subtree")
    items = sorted(_descendants(tokens, target), key=lambda token: token.canonical_id)
    if not include_inactive:
        items = [token for token in items if token.is_active and token.revoked_at is None]
    if cursor:
        items = [token for token in items if token.canonical_id > cursor]
    page = items[: max(1, min(limit, 500))]
    next_cursor = page[-1].canonical_id if len(items) > len(page) else None
    return {"items": [_token_view(token) for token in page], "next_cursor": next_cursor}


async def explain_write(
    session: AsyncSession, principal: Principal, ref: str, version: int | None
) -> dict[str, Any]:
    """Who wrote this version, who issued their token, what they could do then."""
    from app.services.addresses import resolve_document

    document = await resolve_document(session, principal.workspace.id, ref)
    from app.auth import ensure_namespace

    ensure_namespace(principal, document.namespace)
    wanted = version or document.version
    row = await session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document.id,
            DocumentVersion.version == wanted,
        )
    )
    if row is None:
        raise NotFound("document_version", f"version {wanted} not found")
    stamp = row.stamp or {}
    snapshot = None
    if stamp.get("grant_snapshot_id"):
        snapshot = await session.get(GrantSnapshot, stamp["grant_snapshot_id"])
    issued_by = None
    if snapshot is not None and snapshot.parent_token_id:
        issued_by = {
            "token_id": snapshot.parent_token_id,
            "principal": (snapshot.issuer_stamp or {}).get("principal_id")
            or ("workspace_root" if snapshot.parent_token_id == principal.workspace.root_token_id
                else None),
        }
    grant = _snapshot_view(snapshot) if snapshot is not None else None
    return {
        "document": {"id": document.canonical_id, "alias": document.doc_key, "version": wanted},
        "writer": {
            "principal_id": stamp.get("principal_id"),
            "label": stamp.get("principal") or row.author,
            "kind": stamp.get("kind"),
        },
        "token": {"token_id": stamp.get("token_id"), "kind": stamp.get("kind")},
        "chain": stamp.get("chain", []),
        "issued_by": issued_by,
        "grant_at_write": grant,
        "effective_permissions_at_write": grant["scopes"] if grant else None,
        "legacy": bool(stamp.get("legacy")),
        "stamp": stamp,
    }


def _snapshot_view(snapshot: GrantSnapshot) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "token_id": snapshot.token_id,
        "token_kind": snapshot.token_kind,
        "parent_token_id": snapshot.parent_token_id,
        "principal_id": snapshot.principal_id,
        "role_scope": snapshot.role_scope,
        "scopes": list(snapshot.scopes or []),
        "namespaces": list(snapshot.namespaces or []),
        "depth": snapshot.depth,
        "can_issue": snapshot.can_issue,
        "max_descendants": snapshot.max_descendants,
        "budget_limits": snapshot.budget_limits,
        "purpose": snapshot.purpose,
        "ttl_seconds": snapshot.ttl_seconds,
        "issued_at": snapshot.issued_at,
        "issuer_stamp": snapshot.issuer_stamp,
        "parent_effective_grant_hash": snapshot.parent_effective_grant_hash,
        "grant_hash": snapshot.grant_hash,
        "backfilled": snapshot.backfilled,
    }


async def subtree_token_ids(
    session: AsyncSession, principal: Principal, token_id: str
) -> list[str]:
    tokens = await _workspace_tokens(session, principal.workspace.id)
    if not await _visible(session, principal, tokens, token_id):
        raise Forbidden("this token may only inspect itself, its ancestors and its subtree")
    return [token_id, *[token.canonical_id for token in _descendants(tokens, token_id)]]


__all__ = [
    "ADMIN_STAMP",
    "current_stamp",
    "explain_write",
    "get_lineage",
    "get_token",
    "list_token_tree",
    "set_current_stamp",
    "snapshot_root",
    "snapshot_token",
    "stamp_for",
    "subtree_token_ids",
]
