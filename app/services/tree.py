"""The tenant tree in use (PRD AG-ID-09, AG-ID-11).

Structure: child nodes, the visible tree, moving a subtree inside its tree
and attaching an independent root, with confirmations from the owners on
both sides (409 ``confirmation_required`` until every one has confirmed).
Ids and aliases never change; the database re-derives path, root and depth
for the whole subtree in the same statement.

Inheritance: settings that only ever tighten down the tree (the effective
value is the strictest along the path; an ancestor may lock a key), quota
pools (a node's cap may not exceed its parent's; usage counts against every
ancestor), and usage reports by node. People only: agent tokens get 403.
"""

from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.database import async_session_factory
from app.errors import ApiError, Forbidden, NotFound
from app.services import members

TAINT_MODES = ("annotate", "separate", "exclude")
# key -> (default, kind); for numbers smaller is stricter, for booleans False is.
POLICIES: dict[str, tuple[Any, str]] = {
    "token_max_ttl_seconds": (None, "max"),
    "max_issue_depth": (None, "max"),
    "external_agents_allowed": (True, "allow"),
    "taint_mode_min": ("annotate", "taint"),
    "agent_export_default": (False, "allow"),
    "guests_allowed": (True, "allow"),
    "members_can_issue": (True, "allow"),
    "personal_space_export": (True, "allow"),
}
_TREE_ERRORS = ("tenant_type_invalid_parent", "tenant_cycle", "tenant_depth_exceeded")
_CACHE_SECONDS = 5.0
_effective_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _people_only(principal: Principal) -> None:
    if not principal.is_human or principal.user_id is None:
        raise Forbidden("structure and policies are managed by people, not agent tokens")


async def node(session: AsyncSession, principal: Principal | None, ref: str) -> dict[str, Any]:
    tenant_ref = principal.tenant_id if principal is not None and ref in ("current", "self") \
        else ref
    row = (await session.execute(
        text("SELECT id, alias, display_name, tenant_type, parent_tenant_id, root_tenant_id, "
             "path, depth, status, billing_owner_tenant_id, billing_account FROM tenants "
             "WHERE id = :r OR alias = :r"), {"r": tenant_ref})).mappings().one_or_none()
    if row is None or row["status"] == "deleted":
        raise NotFound("tenant", f"tenant '{ref}' not found")
    return dict(row)


async def role_on(session: AsyncSession, principal: Principal, tenant_id: str) -> str | None:
    if principal.user_id is None:
        return None
    found = await members.effective_role(session, principal.user_id, tenant_id)
    return found["role"] if found else None


async def _require(session: AsyncSession, principal: Principal, tenant_id: str,
                   roles: tuple[str, ...] = ("owner", "admin")) -> str:
    _people_only(principal)
    role = await role_on(session, principal, tenant_id)
    if role not in roles:
        raise Forbidden(f"this needs an {' or '.join(roles)} of the node (or an ancestor)")
    return role


def _tree_error(exc: DBAPIError) -> ApiError:
    message = str(getattr(exc, "orig", exc))
    code = next((item for item in _TREE_ERRORS if item in message), "validation_error")
    details: dict[str, Any] = {"reason": message.splitlines()[0][:200]}
    if code == "tenant_depth_exceeded":
        details["max_depth"] = 3
    return ApiError(422, code, "the tenant tree refused this change", details=details)


# ------------------------------------------------------------------ structure


async def create_child(session: AsyncSession, principal: Principal, parent_ref: str, *,
                       tenant_type: str, display_name: str, alias: str | None,
                       workspace_name: str | None) -> dict[str, Any]:
    from app.services.tenants import create_child_tenant

    _people_only(principal)
    async with async_session_factory() as platform:
        parent = await node(platform, principal, parent_ref)
        await _require(platform, principal, parent["id"])
        result = await create_child_tenant(
            platform, parent["id"], display_name=display_name, tenant_type=tenant_type,
            alias=alias, workspace_name=workspace_name)
        await platform.commit()
    return result


async def get_tree(session: AsyncSession, principal: Principal, ref: str,
                   depth: int | None) -> dict[str, Any]:
    """The subtree under ``ref`` as far as the caller may see it (AG-ID-12 point 6)."""
    async with async_session_factory() as platform:
        top = await node(platform, principal, ref)
        rows = (await platform.execute(
            text("SELECT id, alias, display_name, tenant_type, parent_tenant_id, path, depth, "
                 "status FROM tenants WHERE :top = ANY(path) AND status <> 'deleted' "
                 "ORDER BY depth, alias"), {"top": top["id"]})).mappings().all()
        visible: dict[str, dict[str, Any]] = {}
        for row in rows:
            if depth is not None and row["depth"] - top["depth"] > depth:
                continue
            access = await _visibility(platform, principal, dict(row))
            if access is None:
                continue
            visible[row["id"]] = {"id": row["id"], "alias": row["alias"],
                                  "display_name": row["display_name"],
                                  "tenant_type": row["tenant_type"],
                                  "depth": row["depth"], "path": list(row["path"]),
                                  "status": row["status"], "your_role": access,
                                  "children": []}
            if access in ("member", "named"):
                visible[row["id"]].pop("path")
    roots = []
    for item in visible.values():
        parent = item["path"][-2] if item.get("path") and len(item["path"]) > 1 else None
        if parent in visible:
            visible[parent]["children"].append(item)
        else:
            roots.append(item)
    return {"root": roots[0] if len(roots) == 1 else None, "nodes": roots}


async def _visibility(session: AsyncSession, principal: Principal,
                      row: dict[str, Any]) -> str | None:
    if principal.is_human and principal.user_id is not None:
        role = await role_on(session, principal, row["id"])
        if role in ("owner", "admin", "member"):
            return role
        # Members see the names of their node's children.
        parent = row["path"][-2] if len(row["path"]) > 1 else None
        if parent and await role_on(session, principal, parent) == "member":
            return "named"
        shared = await session.scalar(
            text("SELECT 1 FROM tenant_shares WHERE tenant_id = :t AND revoked_at IS NULL "
                 "AND (expires_at IS NULL OR expires_at > NOW()) AND grantee_kind = "
                 "'principal' AND grantee_ref = :p"),
            {"t": row["id"], "p": f"human:{principal.user_id}"})
        return "shared" if shared else None
    return "agent" if row["id"] == principal.tenant_id else None


async def _confirm(session: AsyncSession, principal: Principal, *, action: str,
                   node_id: str, target_id: str | None, required: list[str],
                   confirmation_id: str | None) -> bool:
    """Record the caller's confirmations; True once every required owner confirmed."""
    owned = [tenant for tenant in required if await role_on(session, principal, tenant)
             == "owner"]
    if not owned:
        raise Forbidden("this needs an owner of one of the affected nodes",)
    row = None
    if confirmation_id:
        row = (await session.execute(
            text("SELECT * FROM tenant_confirmations WHERE id = :id AND action = :a AND "
                 "node_id = :n AND target_id IS NOT DISTINCT FROM :t AND done_at IS NULL "
                 "AND expires_at > NOW() FOR UPDATE"),
            {"id": confirmation_id, "a": action, "n": node_id, "t": target_id}
        )).mappings().one_or_none()
        if row is None:
            raise NotFound("confirmation", f"no pending confirmation '{confirmation_id}'")
    if row is None:
        row = (await session.execute(
            text("INSERT INTO tenant_confirmations (action, node_id, target_id, required, "
                 "requested_by) VALUES (:a, :n, :t, :r, :by) RETURNING *"),
            {"a": action, "n": node_id, "t": target_id, "r": required,
             "by": principal.agent_signature})).mappings().one()
    confirmed = sorted(set(row["confirmed"] or []) | set(owned))
    pending = [tenant for tenant in row["required"] if tenant not in confirmed]
    await session.execute(
        text("UPDATE tenant_confirmations SET confirmed = :c, done_at = CASE WHEN :done "
             "THEN NOW() END WHERE id = :id"),
        {"c": confirmed, "done": not pending, "id": row["id"]})
    if pending:
        await session.commit()
        raise ApiError(409, "confirmation_required",
                       "the owners of the other affected nodes must confirm",
                       details={"confirmation_id": row["id"],
                                "pending_confirmations": pending,
                                "confirm_with": {"confirmation_id": row["id"]}})
    return True


async def _apply_parent(session: AsyncSession, node_id: str, parent_id: str | None) -> None:
    try:
        async with session.begin_nested():
            await session.execute(
                text("UPDATE tenants SET parent_tenant_id = :p WHERE id = :n"),
                {"p": parent_id, "n": node_id})
    except DBAPIError as exc:
        raise _tree_error(exc) from exc


async def _emit(session: AsyncSession, subtree_of: str, event_type: str,
                details: dict[str, Any], targets: list[str] | None = None) -> None:
    from app.services.common import append_event

    workspaces = (await session.execute(
        text("SELECT w.id FROM workspaces w JOIN tenants t ON t.id = w.tenant_id "
             "WHERE :n = ANY(t.path)"), {"n": subtree_of})).scalars().all()
    for workspace_id in workspaces:
        await append_event(session, workspace_id=workspace_id, agent="tenant_tree",
                           event_type=event_type,
                           payload={"details": details, "targets": targets or []})


async def move(session: AsyncSession, principal: Principal, node_ref: str, new_parent_ref: str,
               *, confirmation_id: str | None) -> dict[str, Any]:
    _people_only(principal)
    async with async_session_factory() as platform:
        moving = await node(platform, principal, node_ref)
        target = await node(platform, principal, new_parent_ref)
        if moving["parent_tenant_id"] is None:
            raise ApiError(422, "tenant_type_invalid_parent",
                           "a root moves into a tree with attach_tenant",
                           details={"use": "attach_tenant"})
        if target["root_tenant_id"] != moving["root_tenant_id"]:
            raise ApiError(422, "tenant_type_invalid_parent",
                           "move_tenant stays inside one tree",
                           details={"use": "attach_tenant / detach_tenant"})
        if moving["id"] in target["path"]:
            raise ApiError(422, "tenant_cycle", "a node cannot move under itself")
        required = list(dict.fromkeys([moving["parent_tenant_id"], target["id"]]))
        await _confirm(platform, principal, action="move", node_id=moving["id"],
                       target_id=target["id"], required=required,
                       confirmation_id=confirmation_id)
        await _apply_parent(platform, moving["id"], target["id"])
        await _emit(platform, moving["id"], "TENANT_MOVED",
                    {"tenant_id": moving["id"], "from": moving["parent_tenant_id"],
                     "to": target["id"]})
        await platform.commit()
        forget_policies()
        return await node(platform, principal, moving["id"])


async def attach(session: AsyncSession, principal: Principal, root_ref: str, parent_ref: str,
                 *, confirmation_id: str | None) -> dict[str, Any]:
    _people_only(principal)
    async with async_session_factory() as platform:
        root = await node(platform, principal, root_ref)
        parent = await node(platform, principal, parent_ref)
        if root["parent_tenant_id"] is not None:
            raise ApiError(422, "tenant_type_invalid_parent", "only an independent root attaches",
                           details={"use": "move_tenant"})
        if root["tenant_type"] == "company":
            raise ApiError(422, "tenant_type_invalid_parent", "a company is always a root",
                           details={"child_type": "company", "parent_type":
                                    parent["tenant_type"], "allowed": ["organization",
                                                                       "personal"]})
        if parent["root_tenant_id"] == root["id"]:
            raise ApiError(422, "tenant_cycle", "a tree cannot attach under itself")
        required = list(dict.fromkeys([root["id"], parent["id"]]))
        await _confirm(platform, principal, action="attach", node_id=root["id"],
                       target_id=parent["id"], required=required,
                       confirmation_id=confirmation_id)
        await _apply_parent(platform, root["id"], parent["id"])
        await _emit(platform, root["id"], "TENANT_ATTACHED",
                    {"tenant_id": root["id"], "parent": parent["id"]})
        await platform.commit()
        forget_policies()
        return await node(platform, principal, root["id"])


# ------------------------------------------------------------------ policies


def forget_policies() -> None:
    _effective_cache.clear()


def _stricter(kind: str, first: Any, second: Any) -> Any:
    if first is None:
        return second
    if second is None:
        return first
    if kind == "max":
        return min(first, second)
    if kind == "allow":
        return bool(first) and bool(second)
    if kind == "taint":
        return max(first, second, key=TAINT_MODES.index)
    return second


def _looser(kind: str, value: Any, than: Any) -> bool:
    return than is not None and _stricter(kind, value, than) != value


def _validate(key: str, value: Any) -> Any:
    if key not in POLICIES:
        raise ApiError(422, "validation_error", f"unknown policy '{key}'",
                       details={"field": "key", "allowed": sorted(POLICIES)})
    kind = POLICIES[key][1]
    ok = (value is None or (kind == "max" and isinstance(value, int) and not isinstance(
        value, bool) and value > 0) or (kind == "allow" and isinstance(value, bool))
        or (kind == "taint" and value in TAINT_MODES))
    if not ok:
        raise ApiError(422, "validation_error", f"invalid value for {key}",
                       details={"field": "value", "key": key})
    return value


async def effective(session: AsyncSession, tenant_id: str) -> dict[str, Any]:
    """{key: {value, source_node, locked}}: the strictest value along the path."""
    cached = _effective_cache.get(tenant_id)
    if cached and time.monotonic() - cached[0] < _CACHE_SECONDS:
        return cached[1]
    path = await session.scalar(text("SELECT path FROM tenants WHERE id = :t"),
                                {"t": tenant_id}) or [tenant_id]
    rows = (await session.execute(
        text("SELECT p.tenant_id, p.key, p.value, p.locked, t.alias FROM tenant_policies p "
             "JOIN tenants t ON t.id = p.tenant_id WHERE p.tenant_id = ANY(:path)"),
        {"path": list(path)})).mappings().all()
    by_node: dict[str, dict[str, Any]] = {}
    for row in rows:
        by_node.setdefault(row["tenant_id"], {})[row["key"]] = row
    result: dict[str, Any] = {}
    for key, (default, kind) in POLICIES.items():
        value, source, locked = default, "default", False
        for node_id in path:
            row = by_node.get(node_id, {}).get(key)
            if row is None:
                continue
            stricter = _stricter(kind, value, row["value"])
            if stricter != value or source == "default":
                value, source = stricter, row["alias"]
            locked = locked or row["locked"]
        result[key] = {"value": value, "source_node": source, "locked": locked}
    _effective_cache[tenant_id] = (time.monotonic(), result)
    return result


async def policy_value(tenant_id: str, key: str) -> Any:
    async with async_session_factory() as session:
        return (await effective(session, tenant_id))[key]["value"]


async def set_policy(session: AsyncSession, principal: Principal, node_ref: str, key: str,
                     value: Any, locked: bool) -> dict[str, Any]:
    value = _validate(key, value)
    async with async_session_factory() as platform:
        target = await node(platform, principal, node_ref)
        await _require(platform, principal, target["id"])
        kind = POLICIES[key][1]
        ancestors = list(target["path"][:-1])
        if ancestors:
            parent_effective = await effective(platform, ancestors[-1])
            inherited = parent_effective[key]
            if inherited["locked"] and value != inherited["value"]:
                raise ApiError(422, "policy_loosening_not_allowed",
                               "an ancestor locked this policy",
                               details={"policy": key, "ancestor": inherited["source_node"],
                                        "ancestor_value": inherited["value"], "locked": True})
            if inherited["source_node"] != "default" and (
                    value is None or _looser(kind, value, inherited["value"])):
                raise ApiError(422, "policy_loosening_not_allowed",
                               "a node can only tighten what its ancestors set",
                               details={"policy": key, "ancestor": inherited["source_node"],
                                        "ancestor_value": inherited["value"]})
        await platform.execute(
            text("INSERT INTO tenant_policies (tenant_id, key, value, locked, updated_by) "
                 "VALUES (:t, :k, CAST(:v AS jsonb), :l, :by) ON CONFLICT (tenant_id, key) "
                 "DO UPDATE SET value = EXCLUDED.value, locked = EXCLUDED.locked, "
                 "updated_by = EXCLUDED.updated_by, updated_at = NOW()"),
            {"t": target["id"], "k": key, "v": json.dumps(value), "l": locked,
             "by": principal.agent_signature})
        await platform.commit()
        forget_policies()
        return {"tenant_id": target["id"], "policy": await effective(platform, target["id"])}


async def get_effective_policy(session: AsyncSession, principal: Principal,
                               node_ref: str) -> dict[str, Any]:
    async with async_session_factory() as platform:
        target = await node(platform, principal, node_ref)
        if principal.is_human:
            await _require(platform, principal, target["id"],
                           ("owner", "admin", "member", "guest"))
        elif target["id"] != principal.tenant_id:
            raise NotFound("tenant", f"tenant '{node_ref}' not found")
        return {"tenant_id": target["id"], "alias": target["alias"],
                "policy": await effective(platform, target["id"])}


# ------------------------------------------------------------------ quotas and usage


async def set_quota(session: AsyncSession, principal: Principal, node_ref: str,
                    dimension: str, limit: int | None) -> dict[str, Any]:
    from app.services import budget

    if dimension != "embedding_tokens":
        raise ApiError(422, "validation_error", "node quotas cover embedding_tokens per day",
                       details={"field": "dimension", "allowed": ["embedding_tokens"]})
    async with async_session_factory() as platform:
        target = await node(platform, principal, node_ref)
        await _require(platform, principal, target["id"])
        if limit is not None:
            for ancestor in reversed(target["path"][:-1]):
                parent_limit = await platform.scalar(
                    text("SELECT embedding_tokens_per_day FROM budget_limits WHERE "
                         "scope_kind = 'tenant' AND scope_id = :t"), {"t": ancestor})
                if parent_limit is not None:
                    if limit > parent_limit:
                        raise ApiError(422, "quota_exceeds_parent",
                                       "a node's quota cannot exceed its parent's",
                                       details={"parent_limit": int(parent_limit),
                                                "parent": ancestor, "limit": limit})
                    break
    return await budget.set_limits("tenant", target["id"],
                                   {"embedding_tokens_per_day": limit},
                                   updated_by=principal.agent_signature)


async def usage_by_node(session: AsyncSession, principal: Principal, node_ref: str, *,
                        include_subtree: bool, window_days: int = 1) -> dict[str, Any]:
    async with async_session_factory() as platform:
        target = await node(platform, principal, node_ref)
        await _require(platform, principal, target["id"])
        rows = (await platform.execute(
            text("SELECT t.id, t.alias, t.path, u.category, COALESCE(sum(u.amount), 0) AS amount "
                 "FROM tenants t LEFT JOIN usage_ledger u ON u.tenant_id = t.id AND "
                 "u.created_at >= date_trunc('day', NOW()) - make_interval(days => :d - 1) "
                 "WHERE (CAST(:subtree AS boolean) AND :n = ANY(t.path)) OR t.id = :n "
                 "GROUP BY t.id, t.alias, t.path, u.category"),
            {"n": target["id"], "subtree": include_subtree, "d": window_days})).mappings().all()
    nodes: dict[str, dict[str, Any]] = {}
    totals: dict[str, int] = {}
    for row in rows:
        entry = nodes.setdefault(row["id"], {"tenant_id": row["id"], "alias": row["alias"],
                                             "path": list(row["path"]), "usage": {}})
        if row["category"]:
            entry["usage"][row["category"]] = int(row["amount"])
            totals[row["category"]] = totals.get(row["category"], 0) + int(row["amount"])
    return {"tenant_id": target["id"], "window_days": window_days,
            "include_subtree": include_subtree, "nodes": list(nodes.values()), "total": totals}


# ------------------------------------------------------------------ constitution


async def inherited_constitution(tenant_id: str) -> list[dict[str, Any]]:
    """Ancestors' constitution sections, nearest last; ``locked_sections`` in an
    ancestor constitution's frontmatter cannot be overridden below it."""
    from app.engine.chunking import extract_section
    from app.services.contention import list_sections
    from app.services.onboarding import CONSTITUTION_KEY

    async with async_session_factory() as session:
        path = await session.scalar(text("SELECT path FROM tenants WHERE id = :t"),
                                    {"t": tenant_id}) or []
        rules: list[dict[str, Any]] = []
        for ancestor in list(path)[:-1]:
            row = (await session.execute(
                text("SELECT d.content, d.frontmatter, t.alias FROM shared_documents d "
                     "JOIN workspaces w ON w.id = d.workspace_id JOIN tenants t ON "
                     "t.id = w.tenant_id WHERE w.tenant_id = :t AND d.doc_key = :k "
                     "ORDER BY w.created_at LIMIT 1"),
                {"t": ancestor, "k": CONSTITUTION_KEY})).mappings().one_or_none()
            if row is None or (row["frontmatter"] or {}).get("stub"):
                continue
            locked = set((row["frontmatter"] or {}).get("locked_sections") or [])
            for section in list_sections(row["content"])[:20]:
                if section["level"] != 2:
                    continue
                rules.append({"section": section["heading"],
                              "text": extract_section(row["content"], section["heading"]),
                              "from": row["alias"], "locked": section["heading"] in locked})
    return rules


async def check_constitution(tenant_id: str, content: str) -> None:
    """A node's constitution may not rewrite a section an ancestor locked."""
    from app.engine.chunking import extract_section

    for rule in await inherited_constitution(tenant_id):
        if not rule["locked"]:
            continue
        mine = extract_section(content, rule["section"])
        if mine is not None and mine.strip() != (rule["text"] or "").strip():
            raise ApiError(422, "policy_loosening_not_allowed",
                           "an ancestor locked this constitution section",
                           details={"policy": f"constitution:{rule['section']}",
                                    "ancestor": rule["from"], "ancestor_value": rule["text"]})


# ------------------------------------------------------------------ lifecycle (AG-ID-13)


async def _admin_principal(session: AsyncSession, tenant_id: str) -> Principal | None:
    from app.auth import principal_for_admin
    from app.models import Workspace

    workspace_id = await session.scalar(
        text("SELECT id FROM workspaces WHERE tenant_id = :t ORDER BY created_at LIMIT 1"),
        {"t": tenant_id})
    if workspace_id is None:
        return None
    return principal_for_admin(await session.get(Workspace, workspace_id))


async def _subtree_export(session: AsyncSession, tenant_id: str, *, deliver_to: list[str],
                          requested_by: str) -> str | None:
    from app.services.exports import request_export

    admin = await _admin_principal(session, tenant_id)
    if admin is None:
        return None
    export = await request_export(admin, scope="tenant", scope_ref=tenant_id,
                                  include_subtree=True, purpose="final",
                                  requested_by=requested_by, deliver_to=deliver_to)
    return export["export_id"]


async def delete_node(session: AsyncSession, principal: Principal, node_ref: str, *,
                      confirm: str, cascade: bool) -> dict[str, Any]:
    """Delete a node below the root (with its subtree when ``cascade``): 30 days'
    grace, a final export for the node's and the root's owners, then a tombstone.
    A root keeps the two-party deletion of AG-ID-07."""
    from app.config import get_settings

    _people_only(principal)
    async with async_session_factory() as platform:
        target = await node(platform, principal, node_ref)
        if await role_on(platform, principal, target["id"]) != "owner":
            raise Forbidden("deleting a node needs an owner of it or of an ancestor")
        if target["parent_tenant_id"] is None:
            raise ApiError(409, "confirmation_required",
                           "a root is deleted by its owner and the operator",
                           details={"pending_confirmations": ["owner", "operator"],
                                    "use": "POST /v1/human/tenant/delete"})
        if confirm != target["alias"]:
            raise ApiError(409, "workspace_confirmation_mismatch",
                           "confirm must exactly match the node's alias",
                           details={"tenant": target["alias"]})
        children = await platform.scalar(
            text("SELECT count(*) FROM tenants WHERE parent_tenant_id = :n AND "
                 "status <> 'deleted'"), {"n": target["id"]})
        if children and not cascade:
            raise ApiError(409, "tenant_has_children",
                           "the node has children; pass cascade=true to delete them too",
                           details={"children": int(children)})
        grace = get_settings().tenant_deletion_grace_days
        affected = (await platform.execute(
            text("UPDATE tenants SET status = 'deleting', status_reason = :why, "
                 "status_changed_at = NOW(), deletion_owner_confirmed_at = NOW(), "
                 "deletion_owner_confirmed_by = :by, purge_after = NOW() + make_interval("
                 "days => :days) WHERE :n = ANY(path) AND status IN ('active', 'suspended') "
                 "RETURNING id"),
            {"why": "node deletion confirmed by an owner", "by": principal.agent_signature,
             "days": grace, "n": target["id"]})).scalars().all()
        await platform.commit()
        export_id = await _subtree_export(platform, target["id"],
                                          deliver_to=[target["root_tenant_id"]],
                                          requested_by="platform")
        await platform.execute(text("UPDATE tenants SET final_export_id = :e WHERE id = :n"),
                               {"e": export_id, "n": target["id"]})
        await platform.commit()
    return {"tenant_id": target["id"], "status": "deleting", "nodes": list(affected),
            "final_export_id": export_id, "grace_days": grace}


async def restore_node(session: AsyncSession, principal: Principal,
                       node_ref: str) -> dict[str, Any]:
    _people_only(principal)
    async with async_session_factory() as platform:
        target = await node(platform, principal, node_ref)
        if await role_on(platform, principal, target["id"]) != "owner":
            raise Forbidden("restoring a node needs an owner of it or of an ancestor")
        restored = (await platform.execute(
            text("UPDATE tenants SET status = 'active', status_reason = 'deletion cancelled', "
                 "status_changed_at = NOW(), deletion_owner_confirmed_at = NULL, "
                 "deletion_owner_confirmed_by = NULL, deletion_admin_confirmed_at = NULL, "
                 "purge_after = NULL WHERE :n = ANY(path) AND status = 'deleting' "
                 "RETURNING id"), {"n": target["id"]})).scalars().all()
        await platform.commit()
    return {"tenant_id": target["id"], "restored": list(restored)}


async def detach(session: AsyncSession, principal: Principal, node_ref: str, *,
                 confirmation_id: str | None) -> dict[str, Any]:
    """Make a subtree its own tree; the root's and the node's owners confirm."""
    _people_only(principal)
    async with async_session_factory() as platform:
        target = await node(platform, principal, node_ref)
        if target["parent_tenant_id"] is None:
            raise ApiError(422, "tenant_type_invalid_parent", "the node is already a root")
        old_root = target["root_tenant_id"]
        await _confirm(platform, principal, action="detach", node_id=target["id"],
                       target_id=None, required=[old_root, target["id"]],
                       confirmation_id=confirmation_id)
        # Inherited policies are frozen into the node, so nothing changes at once.
        frozen = await effective(platform, target["id"])
        for key, item in frozen.items():
            if item["source_node"] in ("default", target["alias"]):
                continue
            await platform.execute(
                text("INSERT INTO tenant_policies (tenant_id, key, value, locked, updated_by) "
                     "VALUES (:t, :k, CAST(:v AS jsonb), FALSE, 'detach') ON CONFLICT "
                     "(tenant_id, key) DO UPDATE SET value = EXCLUDED.value"),
                {"t": target["id"], "k": key, "v": json.dumps(item["value"])})
        await _apply_parent(platform, target["id"], None)
        await platform.execute(
            text("UPDATE tenants SET detached_from = :root WHERE id = :n"),
            {"root": old_root, "n": target["id"]})
        await platform.execute(
            text("UPDATE tenant_shares SET revoked_at = NOW() WHERE revoked_at IS NULL AND "
                 "root_tenant_id = :root AND (tenant_id IN (SELECT id FROM tenants WHERE "
                 ":n = ANY(path)) OR (grantee_kind = 'node' AND grantee_ref IN (SELECT id "
                 "FROM tenants WHERE :n = ANY(path))))"),
            {"root": old_root, "n": target["id"]})
        await _emit(platform, target["id"], "TENANT_DETACHED",
                    {"tenant_id": target["id"], "from_root": old_root})
        await platform.commit()
        forget_policies()
        export_id = await _subtree_export(platform, target["id"], deliver_to=[old_root],
                                          requested_by=f"detach:{old_root}")
        return {"tenant": await node(platform, principal, target["id"]),
                "handover_export_id": export_id}
