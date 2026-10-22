"""Address resolution, namespaces, references, rename and purge (PRD AG-ST-17).

Every reference parameter accepts a canonical id (``doc_…``, ``task_…``) or an
alias (``doc_key`` / ``task_key``). Renamed aliases resolve through permanent
redirects; purged objects leave tombstones that answer 410 ``gone`` and whose
aliases are never assigned again.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import addresses
from app.auth import Principal, can, require_permission
from app.errors import ApiError, NotFound
from app.models import (
    AgentToken,
    AliasRedirect,
    CollabTask,
    ContextSlot,
    DocumentRef,
    HumanUser,
    NamespaceRecord,
    SharedDocument,
    Tombstone,
    Workspace,
)
from app.services.common import utcnow
from app.versioning import LATEST_VERSION, at_least
from app.warnings_context import add_warning

MAX_EXPANDED_PARTS_BYTES = 4 * 1024 * 1024


# ----------------------------------------------------------------- errors


def gone(tombstone: Tombstone) -> ApiError:
    return ApiError(
        410,
        "gone",
        f"{tombstone.object_type} '{tombstone.alias}' was purged",
        details={
            "id": tombstone.canonical_id,
            "alias": tombstone.alias,
            "type": tombstone.object_type,
            "last_version": tombstone.last_version,
            "last_content_hash": tombstone.last_content_hash,
            "purged_by": tombstone.purged_by,
            "purged_at": tombstone.purged_at.isoformat() if tombstone.purged_at else None,
            "reason": tombstone.reason,
        },
    )


def invalid_address(violation: addresses.AddressViolation) -> ApiError:
    return ApiError(
        422,
        "invalid_address",
        f"{violation.field} '{violation.value}' violates the address rule {violation.rule}",
        details=violation.details(),
    )


# -------------------------------------------------------------- resolution


async def _tombstone(
    session: AsyncSession, workspace_id: uuid.UUID, *, canonical_id: str | None = None,
    alias: str | None = None, object_type: str = "document",
) -> Tombstone | None:
    query = select(Tombstone).where(Tombstone.workspace_id == workspace_id)
    if canonical_id is not None:
        query = query.where(Tombstone.canonical_id == canonical_id)
    else:
        query = query.where(Tombstone.object_type == object_type, Tombstone.alias == alias)
    return await session.scalar(query)


async def _redirect(
    session: AsyncSession, workspace_id: uuid.UUID, object_type: str, alias: str
) -> AliasRedirect | None:
    return await session.scalar(
        select(AliasRedirect).where(
            AliasRedirect.workspace_id == workspace_id,
            AliasRedirect.object_type == object_type,
            AliasRedirect.old_alias == alias,
        )
    )


async def find_document(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    ref: str,
    *,
    for_update: bool = False,
) -> SharedDocument | None:
    """Resolve a document by canonical id, alias or redirect; None if absent.

    Raises 410 ``gone`` for tombstoned references.
    """

    def query(*conditions: Any):
        statement = select(SharedDocument).where(
            SharedDocument.workspace_id == workspace_id, *conditions
        )
        return statement.with_for_update() if for_update else statement

    if addresses.is_canonical(ref, "doc"):
        document = await session.scalar(query(SharedDocument.canonical_id == ref))
        if document is None:
            tombstone = await _tombstone(session, workspace_id, canonical_id=ref)
            if tombstone is not None:
                raise gone(tombstone)
        return _flag_legacy(document)
    document = await session.scalar(query(SharedDocument.doc_key == ref))
    if document is not None:
        return _flag_legacy(document)
    redirect = await _redirect(session, workspace_id, "document", ref)
    if redirect is not None:
        document = await session.scalar(
            query(SharedDocument.canonical_id == redirect.target_canonical_id)
        )
        if document is not None:
            add_warning(
                "alias_redirected",
                f"'{ref}' was renamed; use the current alias or the canonical id",
                current=document.doc_key,
                id=document.canonical_id,
            )
            return _flag_legacy(document)
        tombstone = await _tombstone(
            session, workspace_id, canonical_id=redirect.target_canonical_id
        )
        if tombstone is not None:
            raise gone(tombstone)
    tombstone = await _tombstone(session, workspace_id, alias=ref)
    if tombstone is not None:
        raise gone(tombstone)
    return None


def _flag_legacy(document: SharedDocument | None) -> SharedDocument | None:
    if document is not None and document.alias_legacy:
        add_warning(
            "legacy_alias",
            "this document address predates the address grammar; it keeps working "
            "but new documents must follow it",
            alias=document.doc_key,
            id=document.canonical_id,
        )
    return document


async def resolve_document(
    session: AsyncSession, workspace_id: uuid.UUID, ref: str, *, for_update: bool = False
) -> SharedDocument:
    document = await find_document(session, workspace_id, ref, for_update=for_update)
    if document is None:
        raise NotFound("shared_document", f"document '{ref}' not found")
    return document


async def find_task(
    session: AsyncSession, workspace_id: uuid.UUID, ref: str, *, for_update: bool = False
) -> CollabTask | None:
    def query(*conditions: Any):
        statement = select(CollabTask).where(CollabTask.workspace_id == workspace_id, *conditions)
        return statement.with_for_update() if for_update else statement

    if addresses.is_canonical(ref, "task"):
        return await session.scalar(query(CollabTask.canonical_id == ref))
    task = await session.scalar(query(CollabTask.task_key == ref))
    if task is not None:
        return task
    redirect = await _redirect(session, workspace_id, "task", ref)
    if redirect is None:
        return None
    task = await session.scalar(query(CollabTask.canonical_id == redirect.target_canonical_id))
    if task is not None:
        add_warning(
            "alias_redirected",
            f"task '{ref}' was renamed; use the current key or the canonical id",
            current=task.task_key,
            id=task.canonical_id,
        )
    return task


async def ensure_alias_available(
    session: AsyncSession, workspace_id: uuid.UUID, object_type: str, alias: str
) -> None:
    """New aliases may not reuse a tombstoned or redirected address."""
    tombstone = await _tombstone(session, workspace_id, alias=alias, object_type=object_type)
    if tombstone is not None:
        raise ApiError(
            409,
            "alias_tombstoned",
            f"'{alias}' belonged to a purged {object_type}; tombstoned addresses are never reused",
            details={"alias": alias, "id": tombstone.canonical_id},
        )
    redirect = await _redirect(session, workspace_id, object_type, alias)
    if redirect is not None:
        raise ApiError(
            409,
            "alias_taken",
            f"'{alias}' is a permanent redirect to a renamed {object_type}",
            details={"alias": alias, "id": redirect.target_canonical_id},
        )


def check_new_document_address(
    namespace: str, doc_key: str, *, allow_system: bool = False
) -> bool:
    """Validate a new document address. Returns True when it is a legacy alias.

    Strict from API version 2026-11; the baseline accepts and flags it.
    """
    violation = addresses.namespace_violation(
        namespace, allow_reserved=allow_system and namespace == "system"
    ) or addresses.doc_key_violation(doc_key)
    if violation is None:
        return False
    if violation.rule == "reserved_namespace":
        raise invalid_address(violation)
    if at_least(LATEST_VERSION):
        raise invalid_address(violation)
    add_warning(
        "legacy_alias",
        f"{violation.field} '{violation.value}' breaks the address rule {violation.rule}; "
        f"it is accepted as a legacy alias but API version {LATEST_VERSION} rejects it",
        rule=violation.rule,
        field=violation.field,
    )
    return True


# -------------------------------------------------------------- namespaces


def namespace_out(record: NamespaceRecord) -> dict[str, Any]:
    return {
        "namespace": record.namespace,
        "purpose": record.purpose,
        "owner": record.owner,
        "doc_type_default": record.doc_type_default,
        "write_policy": record.write_policy,
        "auto_registered": record.auto_registered,
        "created_by": record.created_by,
        "created_at": record.created_at,
    }


async def _namespace(
    session: AsyncSession, workspace_id: uuid.UUID, namespace: str
) -> NamespaceRecord | None:
    return await session.scalar(
        select(NamespaceRecord).where(
            NamespaceRecord.workspace_id == workspace_id,
            NamespaceRecord.namespace == namespace,
        )
    )


async def ensure_namespace_registered(
    session: AsyncSession, principal: Principal, namespace: str
) -> None:
    if await _namespace(session, principal.workspace.id, namespace) is not None:
        return
    implicit = namespace == "default"  # every workspace has it
    if at_least(LATEST_VERSION) and not implicit:
        raise ApiError(
            422,
            "namespace_not_registered",
            f"namespace '{namespace}' is not registered; register it, then retry",
            details={
                "namespace": namespace,
                "action": "register_namespace",
                "tool": "register_namespace",
                "rest": "POST /v1/namespaces",
            },
        )
    try:
        # Concurrent first writes to a new namespace race to register it.
        async with session.begin_nested():
            session.add(
                NamespaceRecord(
                    workspace_id=principal.workspace.id,
                    namespace=namespace,
                    purpose=None,
                    owner=principal.agent_signature,
                    created_by=principal.agent_signature,
                    auto_registered=True,
                )
            )
            await session.flush()
    except IntegrityError:
        return  # registered by the request that won the race
    if implicit:
        return
    from app.services.onboarding import ensure_namespace_readme

    await ensure_namespace_readme(
        session, principal.workspace.id, namespace, registered_by=principal.agent_signature
    )
    add_warning(
        "namespace_auto_registered",
        f"namespace '{namespace}' was registered automatically; API version "
        f"{LATEST_VERSION} requires register_namespace first",
        namespace=namespace,
    )


async def register_namespace(
    session: AsyncSession,
    principal: Principal,
    *,
    namespace: str,
    purpose: str | None,
    doc_type_default: str = "collaborative",
    write_policy: str = "open",
) -> dict[str, Any]:
    if not (can(principal, "doc:write") or can(principal, "doc:create")):
        require_permission(principal, "doc:create")
    violation = addresses.namespace_violation(namespace)
    if violation is not None:
        raise invalid_address(violation)
    existing = await _namespace(session, principal.workspace.id, namespace)
    if existing is not None:
        if purpose and not existing.purpose:
            existing.purpose = purpose
            existing.auto_registered = False
            await session.commit()
        return {**namespace_out(existing), "created": False}
    record = NamespaceRecord(
        workspace_id=principal.workspace.id,
        namespace=namespace,
        purpose=purpose,
        owner=principal.agent_signature,
        doc_type_default=doc_type_default,
        write_policy=write_policy,
        created_by=principal.agent_signature,
    )
    session.add(record)
    await session.flush()
    from app.services.onboarding import ensure_namespace_readme, readme_key

    await ensure_namespace_readme(
        session, principal.workspace.id, namespace, registered_by=principal.agent_signature
    )
    await session.commit()
    return {**namespace_out(record), "created": True, "readme_doc_key": readme_key(namespace)}


async def list_namespaces(session: AsyncSession, principal: Principal) -> dict[str, Any]:
    from app.services import agent_onboarding
    from app.services.onboarding import namespace_overview

    if agent_onboarding.tracked(principal) and agent_onboarding.primary_namespace(
            principal) is None:
        # A token without namespaces reads the namespace map instead of a readme.
        await agent_onboarding.observe(principal, "readme_read", namespace=None)

    entries = await namespace_overview(session, principal)
    rows = {
        row.namespace: row
        for row in (
            await session.execute(
                select(NamespaceRecord).where(
                    NamespaceRecord.workspace_id == principal.workspace.id
                )
            )
        ).scalars()
    }
    items = []
    for entry in entries:
        if entry["my_access"] == "none":
            continue
        record = rows[entry["namespace"]]
        items.append(
            {
                **namespace_out(record),
                **entry,
                "readme_doc_key": (entry.get("readme") or {}).get("doc_key"),
            }
        )
    return {"items": items, "reserved": sorted(addresses.RESERVED_NAMESPACES)}


# -------------------------------------------------------------- references

_REF_TABLES: dict[str, Any] = {
    "doc": SharedDocument,
    "task": CollabTask,
    "slot": ContextSlot,
    "tok": AgentToken,
    "prn": HumanUser,
}


async def resolve_ref(
    session: AsyncSession, workspace_id: uuid.UUID, ref: str
) -> str | None:
    """Return the canonical id a ``$ref`` points at, or None when dangling."""
    prefix = addresses.canonical_prefix(ref)
    if prefix == "ws":
        found = await session.scalar(
            select(Workspace.canonical_id).where(
                Workspace.id == workspace_id, Workspace.canonical_id == ref
            )
        )
        return found
    if prefix in _REF_TABLES:
        model = _REF_TABLES[prefix]
        return await session.scalar(
            select(model.canonical_id).where(
                model.workspace_id == workspace_id, model.canonical_id == ref
            )
        )
    try:
        document = await find_document(session, workspace_id, ref)
    except ApiError:
        document = None
    if document is not None:
        return document.canonical_id
    task = await find_task(session, workspace_id, ref)
    return task.canonical_id if task is not None else None


async def resolve_frontmatter_refs(
    session: AsyncSession, workspace_id: uuid.UUID, frontmatter: dict[str, Any]
) -> tuple[dict[str, Any], list[tuple[str, str, str | None]]]:
    """Rewrite {"$ref": X} values to canonical ids; collect (path, ref, target)."""
    edges: list[tuple[str, str, str | None]] = []

    async def walk(value: Any, path: str) -> Any:
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str) and ref.strip():
                target = await resolve_ref(session, workspace_id, ref.strip())
                edges.append((path or "$", ref.strip(), target))
                if target is None:
                    add_warning(
                        "dangling_ref",
                        f"$ref '{ref}' at {path or '$'} does not resolve; stored as written",
                        ref=ref,
                        path=path or "$",
                    )
                    return value
                return {**value, "$ref": target}
            return {key: await walk(item, f"{path}.{key}" if path else key)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [await walk(item, f"{path}[{index}]") for index, item in enumerate(value)]
        return value

    resolved = await walk(frontmatter or {}, "")
    return resolved, edges


def has_refs(value: Any) -> bool:
    if isinstance(value, dict):
        return "$ref" in value or any(has_refs(item) for item in value.values())
    if isinstance(value, list):
        return any(has_refs(item) for item in value)
    return False


async def record_document_refs(
    session: AsyncSession,
    document: SharedDocument,
    edges: list[tuple[str, str, str | None]],
) -> None:
    for path, ref, target in edges:
        session.add(
            DocumentRef(
                workspace_id=document.workspace_id,
                source_document_id=document.id,
                source_version=document.version,
                field_path=path[:512],
                target_canonical_id=target,
                target_ref=ref[:512],
                kind="declared",
            )
        )


# --------------------------------------------------------------- parts


async def expand_parts(
    session: AsyncSession, principal: Principal, document: SharedDocument
) -> tuple[str, list[dict[str, Any]]]:
    """Concatenate a parent's parts in index order (PRD AG-ST-17 point 2)."""
    raw_parts = (document.frontmatter or {}).get("parts")
    if not isinstance(raw_parts, list) or not raw_parts:
        return document.content, []
    entries: list[tuple[int, str]] = []
    for position, item in enumerate(raw_parts):
        if not isinstance(item, dict) or not isinstance(item.get("$ref"), str):
            continue
        index = item.get("index", position)
        entries.append((int(index) if isinstance(index, int) else position, item["$ref"]))
    entries.sort(key=lambda entry: entry[0])
    chunks: list[str] = [document.content] if document.content.strip() else []
    used = len(document.content.encode("utf-8"))
    manifest: list[dict[str, Any]] = []
    from app.auth import ensure_namespace

    for index, ref in entries:
        part = await find_document(session, document.workspace_id, ref)
        if part is None:
            add_warning("dangling_ref", f"part '{ref}' does not resolve", ref=ref)
            manifest.append({"index": index, "ref": ref, "missing": True})
            continue
        ensure_namespace(principal, part.namespace)
        used += len(part.content.encode("utf-8"))
        if used > MAX_EXPANDED_PARTS_BYTES:
            raise ApiError(
                413,
                "content_too_large",
                "expanded parts exceed the 4 MiB limit; dereference parts individually",
                details={"limit": MAX_EXPANDED_PARTS_BYTES, "action": "dereference_parts"},
            )
        chunks.append(part.content)
        manifest.append(
            {
                "index": index,
                "id": part.canonical_id,
                "alias": part.doc_key,
                "version": part.version,
                "content_hash": part.content_hash,
            }
        )
    return "\n\n".join(chunks), manifest


# --------------------------------------------------------- rename / purge


async def add_redirect(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    object_type: str,
    old_alias: str,
    target_canonical_id: str,
    renamed_by: str,
) -> None:
    session.add(
        AliasRedirect(
            workspace_id=workspace_id,
            object_type=object_type,
            old_alias=old_alias,
            target_canonical_id=target_canonical_id,
            renamed_by=renamed_by,
        )
    )


async def tombstone_document(
    session: AsyncSession, document: SharedDocument, *, purged_by: str, reason: str | None
) -> Tombstone:
    tombstone = Tombstone(
        workspace_id=document.workspace_id,
        object_type="document",
        canonical_id=document.canonical_id,
        alias=document.doc_key,
        namespace=document.namespace,
        last_version=document.version,
        last_content_hash=document.content_hash,
        purged_by=purged_by,
        purged_at=utcnow(),
        reason=reason,
    )
    session.add(tombstone)
    return tombstone
