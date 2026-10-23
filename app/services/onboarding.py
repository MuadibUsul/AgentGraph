"""Workspace conventions: constitution, namespace readmes (PRD AG-OB-02).

The ``system`` namespace belongs to the platform: ``system/constitution`` is
the workspace constitution and ``system/namespaces/<ns>`` documents each
namespace. Humans edit them directly; agents propose changes. Conventions are
soft by default (warnings, never rejected writes) unless a human turns on a
namespace's strict mode.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.errors import ApiError
from app.models import NamespaceRecord, SharedDocument, Workspace
from app.services.common import append_event, utcnow
from app.warnings_context import add_warning

SYSTEM_NAMESPACE = "system"
CONSTITUTION_KEY = "system/constitution"
NAMESPACE_README_PREFIX = "system/namespaces/"

CONSTITUTION_SECTIONS = (
    "空间用途",
    "参与者与职责（谁负责什么）",
    "写入规范（何时新建文档、何时更新、frontmatter 必填字段、命名约定）",
    "命名空间约定（新建 namespace 的规则）",
    "当前重点",
    "禁止事项",
)


_SYSTEM_STAMP = (
    '{"chain": ["system"], "kind": "system", "principal_id": "system", "token_id": null}'
)


def readme_key(namespace: str) -> str:
    return f"{NAMESPACE_README_PREFIX}{namespace}"


def is_system_address(namespace: str | None, doc_key: str) -> bool:
    return namespace == SYSTEM_NAMESPACE or doc_key == "system" or doc_key.startswith("system/")


def ensure_system_writable(principal: Principal, namespace: str | None, doc_key: str) -> None:
    """Agents change system documents through proposals, never directly."""
    if is_system_address(namespace, doc_key) and not principal.is_human:
        raise ApiError(
            403,
            "system_namespace_protected",
            "system/* documents are edited by humans; submit propose_document_change",
            details={"doc_key": doc_key, "action": "propose_document_change"},
        )


def constitution_body() -> str:
    lines = [
        "# 空间宪法 / Workspace constitution",
        "",
        "> 这份宪法由平台生成，等待人类补全。Agents: read it first; propose changes "
        "with propose_document_change.",
        "",
    ]
    for section in CONSTITUTION_SECTIONS:
        lines += [f"## {section}", "", "（待补充）", ""]
    return "\n".join(lines).rstrip() + "\n"


def readme_body(namespace: str, stats: dict[str, Any] | None) -> str:
    lines = [
        f"# Namespace `{namespace}`",
        "",
        "> 平台生成的说明桩，等待人类补全。Platform-generated stub; a human should fill it in.",
        "",
        "## 用途 / Purpose",
        "",
        "（待补充）",
        "",
        "## 文档结构 / Document structure",
        "",
        "（待补充）",
        "",
        "## 示例 doc_key / Example keys",
        "",
    ]
    samples = (stats or {}).get("sample_doc_keys") or []
    lines += [f"- `{key}`" for key in samples] or ["（暂无）"]
    if stats:
        common_keys = ", ".join(stats.get("top_frontmatter_keys") or []) or "-"
        lines += [
            "",
            "## 现状 / Current state (generated)",
            "",
            f"- documents: {stats.get('documents', 0)}",
            f"- earliest writer: {stats.get('earliest_writer') or '-'}",
            f"- latest writer: {stats.get('latest_writer') or '-'}",
            f"- common frontmatter keys: {common_keys}",
        ]
    return "\n".join(lines).rstrip() + "\n"


async def _create_once(session: AsyncSession, workspace_id: uuid.UUID, doc_key: str, build):
    """Create a platform document unless a concurrent request already did."""
    try:
        async with session.begin_nested():
            document = await build()
        return document, True
    except IntegrityError:
        existing = await _find(session, workspace_id, doc_key)
        if existing is None:
            raise
        return existing, False


async def _system_document(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    doc_key: str,
    title: str,
    content: str,
    frontmatter: dict[str, Any],
    author: str,
    namespace: str = SYSTEM_NAMESPACE,
) -> SharedDocument:
    """Insert a platform-owned DRAFT; system docs never count against quotas."""
    from app.services.documents import _record_version, content_hash
    from app.services.retrieval import reindex_document_chunks
    from app.stamp_context import stamp_json

    # Platform-generated documents carry the system stamp, not the stamp of
    # whichever request happened to trigger them (PRD AG-ID-03 accountability).
    await session.execute(
        text("SELECT set_config('app.lineage_stamp', :stamp, true)"), {"stamp": _SYSTEM_STAMP}
    )

    document = SharedDocument(
        workspace_id=workspace_id,
        doc_key=doc_key,
        title=title,
        content=content,
        content_hash=content_hash(content),
        frontmatter=frontmatter,
        verification_status="DRAFT",
        trust_level="TRUSTED",
        verified_version=None,
        verified_hash=None,
        embedding=None,
        embedding_model=None,
        namespace=namespace,
        version=1,
        locked_by_human=False,
        human_note=None,
        tainted=False,
        taint_sources=[],
        created_by=author,
        updated_by=author,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(document)
    await session.flush()
    from app.services.budget import suppress_metering

    with suppress_metering():
        await reindex_document_chunks(session, document)
    await _record_version(
        session, document, author=author, batch_id=uuid.uuid4(),
        change_summary="generated by the platform",
    )
    await session.flush()
    await session.execute(
        text("SELECT set_config('app.lineage_stamp', :stamp, true)"), {"stamp": stamp_json()}
    )
    return document


async def write_platform_document(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    namespace: str,
    doc_key: str,
    title: str,
    content: str,
    frontmatter: dict[str, Any],
    author: str,
    change_summary: str,
) -> SharedDocument:
    """Create or append a version of a platform-written document."""
    from app.services.documents import _record_version, content_hash
    from app.services.retrieval import reindex_document_chunks
    from app.stamp_context import stamp_json

    existing = await _find(session, workspace_id, doc_key)
    if existing is None:
        document, _ = await _create_once(
            session, workspace_id, doc_key,
            lambda: _system_document(
                session, workspace_id, doc_key=doc_key, title=title, content=content,
                frontmatter=frontmatter, author=author, namespace=namespace,
            ),
        )
        return document
    await session.execute(
        text("SELECT set_config('app.lineage_stamp', :stamp, true)"), {"stamp": _SYSTEM_STAMP}
    )
    existing.title = title
    existing.content = content
    existing.content_hash = content_hash(content)
    existing.frontmatter = frontmatter
    existing.version += 1
    existing.updated_by = author
    existing.updated_at = utcnow()
    await session.flush()
    from app.services.budget import suppress_metering

    with suppress_metering():
        await reindex_document_chunks(session, existing)
    await _record_version(
        session, existing, author=author, batch_id=uuid.uuid4(), change_summary=change_summary,
    )
    await session.flush()
    await session.execute(
        text("SELECT set_config('app.lineage_stamp', :stamp, true)"), {"stamp": stamp_json()}
    )
    return existing


async def _find(session: AsyncSession, workspace_id: uuid.UUID, doc_key: str):
    return await session.scalar(
        select(SharedDocument).where(
            SharedDocument.workspace_id == workspace_id, SharedDocument.doc_key == doc_key
        )
    )


async def _register_system_namespace(session: AsyncSession, workspace_id: uuid.UUID) -> None:
    exists = await session.scalar(
        select(NamespaceRecord.id).where(
            NamespaceRecord.workspace_id == workspace_id,
            NamespaceRecord.namespace == SYSTEM_NAMESPACE,
        )
    )
    if exists is None:
        session.add(
            NamespaceRecord(
                workspace_id=workspace_id,
                namespace=SYSTEM_NAMESPACE,
                purpose="platform conventions: constitution and namespace readmes",
                owner="system",
                write_policy="human_only",
                created_by="system",
            )
        )
        await session.flush()


async def ensure_constitution(session: AsyncSession, workspace_id: uuid.UUID) -> SharedDocument:
    existing = await _find(session, workspace_id, CONSTITUTION_KEY)
    if existing is not None:
        return existing
    await _register_system_namespace(session, workspace_id)
    document, _ = await _create_once(
        session,
        workspace_id,
        CONSTITUTION_KEY,
        lambda: _system_document(
            session,
            workspace_id,
            doc_key=CONSTITUTION_KEY,
            title="Workspace constitution",
            content=constitution_body(),
            frontmatter={"kind": "constitution", "owner": None, "stub": True},
            author="system",
        ),
    )
    return document


async def namespace_stats(
    session: AsyncSession, workspace_id: uuid.UUID, namespace: str
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(SharedDocument.doc_key, SharedDocument.frontmatter,
                   SharedDocument.created_by, SharedDocument.created_at)
            .where(
                SharedDocument.workspace_id == workspace_id,
                SharedDocument.namespace == namespace,
                SharedDocument.archived_at.is_(None),
            )
            .order_by(SharedDocument.created_at)
        )
    ).all()
    keys: Counter[str] = Counter()
    for _, frontmatter, _, _ in rows:
        keys.update((frontmatter or {}).keys())
    return {
        "documents": len(rows),
        "earliest_writer": rows[0][2] if rows else None,
        "latest_writer": rows[-1][2] if rows else None,
        "top_frontmatter_keys": [key for key, _ in keys.most_common(10)],
        "sample_doc_keys": [row[0] for row in rows[:5]],
    }


async def ensure_namespace_readme(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    namespace: str,
    *,
    registered_by: str,
    request_review: bool = True,
) -> tuple[SharedDocument, bool]:
    """Create the namespace readme stub if missing. Returns (doc, created)."""
    key = readme_key(namespace)
    existing = await _find(session, workspace_id, key)
    if existing is not None:
        return existing, False
    await _register_system_namespace(session, workspace_id)
    stats = await namespace_stats(session, workspace_id, namespace)
    document, created = await _create_once(session, workspace_id, key, lambda: _system_document(
        session,
        workspace_id,
        doc_key=key,
        title=f"Namespace {namespace}",
        content=readme_body(namespace, stats),
        frontmatter={
            "kind": "namespace_readme",
            "namespace": namespace,
            "owner": registered_by,
            "write_policy": "open",
            "required_frontmatter": [],
            "doc_key_pattern": None,
            "strict": False,
            "retention": None,
            "stub": True,
            "generated": {**stats, "registered_by": registered_by},
        },
        author="system",
    ))
    if not created:
        return document, False
    if request_review:
        await append_event(
            session,
            workspace_id=workspace_id,
            agent=registered_by,
            event_type="HUMAN_REVIEW_REQUESTED",
            payload={
                "resource": "namespace_readme",
                "doc_key": key,
                "namespace": namespace,
                "reason": "new namespace needs a human-written readme",
            },
            namespace=SYSTEM_NAMESPACE,
        )
    return document, True


async def check_conventions(
    session: AsyncSession,
    principal: Principal,
    namespace: str,
    doc_key: str,
    frontmatter: dict[str, Any],
) -> None:
    """Soft namespace conventions (PRD AG-OB-02 point 5); strict mode rejects."""
    if namespace == SYSTEM_NAMESPACE:
        return
    readme, created = await ensure_namespace_readme(
        session,
        principal.workspace.id,
        namespace,
        registered_by=principal.agent_signature,
    )
    meta = readme.frontmatter or {}
    if created or meta.get("stub"):
        add_warning(
            "namespace_undocumented",
            f"namespace '{namespace}' has no human-written readme yet "
            f"({readme_key(namespace)})",
            namespace=namespace,
            readme=readme.doc_key,
        )
    strict = bool(meta.get("strict"))
    required = [key for key in meta.get("required_frontmatter") or [] if isinstance(key, str)]
    missing = [key for key in required if key not in (frontmatter or {})]
    if missing:
        if strict:
            raise ApiError(
                422,
                "validation_error",
                f"namespace '{namespace}' requires frontmatter fields: {', '.join(missing)}",
                details={"field": "frontmatter", "rule": "required_frontmatter",
                         "missing": missing, "readme": readme.doc_key},
            )
        add_warning(
            "frontmatter_missing",
            f"namespace '{namespace}' expects frontmatter fields: {', '.join(missing)}",
            missing=missing,
            readme=readme.doc_key,
        )
    pattern = meta.get("doc_key_pattern")
    if isinstance(pattern, str) and pattern:
        try:
            matches = re.fullmatch(pattern, doc_key) is not None
        except re.error:
            matches = True
        if not matches:
            if strict:
                raise ApiError(
                    422,
                    "validation_error",
                    f"doc_key '{doc_key}' does not match the namespace pattern",
                    details={"field": "doc_key", "rule": "doc_key_pattern",
                             "pattern": pattern, "readme": readme.doc_key},
                )
            add_warning(
                "doc_key_pattern_mismatch",
                f"doc_key '{doc_key}' does not match {pattern}",
                pattern=pattern,
                readme=readme.doc_key,
            )


async def scaffold_new_workspace(session: AsyncSession, workspace_id: uuid.UUID) -> None:
    """Constitution and the default namespace readme for a brand-new workspace."""
    await ensure_constitution(session, workspace_id)
    await ensure_namespace_readme(
        session, workspace_id, "default", registered_by="system", request_review=False
    )
    from app.services.releases import write_changelog

    await write_changelog(session, workspace_id)


async def scaffold_workspace(session: AsyncSession, workspace_id: uuid.UUID) -> None:
    """Constitution plus a readme stub for every registered namespace (no commit)."""
    await ensure_constitution(session, workspace_id)
    namespaces = (
        await session.execute(
            select(NamespaceRecord.namespace).where(
                NamespaceRecord.workspace_id == workspace_id,
                NamespaceRecord.namespace != SYSTEM_NAMESPACE,
            )
        )
    ).scalars().all()
    for namespace in namespaces:
        await ensure_namespace_readme(
            session, workspace_id, namespace, registered_by="migration", request_review=False
        )


async def backfill_all_workspaces(session: AsyncSession) -> int:
    """Idempotent one-time migration: scaffold workspaces missing a constitution."""
    missing = (
        await session.execute(
            select(Workspace.id).where(
                ~select(SharedDocument.id)
                .where(
                    SharedDocument.workspace_id == Workspace.id,
                    SharedDocument.doc_key == CONSTITUTION_KEY,
                )
                .exists()
            )
        )
    ).scalars().all()
    for workspace_id in missing:
        await scaffold_workspace(session, workspace_id)
    await session.commit()
    return len(missing)


async def namespace_overview(
    session: AsyncSession, principal: Principal
) -> list[dict[str, Any]]:
    """namespace_map entries, trimmed to what the caller may read."""
    records = (
        await session.execute(
            select(NamespaceRecord)
            .where(NamespaceRecord.workspace_id == principal.workspace.id)
            .order_by(NamespaceRecord.namespace)
        )
    ).scalars().all()
    counts = dict(
        (
            await session.execute(
                select(SharedDocument.namespace, func.count(SharedDocument.id))
                .where(
                    SharedDocument.workspace_id == principal.workspace.id,
                    SharedDocument.archived_at.is_(None),
                )
                .group_by(SharedDocument.namespace)
            )
        ).all()
    )
    unverified = dict(
        (
            await session.execute(
                select(SharedDocument.namespace, func.count(SharedDocument.id))
                .where(
                    SharedDocument.workspace_id == principal.workspace.id,
                    SharedDocument.archived_at.is_(None),
                    SharedDocument.verification_status != "VERIFIED",
                )
                .group_by(SharedDocument.namespace)
            )
        ).all()
    )
    last = dict(
        (
            await session.execute(
                select(SharedDocument.namespace, func.max(SharedDocument.updated_at))
                .where(SharedDocument.workspace_id == principal.workspace.id)
                .group_by(SharedDocument.namespace)
            )
        ).all()
    )
    readmes = {
        doc.doc_key: doc
        for doc in (
            await session.execute(
                select(SharedDocument).where(
                    SharedDocument.workspace_id == principal.workspace.id,
                    SharedDocument.doc_key.like(f"{NAMESPACE_README_PREFIX}%"),
                )
            )
        ).scalars()
    }
    from app.auth import can

    entries = []
    for record in records:
        allowed = principal.is_commander or "*" in principal.allowed_namespaces or (
            record.namespace in principal.allowed_namespaces
        )
        readme = readmes.get(readme_key(record.namespace))
        if record.namespace == SYSTEM_NAMESPACE:
            access = "write" if principal.is_human else "read"
        elif not allowed:
            access = "none"
        elif can(principal, "doc:write") or can(principal, "doc:create"):
            access = "write"
        else:
            access = "read"
        entry: dict[str, Any] = {
            "namespace": record.namespace,
            "purpose": record.purpose,
            "owner": record.owner,
            "write_policy": (readme.frontmatter or {}).get("write_policy")
            if readme else record.write_policy,
            "readme": {"namespace": SYSTEM_NAMESPACE, "doc_key": readme.doc_key,
                       "version": readme.version, "stub": bool((readme.frontmatter or {})
                                                                .get("stub"))}
            if readme else None,
            "my_access": access,
        }
        if access != "none":
            entry.update(
                documents=int(counts.get(record.namespace, 0)),
                unverified=int(unverified.get(record.namespace, 0)),
                last_updated_at=last.get(record.namespace),
            )
        entries.append(entry)
    return entries
