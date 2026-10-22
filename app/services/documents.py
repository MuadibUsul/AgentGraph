"""Shared document operations with versioning, human locks, and L2 artifacts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Numeric, case, func, select, tuple_, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.addresses import doc_key_violation, is_canonical, is_legacy_alias
from app.auth import (
    Principal,
    can,
    effective_agent,
    ensure_namespace,
    ensure_not_human_locked,
    principal_for_admin,
    require_permission,
)
from app.config import get_settings
from app.engine.blackboard import parse_doc_uri, patch_markdown_section
from app.engine.chunking import extract_section
from app.engine.embeddings import get_embedding_provider
from app.engine.merge import three_way_document
from app.engine.notion import normalize_notion_content
from app.errors import (
    ApiError,
    Forbidden,
    NotFound,
    QuotaExceeded,
    RebaseConflict,
    TaintBlocked,
    VersionConflict,
)
from app.models import (
    CollabTask,
    DocumentChunk,
    DocumentProposal,
    DocumentVersion,
    SharedDocument,
    TaskStatus,
    Workspace,
)
from app.schemas import (
    DereferenceBatchItem,
    DereferenceBatchResponse,
    DereferenceResponse,
    DocumentArchiveRequest,
    DocumentBatchUpsertRequest,
    DocumentBatchUpsertResponse,
    DocumentBatchUpsertResult,
    DocumentHumanUpdateRequest,
    DocumentListResponse,
    DocumentMutationResponse,
    DocumentNormalizeResponse,
    DocumentOut,
    DocumentPurgeRequest,
    DocumentQueryHit,
    DocumentQueryRequest,
    DocumentQueryResponse,
    DocumentRenameRequest,
    DocumentSectionPatchRequest,
    DocumentStatusRequest,
    DocumentSummary,
    DocumentTaintRequest,
    DocumentUpsertRequest,
    DocumentVerifyRequest,
    DocumentVersionListResponse,
    DocumentVersionOut,
    DocumentVersionSummary,
    MatchedChunk,
    RegisterPartsRequest,
    TombstoneOut,
)
from app.services import access_log, agent_onboarding, budget, indexer, onboarding, provenance
from app.services import addresses as address_service
from app.services import recall as recall_service
from app.services.common import (
    PLATFORM_DOC_NAMESPACES,
    append_event,
    db_now,
    document_out,
    document_snapshot,
    document_summary,
    record_audit,
    utcnow,
)
from app.services.policies import enforce_policy
from app.services.retrieval import (
    hybrid_document_ids,
    query_vector,
)
from app.services.taint import (
    clear_document_taint,
    mark_document_taint,
    propagate_from_document,
    recompute_downstream_document_taint,
    refresh_document_taint,
)
from app.services.usage import enforce_embedding_budget
from app.versioning import LATEST_VERSION, at_least

settings = get_settings()


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _version_summary(version: DocumentVersion) -> DocumentVersionSummary:
    return DocumentVersionSummary(
        version=version.version,
        parent_version=version.parent_version,
        content_hash=version.content_hash,
        author=version.author,
        session_batch_id=version.session_batch_id,
        change_summary=version.change_summary,
        verification_status=version.verification_status,
        trust_level=version.trust_level,
        tainted=version.tainted,
        taint_sources=version.taint_sources or [],
        created_at=version.created_at,
        stamp=version.stamp,
        redacted=version.redacted_at is not None,
        redaction_hash=version.redaction_hash,
    )


def _version_out(version: DocumentVersion) -> DocumentVersionOut:
    return DocumentVersionOut(
        **_version_summary(version).model_dump(),
        id=version.id,
        document_id=version.document_id,
        title=version.title,
        content=version.content,
        frontmatter=version.frontmatter or {},
        namespace=version.namespace,
    )


async def _record_version(
    session: AsyncSession,
    document: SharedDocument,
    *,
    author: str,
    batch_id: uuid.UUID | None,
    change_summary: str,
) -> DocumentVersion:
    version = DocumentVersion(
        workspace_id=document.workspace_id,
        document_id=document.id,
        version=document.version,
        parent_version=document.version - 1 if document.version > 1 else None,
        content_hash=document.content_hash or content_hash(document.content),
        title=document.title,
        content=document.content,
        frontmatter=document.frontmatter or {},
        namespace=document.namespace,
        verification_status=document.verification_status,
        trust_level=document.trust_level,
        tainted=document.tainted,
        taint_sources=document.taint_sources or [],
        content_origin=document.content_origin or "internal",
        author=author,
        session_batch_id=batch_id,
        change_summary=change_summary,
        created_at=utcnow(),
    )
    session.add(version)
    await session.flush()
    return version


def _embedding_text(text: str) -> str:
    # Jina's maximum is 8194 tokens; a whole 100 KB Chinese document can exceed
    # that. Document writes use chunk embeddings instead; this is only for the
    # short query fallback.
    return text[:6_000]


async def _document_embedding(
    workspace_id: uuid.UUID, content: str
) -> tuple[list[float] | None, str | None]:
    """Budget check for a write; documents are never embedded whole.

    A whole 100 KB document can exceed the provider's token limit, so writes
    embed chunks only and the document vector is the mean of its chunk
    vectors, set when the chunks are (re)indexed. Asynchronous writes
    (2026-11) leave that to the index worker.
    """
    if indexer.asynchronous() or await budget.defer_embedding(workspace_id):
        return None, None
    await enforce_embedding_budget(session=None, workspace_id=workspace_id,
                                   chars=len(content.encode("utf-8")))
    return None, None


async def human_update_frontmatter(
    session: AsyncSession,
    principal: Principal,
    document: SharedDocument,
    frontmatter: dict[str, Any],
    *,
    change_summary: str,
) -> None:
    """Frontmatter-only edit by a human, recorded as a new version."""
    document.frontmatter = frontmatter
    document.version += 1
    document.updated_by = principal.agent_signature
    document.updated_at = utcnow()
    await session.flush()
    await _record_version(
        session, document, author=principal.agent_signature, batch_id=uuid.uuid4(),
        change_summary=change_summary,
    )


async def _embed(content: str) -> list[float]:
    provider = get_embedding_provider()
    return await provider.embed_one(_embedding_text(content))


async def _ensure_document_quota(
    session: AsyncSession, workspace: Workspace
) -> None:
    limit, _ = settings.tier_limits(workspace.tier)
    used = await session.scalar(
        select(func.count(SharedDocument.id)).where(
            SharedDocument.workspace_id == workspace.id,
            # Platform-owned conventions never consume the customer's quota.
            SharedDocument.namespace != onboarding.SYSTEM_NAMESPACE,
        )
    )
    if (used or 0) >= limit:
        raise QuotaExceeded("shared_documents", limit)


def ensure_content_size(content: str) -> int:
    """Accept up to 1 MiB of UTF-8; larger bodies must be split into parts."""
    size = len(content.encode("utf-8"))
    limit = settings.max_document_content_bytes
    if size > limit:
        raise ApiError(
            413,
            "content_too_large",
            f"document content is {size} bytes; the limit is {limit} bytes. "
            "Split it into parts (frontmatter.parts / part_of) instead.",
            details={
                "limit_bytes": limit,
                "actual_bytes": size,
                "action": "split_into_parts",
                "spec": "AG-ST-17",
            },
        )
    return size


async def _mutation_response(
    session: AsyncSession, document: SharedDocument, batch: uuid.UUID
) -> DocumentMutationResponse:
    chunks = int(
        await session.scalar(
            select(func.count(DocumentChunk.id)).where(DocumentChunk.document_id == document.id)
        )
        or 0
    )
    return DocumentMutationResponse(
        document=document_out(document),
        session_batch_id=batch,
        content_bytes=len(document.content.encode("utf-8")),
        chunk_count=chunks,
        usage=budget.request_usage(),
    )


def _latest(document: SharedDocument) -> dict[str, Any]:
    return document_out(document).model_dump(mode="json")


def _allowed_namespaces(principal: Principal) -> list[str] | None:
    if principal.is_commander or "*" in principal.allowed_namespaces:
        return None
    return principal.allowed_namespaces


def _visibility_conditions(
    principal: Principal,
    *,
    include_unverified: bool,
    include_tainted: bool,
) -> list[Any]:
    if principal.is_commander:
        return []
    conditions: list[Any] = []
    if not include_unverified and not can(principal, "doc:verify"):
        conditions.append(
            (SharedDocument.verification_status == "VERIFIED")
            | (SharedDocument.created_by == principal.agent_signature)
            | (
                SharedDocument.namespace.in_(PLATFORM_DOC_NAMESPACES)
                & (SharedDocument.created_by == "system")
            )
        )
    if not include_tainted:
        conditions.append(
            SharedDocument.tainted.is_(False)
            | (SharedDocument.created_by == principal.agent_signature)
            | (
                SharedDocument.namespace.in_(PLATFORM_DOC_NAMESPACES)
                & (SharedDocument.created_by == "system")
            )
        )
    return conditions


def _ensure_document_visible(
    principal: Principal,
    document: SharedDocument,
    *,
    include_unverified: bool,
    include_tainted: bool,
) -> None:
    if principal.is_commander:
        return
    own = document.created_by == principal.agent_signature
    platform_system = (
        document.namespace in PLATFORM_DOC_NAMESPACES and document.created_by == "system"
    )
    if platform_system:
        return
    if (
        not include_unverified
        and not own
        and not can(principal, "doc:verify")
        and document.verification_status != "VERIFIED"
    ):
        raise NotFound("shared_document")
    if not include_tainted and not own and document.tainted:
        raise NotFound("shared_document")


async def _get_document(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    doc_key: str,
    *,
    for_update: bool = False,
) -> SharedDocument:
    # Accepts a canonical id, an alias or a redirected alias (AG-ST-17).
    return await address_service.resolve_document(
        session, workspace_id, doc_key, for_update=for_update
    )


def _title_for(
    request: DocumentUpsertRequest, doc_key: str, content: str | None = None
) -> str:
    if request.title:
        return request.title
    source = content if content is not None else request.content
    first_line = next(
        (line.strip("# ").strip() for line in source.splitlines() if line.strip()),
        "",
    )
    return (first_line[:256] or doc_key)[:256]


def _encode_cursor(updated_at: datetime, record_id: uuid.UUID) -> str:
    raw = json.dumps(
        {"updated_at": updated_at.isoformat(), "id": str(record_id)},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        )
        return datetime.fromisoformat(payload["updated_at"]), uuid.UUID(payload["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ApiError(400, "invalid_cursor", "document cursor is invalid") from exc


_FILTER_OPERATORS = {"eq", "gte", "lte", "gt", "lt", "exists"}


def _frontmatter_conditions(filters: dict[str, Any]) -> list[Any]:
    conditions: list[Any] = []
    for key, value in filters.items():
        if not isinstance(value, dict):
            conditions.append(SharedDocument.frontmatter.contains({key: value}))
            continue
        unknown = sorted(set(value) - _FILTER_OPERATORS)
        if unknown:
            raise ApiError(
                422,
                "invalid_filter_operator",
                f"unsupported frontmatter filter operator for {key}: {', '.join(unknown)}",
                details={
                    "field": f"filters.{key}",
                    "allowed": sorted(_FILTER_OPERATORS),
                },
            )
        if "exists" in value:
            if not isinstance(value["exists"], bool):
                raise ApiError(
                    422,
                    "invalid_filter_value",
                    f"frontmatter filter {key}.exists must be a boolean",
                    details={"field": f"filters.{key}.exists"},
                )
            present = SharedDocument.frontmatter.has_key(key)
            conditions.append(present if value["exists"] else ~present)
        if "eq" in value:
            conditions.append(
                SharedDocument.frontmatter.contains({key: value["eq"]})
            )
        for operator in ("gte", "lte", "gt", "lt"):
            if operator not in value:
                continue
            expected = value[operator]
            if isinstance(expected, bool) or not isinstance(expected, (int, float, str)):
                raise ApiError(
                    422,
                    "invalid_filter_value",
                    f"frontmatter filter {key}.{operator} must be a scalar",
                    details={"field": f"filters.{key}.{operator}"},
                )
            expression = SharedDocument.frontmatter[key].astext
            if isinstance(expected, (int, float)):
                expression = case(
                    (
                        func.jsonb_typeof(SharedDocument.frontmatter[key])
                        == "number",
                        expression.cast(Numeric),
                    ),
                    else_=None,
                )
            column = {
                "gte": expression >= expected,
                "lte": expression <= expected,
                "gt": expression > expected,
                "lt": expression < expected,
            }[operator]
            conditions.append(column)
    return conditions


LIST_ORDERS = ("doc_key_asc", "updated_at_asc")
LIST_LIMIT_MAX = 500
_LIST_ALWAYS = ("id", "canonical_id", "namespace", "doc_key")


def _cursor_key() -> bytes:
    secret = settings.cursor_signing_secret or settings.database_url
    return hashlib.sha256(f"agentgraph-list-cursor:{secret}".encode()).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_list_cursor(payload: dict[str, Any]) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    mac = hmac.new(_cursor_key(), body.encode(), hashlib.sha256).digest()[:16]
    return f"{body}.{_b64(mac)}"


def _open_list_cursor(cursor: str, filter_hash: str, now: datetime) -> dict[str, Any]:
    invalid = ApiError(400, "cursor_invalid", "cursor is altered or not a list cursor",
                       details={"field": "cursor"})
    try:
        body, mac = cursor.split(".")
        expected = hmac.new(_cursor_key(), body.encode(), hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(_unb64(mac), expected):
            raise invalid
        payload = json.loads(_unb64(body))
        issued = datetime.fromisoformat(payload["t"])
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - any malformed cursor is the client's
        raise invalid from exc
    if payload.get("f") != filter_hash:
        raise ApiError(400, "cursor_invalid",
                       "cursor belongs to a different filter or order; start again",
                       details={"field": "cursor", "reason": "filter_mismatch"})
    if now - issued > timedelta(hours=settings.list_cursor_ttl_hours):
        raise ApiError(410, "cursor_expired", "this enumeration is too old; start again",
                       details={"issued_at": payload["t"],
                                "max_age_hours": settings.list_cursor_ttl_hours})
    return payload


def _project(summary: dict[str, Any], fields: list[str] | None) -> dict[str, Any]:
    if fields is None:
        return summary
    keep = dict.fromkeys([*_LIST_ALWAYS, *fields])
    return {key: summary.get(key) for key in keep}


@access_log.reads("document")
@agent_onboarding.records("read")
async def list_documents(
    session: AsyncSession,
    principal: Principal,
    *,
    namespace: str | None = None,
    filters: dict[str, Any] | None = None,
    limit: int = 100,
    cursor: str | None = None,
    include_archived: bool = False,
    include_unverified: bool | None = None,
    include_tainted: bool | None = None,
    verification_status: str | None = None,
    tainted: bool | None = None,
    updated_after: datetime | None = None,
    updated_before: datetime | None = None,
    sort: str | None = None,
    order: str | None = None,
    fields: list[str] | None = None,
    written_by_subtree: str | None = None,
) -> DocumentListResponse:
    """Complete enumeration (PRD AG-ST-01).

    The census contract (``order``; the default under API 2026-11) pages by
    keyset in doc_key or (updated_at, doc_key) order with a signed cursor that
    carries the filter, the first page's exact ``snapshot_count`` and
    ``as_of``; it includes every trust state by default. Baseline callers keep
    the 3.2.1 (updated_at, id) listing.
    """
    require_permission(principal, "doc:read")
    census = order is not None or (sort is None and at_least(LATEST_VERSION))
    if census:
        order = order or "doc_key_asc"
        if order not in LIST_ORDERS:
            raise ApiError(422, "validation_error", "order must be doc_key_asc or updated_at_asc",
                           details={"field": "order", "allowed": list(LIST_ORDERS)})
        if not 1 <= limit <= LIST_LIMIT_MAX:
            raise ApiError(422, "validation_error", f"limit must be 1-{LIST_LIMIT_MAX}",
                           details={"field": "limit", "limit": LIST_LIMIT_MAX})
        include_unverified = True if include_unverified is None else include_unverified
        include_tainted = True if include_tainted is None else include_tainted
    else:
        sort = sort or "updated_desc"
        include_unverified = bool(include_unverified)
        include_tainted = bool(include_tainted)
        if limit > settings.max_page_size:
            raise ApiError(
                422,
                "limit_too_large",
                f"limit must be <= {settings.max_page_size}",
                details={"limit": settings.max_page_size},
            )
        if sort not in ("updated_desc", "updated_asc"):
            raise ApiError(
                422,
                "invalid_sort",
                "sort must be updated_desc or updated_asc",
                details={"allowed": ["updated_desc", "updated_asc"]},
            )
    if fields is not None:
        allowed_fields = set(DocumentSummary.model_fields)
        unknown = sorted(set(fields) - allowed_fields)
        if unknown:
            raise ApiError(422, "validation_error", "unknown fields requested",
                           details={"field": "fields", "unknown": unknown,
                                    "allowed": sorted(allowed_fields)})
    conditions: list[Any] = [SharedDocument.workspace_id == principal.workspace.id]
    if namespace:
        ensure_namespace(principal, namespace)
        conditions.append(SharedDocument.namespace == namespace)
    else:
        allowed = _allowed_namespaces(principal)
        if allowed is not None:
            conditions.append(SharedDocument.namespace.in_(allowed))
    if filters:
        conditions.extend(_frontmatter_conditions(filters))
    if not include_archived:
        conditions.append(SharedDocument.archived_at.is_(None))
    conditions.extend(
        _visibility_conditions(
            principal,
            include_unverified=include_unverified,
            include_tainted=include_tainted,
        )
    )
    if verification_status:
        status = verification_status.upper()
        if status not in {"DRAFT", "PENDING_REVIEW", "VERIFIED", "DEPRECATED"}:
            raise ApiError(
                422,
                "invalid_verification_status",
                "unknown verification_status",
                details={"field": "verification_status"},
            )
        conditions.append(SharedDocument.verification_status == status)
    if tainted is not None:
        conditions.append(SharedDocument.tainted.is_(tainted))
    if updated_after is not None:
        conditions.append(SharedDocument.updated_at >= updated_after)
    if updated_before is not None:
        conditions.append(SharedDocument.updated_at <= updated_before)
    if written_by_subtree:
        # Documents with any version stamped by this token or its descendants
        # (PRD AG-ID-03 point 5): the blast radius of a token subtree.
        from app.services.lineage import subtree_token_ids

        token_ids = await subtree_token_ids(session, principal, written_by_subtree)
        conditions.append(
            select(DocumentVersion.id)
            .where(
                DocumentVersion.document_id == SharedDocument.id,
                DocumentVersion.stamp["chain"].has_any(postgresql.array(token_ids)),
            )
            .exists()
        )
    if census:
        return await _census_page(
            session, principal, conditions,
            order=order, limit=limit, cursor=cursor, fields=fields, namespace=namespace,
            filter_hash=hashlib.sha256(
                json.dumps(
                    [principal.agent_signature, namespace, filters or {}, include_archived,
                     include_unverified, include_tainted, verification_status, tainted,
                     updated_after.isoformat() if updated_after else None,
                     updated_before.isoformat() if updated_before else None,
                     written_by_subtree, order],
                    sort_keys=True, default=str,
                ).encode()
            ).hexdigest()[:32],
        )
    total = int(
        await session.scalar(
            select(func.count(SharedDocument.id)).where(*conditions)
        )
        or 0
    )
    query = select(SharedDocument).where(*conditions)
    page_size = limit
    if cursor:
        cursor_time, cursor_id = _decode_cursor(cursor)
        comparator = (
            tuple_(SharedDocument.updated_at, SharedDocument.id)
            > tuple_(cursor_time, cursor_id)
            if sort == "updated_asc"
            else tuple_(SharedDocument.updated_at, SharedDocument.id)
            < tuple_(cursor_time, cursor_id)
        )
        query = query.where(comparator)
    if sort == "updated_asc":
        query = query.order_by(
            SharedDocument.updated_at.asc(), SharedDocument.id.asc()
        )
    else:
        query = query.order_by(
            SharedDocument.updated_at.desc(), SharedDocument.id.desc()
        )
    rows = (
        await session.execute(query.limit(page_size + 1))
    ).scalars().all()
    next_cursor = None
    if len(rows) > page_size:
        last = rows[page_size - 1]
        next_cursor = _encode_cursor(last.updated_at, last.id)
        rows = rows[:page_size]
    await indexer.annotate(session, list(rows))
    return DocumentListResponse(
        items=[
            _project(document_summary(row, compact_frontmatter=True).model_dump(mode="json"),
                     fields)
            for row in rows
        ],
        next_cursor=next_cursor,
        namespace=namespace,
        count=len(rows),
        total=total,
    )


async def _census_page(
    session: AsyncSession,
    principal: Principal,
    conditions: list[Any],
    *,
    order: str,
    limit: int,
    cursor: str | None,
    fields: list[str] | None,
    namespace: str | None,
    filter_hash: str,
) -> DocumentListResponse:
    now = await db_now(session)
    if order == "doc_key_asc":
        columns = (SharedDocument.doc_key, SharedDocument.id)
    else:
        columns = (SharedDocument.updated_at, SharedDocument.doc_key, SharedDocument.id)
    query = select(SharedDocument).where(*conditions)
    if cursor:
        payload = _open_list_cursor(cursor, filter_hash, now)
        as_of = datetime.fromisoformat(payload["t"])
        snapshot_count = int(payload["c"])
        key = list(payload["k"])
        if order == "updated_at_asc":
            key[0] = datetime.fromisoformat(key[0])
        key[-1] = uuid.UUID(key[-1])
        query = query.where(tuple_(*columns) > tuple_(*key))
    else:
        as_of = now
        snapshot_count = int(
            await session.scalar(select(func.count(SharedDocument.id)).where(*conditions)) or 0
        )
    rows = (
        await session.execute(query.order_by(*(column.asc() for column in columns))
                              .limit(limit + 1))
    ).scalars().all()
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        key = (
            [last.doc_key, str(last.id)] if order == "doc_key_asc"
            else [last.updated_at.isoformat(), last.doc_key, str(last.id)]
        )
        next_cursor = sign_list_cursor(
            {"v": 2, "o": order, "k": key, "f": filter_hash, "t": as_of.isoformat(),
             "c": snapshot_count}
        )
    await indexer.annotate(session, list(rows))
    return DocumentListResponse(
        items=[
            _project(document_summary(row, compact_frontmatter=True).model_dump(mode="json"),
                     fields)
            for row in rows
        ],
        next_cursor=next_cursor,
        namespace=namespace,
        count=len(rows),
        total=snapshot_count,
        order=order,
        snapshot_count=snapshot_count,
        as_of=as_of,
    )


def _can_manage_document(principal: Principal, document: SharedDocument) -> bool:
    return principal.is_commander or document.created_by == principal.agent_signature


async def _set_document_archived(
    session: AsyncSession,
    principal: Principal,
    doc_key: str,
    request: DocumentArchiveRequest,
    *,
    archived: bool,
) -> DocumentMutationResponse:
    require_permission(principal, "doc:write")
    document = await _get_document(
        session, principal.workspace.id, doc_key, for_update=True
    )
    ensure_namespace(principal, document.namespace, write=True)
    action = "archive" if archived else "restore"
    onboarding.ensure_system_writable(principal, document.namespace, document.doc_key)
    if not _can_manage_document(principal, document):
        raise Forbidden(
            f"only the creator or a workspace commander can {action} this document"
        )
    ensure_not_human_locked(
        principal, "shared_document", document.locked_by_human, document.human_note
    )
    if request.expected_version is not None and document.version != request.expected_version:
        raise VersionConflict("shared_document", _latest(document))
    batch = uuid.uuid4()
    changed = (document.archived_at is None) == archived
    if changed:
        before = document_snapshot(document)
        document.archived_at = utcnow() if archived else None
        document.archived_by = principal.agent_signature if archived else None
        document.archive_reason = request.reason if archived else None
        document.updated_by = principal.agent_signature
        document.updated_at = utcnow()
        await session.flush()
        await record_audit(
            session,
            workspace_id=principal.workspace.id,
            session_batch_id=batch,
            agent_signature=principal.agent_signature,
            target_table="shared_documents",
            target_record_id=document.id,
            operation="UPDATE",
            before=before,
            after=document_snapshot(document),
        )
        await append_event(
            session,
            workspace_id=principal.workspace.id,
            agent=principal.agent_signature,
            event_type="DOC_ARCHIVED" if archived else "DOC_RESTORED",
            payload={"doc_key": doc_key, "reason": request.reason},
            namespace=document.namespace,
        )
        await session.commit()
    return DocumentMutationResponse(document=document_out(document), session_batch_id=batch)


async def archive_document(
    session: AsyncSession,
    principal: Principal,
    doc_key: str,
    request: DocumentArchiveRequest,
) -> DocumentMutationResponse:
    return await _set_document_archived(
        session, principal, doc_key, request, archived=True
    )


async def restore_document(
    session: AsyncSession,
    principal: Principal,
    doc_key: str,
    request: DocumentArchiveRequest,
) -> DocumentMutationResponse:
    return await _set_document_archived(
        session, principal, doc_key, request, archived=False
    )


@agent_onboarding.records("document")
async def batch_upsert_documents(
    session: AsyncSession,
    principal: Principal,
    request: DocumentBatchUpsertRequest,
) -> DocumentBatchUpsertResponse:
    results: list[DocumentBatchUpsertResult] = []
    for index, item in enumerate(request.items):
        try:
            result = await upsert_document(
                session, principal, item.doc_key, item, commit=False
            )
        except ApiError as exc:
            await session.rollback()
            details = {"index": index, "doc_key": item.doc_key}
            if isinstance(exc.details, dict):
                details.update(exc.details)
            raise ApiError(
                exc.status_code,
                exc.code,
                f"batch item {index} ({item.doc_key}): {exc.message}",
                details=details,
            ) from exc
        results.append(
            DocumentBatchUpsertResult(
                ok=True,
                document=result.document,
                session_batch_id=result.session_batch_id,
            )
        )
    await session.commit()
    return DocumentBatchUpsertResponse(items=results)


async def batch_dereference(
    session: AsyncSession,
    principal: Principal,
    uris: list[str],
    *,
    include_unverified: bool = False,
    include_tainted: bool = False,
    taint_mode: str = "annotate",
) -> DereferenceBatchResponse:
    items: list[DereferenceBatchItem] = []
    for uri in uris:
        try:
            result = await dereference(
                session,
                principal,
                uri,
                include_unverified=include_unverified,
                include_tainted=include_tainted,
                taint_mode=taint_mode,
            )
            items.append(DereferenceBatchItem(uri=uri, ok=True, document=result.document))
        except ApiError as exc:
            items.append(DereferenceBatchItem(uri=uri, ok=False, error=exc.body()["error"]))
    return DereferenceBatchResponse(items=items)


@access_log.reads("document")
@agent_onboarding.records_read
async def get_document(
    session: AsyncSession,
    principal: Principal,
    doc_key: str,
    *,
    include_unverified: bool = False,
    include_tainted: bool = False,
    taint_mode: str = "annotate",
) -> DocumentOut:
    require_permission(principal, "doc:read")
    document = await _get_document(session, principal.workspace.id, doc_key)
    ensure_namespace(principal, document.namespace)
    _ensure_document_visible(
        principal,
        document,
        include_unverified=include_unverified,
        include_tainted=include_tainted,
    )
    await indexer.annotate(session, [document])
    await provenance.note_reads(principal, [document])
    return provenance.apply_taint_mode(document_out(document), taint_mode)


@agent_onboarding.records("document")
async def upsert_document(
    session: AsyncSession,
    principal: Principal,
    doc_key: str,
    request: DocumentUpsertRequest,
    *,
    actor: str | None = None,
    batch_id: uuid.UUID | None = None,
    commit: bool = True,
) -> DocumentMutationResponse:
    agent = actor or effective_agent(principal, request.agent)
    batch = batch_id or request.session_batch_id or uuid.uuid4()
    content, normalized_html = normalize_notion_content(request.content)
    ensure_content_size(content)
    try:
        existing = await address_service.find_document(session, principal.workspace.id, doc_key)
    except ApiError as exc:
        if exc.code != "gone" or request.expected_version != 0:
            raise
        # Creating at a purged address: answered by ensure_alias_available below.
        existing = None
    if existing is not None:
        doc_key = existing.doc_key
    await budget.check_write(
        principal, document_id=existing.id if existing else None, doc_key=doc_key
    )
    origin = getattr(request, "content_origin", "internal")
    origins = await provenance.collect(
        session, principal, agent=agent, derived_from=getattr(request, "derived_from", []),
        content_origin=origin, exclude_document_id=existing.id if existing else None,
    )
    frontmatter_in = request.frontmatter
    ref_edges: list[tuple[str, str, str | None]] = []
    if address_service.has_refs(frontmatter_in):
        frontmatter_in, ref_edges = await address_service.resolve_frontmatter_refs(
            session, principal.workspace.id, frontmatter_in
        )

    if existing is None:
        require_permission(principal, "doc:create")
        namespace = request.namespace or "default"
        ensure_namespace(principal, namespace, write=True)
        if request.expected_version != 0:
            raise VersionConflict("shared_document", None)
        if is_canonical(doc_key):
            raise NotFound("shared_document", f"document '{doc_key}' not found")
        onboarding.ensure_system_writable(principal, namespace, doc_key)
        legacy = address_service.check_new_document_address(
            namespace, doc_key, allow_system=principal.is_human
        )
        await address_service.ensure_alias_available(
            session, principal.workspace.id, "document", doc_key
        )
        await address_service.ensure_namespace_registered(session, principal, namespace)
        await onboarding.check_conventions(
            session, principal, namespace, doc_key, frontmatter_in
        )
        policy = await enforce_policy(
            session,
            principal,
            resource_type="document",
            key=doc_key,
            action="create",
            payload=frontmatter_in,
            content_bytes=len(content.encode("utf-8")),
        )
        await _ensure_document_quota(session, principal.workspace)
        vector, model = await _document_embedding(principal.workspace.id, content)
        document = SharedDocument(
            workspace_id=principal.workspace.id,
            doc_key=doc_key,
            title=_title_for(request, doc_key, content),
            content=content,
            content_hash=content_hash(content),
            frontmatter=frontmatter_in,
            alias_legacy=legacy,
            verification_status="DRAFT",
            trust_level="TRUSTED" if principal.is_human else "UNTRUSTED",
            verified_version=None,
            verified_hash=None,
            embedding=vector,
            embedding_model=model,
            namespace=namespace,
            version=1,
            locked_by_human=False,
            human_note=None,
            tainted=False,
            taint_sources=[],
            policy_flags={"policy": policy.name} if policy else {},
            created_by=agent,
            updated_by=agent,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        session.add(document)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            latest = (
                await session.execute(
                    select(SharedDocument).where(
                        SharedDocument.workspace_id == principal.workspace.id,
                        SharedDocument.doc_key == doc_key,
                    )
                )
            ).scalar_one_or_none()
            raise VersionConflict(
                "shared_document",
                document_out(latest).model_dump(mode="json") if latest else None,
            ) from None
        document.provenance_taint = origins.sources
        document.content_origin = origin
        await refresh_document_taint(session, document)
        await indexer.after_write(session, document)
        await record_audit(
            session,
            workspace_id=principal.workspace.id,
            session_batch_id=batch,
            agent_signature=agent,
            target_table="shared_documents",
            target_record_id=document.id,
            operation="INSERT",
            before={},
            after=document_snapshot(document),
        )
        await _record_version(
            session,
            document,
            author=agent,
            batch_id=batch,
            change_summary="document created",
        )
        await provenance.record_edges(session, document, origins)
        await address_service.record_document_refs(session, document, ref_edges)
        await append_event(
            session,
            workspace_id=principal.workspace.id,
            agent=agent,
            event_type="DOC_CREATED",
            payload={
                "doc_key": doc_key,
                "title": document.title,
                "namespace": namespace,
                "version": 1,
                "tainted": document.tainted,
                "html_normalized": normalized_html,
            },
            namespace=namespace,
        )
        await _deliver_mentions(session, principal, request, document, previous=None)
        if commit:
            await session.commit()
        return await _mutation_response(session, document, batch)

    require_permission(principal, "doc:write")
    ensure_namespace(principal, existing.namespace)
    previous_content = existing.content
    if request.expected_version == 0 and at_least(LATEST_VERSION):
        # Creating over an existing alias is refused, never an overwrite.
        raise ApiError(
            409,
            "alias_taken",
            f"document alias '{doc_key}' already exists",
            details={
                "alias": doc_key,
                "id": existing.canonical_id,
                "current_version": existing.version,
            },
        )
    ensure_not_human_locked(
        principal, "shared_document", existing.locked_by_human, existing.human_note
    )
    policy = await enforce_policy(
        session,
        principal,
        resource_type="document",
        key=doc_key,
        action="write",
        payload=frontmatter_in,
        content_bytes=len(content.encode("utf-8")),
    )
    namespace = request.namespace or existing.namespace
    ensure_namespace(principal, namespace, write=True)
    onboarding.ensure_system_writable(principal, existing.namespace, doc_key)
    onboarding.ensure_system_writable(principal, namespace, doc_key)
    if namespace != existing.namespace:
        await address_service.ensure_namespace_registered(session, principal, namespace)
    await onboarding.check_conventions(session, principal, namespace, doc_key, frontmatter_in)

    document = await _get_document(
        session, principal.workspace.id, doc_key, for_update=True
    )
    ensure_not_human_locked(
        principal, "shared_document", document.locked_by_human, document.human_note
    )
    requested_title = _title_for(request, doc_key, content)
    rebased = False
    if document.version != request.expected_version:
        if not request.rebase:
            raise VersionConflict(
                "shared_document",
                _latest(document),
                expected_version=request.expected_version,
                include_content=request.return_current_on_conflict,
            )
        base = (
            await session.execute(
                select(DocumentVersion).where(
                    DocumentVersion.document_id == document.id,
                    DocumentVersion.version == request.expected_version,
                )
            )
        ).scalar_one_or_none()
        if base is None:
            raise VersionConflict(
                "shared_document",
                _latest(document),
                expected_version=request.expected_version,
                include_content=request.return_current_on_conflict,
            )
        merged = three_way_document(
            base_content=base.content,
            current_content=document.content,
            proposed_content=content,
            base_title=base.title,
            current_title=document.title,
            proposed_title=requested_title,
            base_frontmatter=base.frontmatter or {},
            current_frontmatter=document.frontmatter or {},
            proposed_frontmatter=frontmatter_in,
        )
        if merged.conflicts:
            await session.rollback()
            raise RebaseConflict(
                "shared_document",
                [conflict.as_dict() for conflict in merged.conflicts],
            )
        title = merged.title or document.title
        content = merged.content
        frontmatter = merged.frontmatter
        rebased = True
    else:
        title = requested_title
        frontmatter = frontmatter_in

    new_hash = content_hash(content)
    if (
        not rebased
        and new_hash == (document.content_hash or content_hash(document.content))
        and frontmatter == (document.frontmatter or {})
        and title == document.title
        and namespace == document.namespace
    ):
        # Same as the current version: no new version, no embedding (AG-ST-06).
        await indexer.annotate(session, [document])
        response = await _mutation_response(session, document, batch)
        return response.model_copy(update={"noop": True})
    before = document_snapshot(document)
    if new_hash != (document.content_hash or content_hash(document.content)):
        document.verification_status = "DRAFT"
        document.verified_version = None
        document.verified_hash = None
    document.title = title
    document.content = content
    document.content_hash = new_hash
    document.frontmatter = frontmatter
    document.embedding, document.embedding_model = await _document_embedding(
        principal.workspace.id, content
    )
    document.namespace = namespace
    document.policy_flags = {"policy": policy.name} if policy else {}
    document.version += 1
    document.updated_by = agent
    document.updated_at = utcnow()
    document.provenance_taint = origins.sources
    document.content_origin = origin
    document.recall_severity = None
    await session.flush()
    await refresh_document_taint(session, document)
    await indexer.after_write(session, document)
    await _record_version(
        session,
        document,
        author=agent,
        batch_id=batch,
        change_summary="document updated"
        + (" with rebase" if rebased else ""),
    )
    await provenance.record_edges(session, document, origins)
    if getattr(request, "resolves_recall", None):
        await recall_service.resolve_by_write(
            session, principal, document, origins, request.resolves_recall)
    await address_service.record_document_refs(session, document, ref_edges)
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=agent,
        target_table="shared_documents",
        target_record_id=document.id,
        operation="UPDATE",
        before=before,
        after=document_snapshot(document),
    )
    await propagate_from_document(session, document)
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=agent,
        event_type="DOC_UPDATED",
        payload={
            "doc_key": doc_key,
            "version": document.version,
            "namespace": namespace,
            "tainted": document.tainted,
            "rebased": rebased,
            "html_normalized": normalized_html,
        },
        namespace=namespace,
    )
    await _deliver_mentions(session, principal, request, document, previous=previous_content)
    if commit:
        await session.commit()
    return await _mutation_response(session, document, batch)


async def _deliver_mentions(
    session: AsyncSession,
    principal: Principal,
    request: DocumentUpsertRequest,
    document: SharedDocument,
    *,
    previous: str | None,
) -> None:
    """AG-EV-03: explicit mentions, plus @refs that are new in this version."""
    from app.services import collab

    explicit = list(getattr(request, "mentions", None) or [])
    known = set(collab.parse_mentions(None, previous)) if previous else set()
    found = [ref for ref in collab.parse_mentions(None, document.content) if ref not in known]
    mentions = collab.parse_mentions(explicit + found, None)
    await collab.deliver_mentions(
        session, principal, mentions,
        refs={"doc_key": document.doc_key, "id": document.canonical_id,
              "title": document.title, "version": document.version},
        namespace=document.namespace,
    )


_SECTION_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _strip_matching_heading(text: str, heading: str) -> str:
    wanted = heading.strip().lstrip("#").strip().casefold()
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        match = _SECTION_HEADING.match(line)
        if match and match.group(2).strip().casefold() == wanted:
            return "\n".join(lines[index + 1 :]).lstrip("\n")
        break
    return text


@agent_onboarding.records("document")
async def patch_document_section(
    session: AsyncSession,
    principal: Principal,
    doc_key: str,
    request: DocumentSectionPatchRequest,
) -> DocumentMutationResponse:
    require_permission(principal, "doc:write")
    agent = effective_agent(principal, request.agent)
    batch = request.session_batch_id or uuid.uuid4()
    document = await _get_document(
        session, principal.workspace.id, doc_key, for_update=True
    )
    ensure_namespace(principal, document.namespace, write=True)
    onboarding.ensure_system_writable(principal, document.namespace, document.doc_key)
    await budget.check_write(principal, document_id=document.id, doc_key=document.doc_key)
    ensure_not_human_locked(
        principal, "shared_document", document.locked_by_human, document.human_note
    )
    origins = await provenance.collect(
        session, principal, agent=agent, derived_from=request.derived_from,
        content_origin=request.content_origin, exclude_document_id=document.id,
    )
    await enforce_policy(
        session,
        principal,
        resource_type="document",
        key=doc_key,
        action="write",
        payload=document.frontmatter or {},
        content_bytes=len(request.new_markdown.encode("utf-8")),
    )
    rebased = False
    if document.version != request.expected_version:
        if not request.rebase:
            raise VersionConflict(
                "shared_document",
                _latest(document),
                expected_version=request.expected_version,
                include_content=request.return_current_on_conflict,
            )
        base = (
            await session.execute(
                select(DocumentVersion).where(
                    DocumentVersion.document_id == document.id,
                    DocumentVersion.version == request.expected_version,
                )
            )
        ).scalar_one_or_none()
        if base is None:
            raise VersionConflict(
                "shared_document",
                _latest(document),
                expected_version=request.expected_version,
                include_content=request.return_current_on_conflict,
            )
        base_section = extract_section(base.content, request.section_heading)
        current_section = extract_section(
            document.content, request.section_heading
        )
        if base_section != current_section:
            raise RebaseConflict(
                "shared_document",
                [
                    {
                        "field": f"section:{request.section_heading}",
                        "reason": "the target section changed after base_version",
                        "base": base_section,
                        "current": current_section,
                        "proposed": request.new_markdown,
                    }
                ],
            )
        rebased = True
    current_content, current_normalized = normalize_notion_content(document.content)
    if current_normalized:
        document.content = current_content
        document.content_hash = content_hash(current_content)
    new_markdown, _ = normalize_notion_content(request.new_markdown)
    new_markdown = _strip_matching_heading(
        new_markdown, request.section_heading
    )
    before = document_snapshot(document)
    document.content = patch_markdown_section(
        document.content, request.section_heading, new_markdown
    )
    document.content_hash = content_hash(document.content)
    document.verification_status = "DRAFT"
    document.verified_version = None
    document.verified_hash = None
    document.embedding, document.embedding_model = await _document_embedding(
        principal.workspace.id, document.content
    )
    document.version += 1
    document.updated_by = agent
    document.updated_at = utcnow()
    # A section patch keeps the rest of the body, and with it its sources.
    kept = list(document.provenance_taint or [])
    document.provenance_taint = kept + [
        entry for entry in origins.sources if entry not in kept]
    if request.content_origin != "internal":
        document.content_origin = request.content_origin
    ensure_content_size(document.content)
    await session.flush()
    await refresh_document_taint(session, document)
    await indexer.after_write(session, document)
    await _record_version(
        session,
        document,
        author=agent,
        batch_id=batch,
        change_summary=f"section patched: {request.section_heading}"
        + (" with rebase" if rebased else ""),
    )
    await provenance.record_edges(session, document, origins)
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=agent,
        target_table="shared_documents",
        target_record_id=document.id,
        operation="UPDATE",
        before=before,
        after=document_snapshot(document),
    )
    await propagate_from_document(session, document)
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=agent,
        event_type="DOC_SECTION_PATCHED",
        payload={
            "doc_key": doc_key,
            "section": request.section_heading,
            "version": document.version,
            "tainted": document.tainted,
            "rebased": rebased,
        },
        namespace=document.namespace,
    )
    await session.commit()
    return await _mutation_response(session, document, batch)


@access_log.reads("document")
@agent_onboarding.records("read")
async def query_documents(
    session: AsyncSession,
    principal: Principal,
    request: DocumentQueryRequest,
) -> DocumentQueryResponse:
    """Semantic/structured discovery. Not exhaustive: use list_documents to enumerate."""
    require_permission(principal, "query")
    await budget.check_query(principal)
    base = [SharedDocument.workspace_id == principal.workspace.id]
    if request.namespace:
        ensure_namespace(principal, request.namespace)
        base.append(SharedDocument.namespace == request.namespace)
    else:
        allowed = _allowed_namespaces(principal)
        if allowed is not None:
            base.append(SharedDocument.namespace.in_(allowed))
    base.extend(_frontmatter_conditions(request.filters))
    conditions = list(base)
    own = SharedDocument.created_by == principal.agent_signature
    if not request.include_unverified:
        verified = SharedDocument.verification_status == "VERIFIED"
        conditions.append(verified | own if request.include_own_unverified else verified)
    if not request.include_tainted:
        conditions.append(SharedDocument.tainted.is_(False))
    if not request.include_archived:
        conditions.append(SharedDocument.archived_at.is_(None))
    limit = min(request.limit, settings.max_search_top_k)

    if not request.query:
        rows = (
            await session.execute(
                select(SharedDocument)
                .where(*conditions)
                .order_by(SharedDocument.updated_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        items = [_query_hit(principal, row, []) for row in rows]
        await provenance.note_reads(principal, list(rows))
        excluded = await _excluded_counts(
            session, principal, request, base_ids=select(SharedDocument.id).where(*base),
            shown={row.id for row in rows},
        )
        return DocumentQueryResponse(items=items, total=len(items), excluded=excluded)

    ranked = await _rank(session, principal, request, conditions, limit)
    if not ranked:
        excluded = await _excluded_ranked(session, principal, request, base, limit, set())
        return DocumentQueryResponse(
            items=[],
            total=0,
            excluded=excluded,
            index_lag=await indexer.lag(session, principal.workspace.id),
        )
    ids = [doc_id for doc_id, _ in ranked]
    score_by_id = {doc_id: score for doc_id, score in ranked}
    rows = (
        await session.execute(select(SharedDocument).where(SharedDocument.id.in_(ids)))
    ).scalars().all()
    by_id = {row.id: row for row in rows}
    await indexer.annotate(session, list(rows))
    chunks = await _matched_chunks(session, ids, request.query)
    items = [
        _query_hit(principal, by_id[doc_id], chunks.get(doc_id, [])).model_copy(
            update={"score": round(score_by_id[doc_id], 6)}
        )
        for doc_id in ids
        if doc_id in by_id
    ]
    await provenance.note_reads(principal, list(rows))
    excluded = await _excluded_ranked(session, principal, request, base, limit, set(ids))
    return DocumentQueryResponse(
        items=items,
        total=len(items),
        excluded=excluded,
        index_lag=await indexer.lag(session, principal.workspace.id),
    )


async def _rank(
    session: AsyncSession,
    principal: Principal,
    request: DocumentQueryRequest,
    conditions: list[Any],
    limit: int,
) -> list[tuple[uuid.UUID, float]]:
    filter_stmt = select(SharedDocument.id).where(*conditions)
    ranked = await hybrid_document_ids(
        session,
        workspace_id=principal.workspace.id,
        doc_filter=SharedDocument.id.in_(filter_stmt),
        query=request.query or "",
        limit=limit,
        mode=request.mode,
    )
    if ranked:
        return ranked
    # Legacy databases or documents written before chunking existed still
    # have a whole-document embedding; use it as a best-effort fallback.
    vector = await query_vector(request.query or "", principal.workspace.id)
    distance = SharedDocument.embedding.cosine_distance(vector)
    legacy = select(SharedDocument.id).where(*conditions, SharedDocument.embedding.is_not(None))
    if settings.semantic_threshold > 0:
        legacy = legacy.where(distance <= 1 - settings.semantic_threshold)
    legacy_ids = list(
        (await session.execute(legacy.order_by(distance).limit(limit))).scalars().all()
    )
    return [
        (doc_id, 1.0 / (settings.hybrid_rrf_k + rank))
        for rank, doc_id in enumerate(legacy_ids, start=1)
    ]


def _query_hit(
    principal: Principal, document: SharedDocument, chunks: list[MatchedChunk]
) -> DocumentQueryHit:
    return DocumentQueryHit(
        **document_summary(document).model_dump(),
        created_by=document.created_by,
        is_own=document.created_by == principal.agent_signature,
        matched_chunks=chunks,
    )


def _excluded_reason(principal: Principal, document: SharedDocument) -> str | None:
    if document.archived_at is not None:
        return "archived"
    if document.recall_severity == "block":
        return "recalled"
    if document.tainted:
        return "tainted"
    if document.verification_status != "VERIFIED":
        if document.created_by == principal.agent_signature:
            return "own_unverified"
        return "unverified_by_others"
    return None


async def _excluded_counts(
    session: AsyncSession,
    principal: Principal,
    request: DocumentQueryRequest,
    *,
    base_ids: Any,
    shown: set[uuid.UUID],
) -> dict[str, int]:
    rows = (
        await session.execute(
            select(SharedDocument).where(SharedDocument.id.in_(base_ids)).limit(500)
        )
    ).scalars().all()
    return _tally(principal, [row for row in rows if row.id not in shown])


async def _excluded_ranked(
    session: AsyncSession,
    principal: Principal,
    request: DocumentQueryRequest,
    base: list[Any],
    limit: int,
    shown: set[uuid.UUID],
) -> dict[str, int]:
    """How many matching hits the default filters hid (PRD AG-ST-04 point 4)."""
    relaxed = await _rank(session, principal, request, base, limit)
    hidden_ids = [doc_id for doc_id, _ in relaxed if doc_id not in shown]
    if not hidden_ids:
        return _tally(principal, [])
    rows = (
        await session.execute(select(SharedDocument).where(SharedDocument.id.in_(hidden_ids)))
    ).scalars().all()
    return _tally(principal, rows)


def _tally(principal: Principal, rows: list[SharedDocument]) -> dict[str, int]:
    counts = {"unverified_by_others": 0, "own_unverified": 0, "archived": 0, "tainted": 0,
              "recalled": 0}
    for row in rows:
        reason = _excluded_reason(principal, row)
        if reason is not None:
            counts[reason] += 1
    return counts


async def _matched_chunks(
    session: AsyncSession, document_ids: list[uuid.UUID], query: str
) -> dict[uuid.UUID, list[MatchedChunk]]:
    """Up to three chunks per hit: literal matches first, then by similarity."""
    if not document_ids:
        return {}
    vector = await query_vector(query)  # cached by the ranking step
    rows = (
        await session.execute(
            select(DocumentChunk)
            .where(DocumentChunk.document_id.in_(document_ids))
            .order_by(DocumentChunk.embedding.cosine_distance(vector))
        )
    ).scalars().all()
    needle = query.strip().casefold()
    grouped: dict[uuid.UUID, list[DocumentChunk]] = {}
    for row in rows:
        grouped.setdefault(row.document_id, []).append(row)
    result: dict[uuid.UUID, list[MatchedChunk]] = {}
    for doc_id, chunks in grouped.items():
        literal = [chunk for chunk in chunks if needle and needle in chunk.content.casefold()]
        ordered = literal + [chunk for chunk in chunks if chunk not in literal]
        result[doc_id] = [
            MatchedChunk(
                chunk_index=chunk.chunk_index,
                heading_path=chunk.heading,
                snippet=_snippet(chunk.content, needle),
            )
            for chunk in ordered[:3]
        ]
    return result


def _snippet(text: str, needle: str, width: int = 240) -> str:
    position = text.casefold().find(needle) if needle else -1
    if position < 0:
        return text[:width]
    start = max(position - width // 3, 0)
    return text[start : start + width]


@access_log.reads("document")
@agent_onboarding.records_read
async def dereference(
    session: AsyncSession,
    principal: Principal,
    uri: str,
    *,
    include_unverified: bool = False,
    include_tainted: bool = False,
    expand_parts: bool = False,
    content_offset: int = 0,
    content_limit: int | None = None,
    taint_mode: str = "annotate",
) -> DereferenceResponse:
    require_permission(principal, "doc:read")
    doc_key = parse_doc_uri(uri)
    document = await _get_document(session, principal.workspace.id, doc_key)
    ensure_namespace(principal, document.namespace)
    _ensure_document_visible(
        principal,
        document,
        include_unverified=include_unverified,
        include_tainted=include_tainted,
    )
    await indexer.annotate(session, [document])
    await provenance.note_reads(principal, [document])
    if expand_parts:
        expanded, parts = await address_service.expand_parts(session, principal, document)
        return DereferenceResponse(
            uri=uri, document=provenance.apply_taint_mode(document_out(document), taint_mode),
            expanded_content=expanded, parts=parts
        )
    out = document_out(document)
    raw = document.content.encode("utf-8")
    truncated = False
    if content_offset or content_limit is not None:
        start = _char_boundary(raw, min(content_offset, len(raw)))
        end = len(raw) if content_limit is None else _char_boundary(
            raw, min(start + content_limit, len(raw))
        )
        truncated = start > 0 or end < len(raw)
        out = out.model_copy(update={"content": raw[start:end].decode("utf-8")})
    return DereferenceResponse(
        uri=uri,
        document=provenance.apply_taint_mode(out, taint_mode),
        content_total_bytes=len(raw),
        content_truncated=truncated,
    )


def _char_boundary(raw: bytes, index: int) -> int:
    """Move a byte index back to the start of a UTF-8 character."""
    while 0 < index < len(raw) and (raw[index] & 0xC0) == 0x80:
        index -= 1
    return index


async def create_artifact(
    session: AsyncSession,
    *,
    workspace: Workspace,
    task: CollabTask,
    agent: str,
    artifact_text: str,
    risk_flag: bool,
    batch_id: uuid.UUID,
    principal: Principal | None = None,
) -> tuple[SharedDocument, str]:
    await _ensure_document_quota(session, workspace)
    suffix = uuid.uuid4().hex[:10]
    # Server-generated keys follow the address grammar (lowercase segments).
    slug = re.sub(r"[^a-z0-9._-]+", "-", task.task_key.lower()).strip("-.") or "task"
    doc_key = f"artifact/{slug[:64]}/{suffix}"
    artifact_text, _ = normalize_notion_content(artifact_text)
    ensure_content_size(artifact_text)
    frontmatter = {
        "type": "artifact",
        "task_key": task.task_key,
        "status": "FINAL",
        "risk_flag": risk_flag,
    }
    if principal is not None:
        await enforce_policy(
            session,
            principal,
            resource_type="document",
            key=doc_key,
            action="create",
            payload=frontmatter,
            content_bytes=len(artifact_text.encode("utf-8")),
        )
    document = SharedDocument(
        workspace_id=workspace.id,
        doc_key=doc_key,
        title=f"Artifact: {task.title}"[:256],
        content=artifact_text,
        content_hash=content_hash(artifact_text),
        frontmatter=frontmatter,
        verification_status="DRAFT",
        trust_level="UNTRUSTED",
        verified_version=None,
        verified_hash=None,
        embedding=None,
        embedding_model=None,
        namespace=task.namespace,
        version=1,
        locked_by_human=False,
        human_note=None,
        tainted=False,
        taint_sources=[],
        created_by=agent,
        updated_by=agent,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(document)
    await session.flush()
    await refresh_document_taint(session, document)
    if not indexer.asynchronous():
        await enforce_embedding_budget(
            session, workspace.id, len(artifact_text.encode("utf-8"))
        )
    await indexer.after_write(session, document)
    await _record_version(
        session,
        document,
        author=agent,
        batch_id=batch_id,
        change_summary="task handoff artifact",
    )
    await record_audit(
        session,
        workspace_id=workspace.id,
        session_batch_id=batch_id,
        agent_signature=agent,
        target_table="shared_documents",
        target_record_id=document.id,
        operation="INSERT",
        before={},
        after=document_snapshot(document),
    )
    return document, f"doc://{doc_key}"


async def human_update_document(
    session: AsyncSession,
    workspace: Workspace,
    doc_key: str,
    request: DocumentHumanUpdateRequest,
    *,
    actor: str = "human_commander",
) -> DocumentMutationResponse:
    batch = request.session_batch_id or uuid.uuid4()
    document = await _get_document(
        session, workspace.id, doc_key, for_update=True
    )
    if document.version != request.expected_version:
        raise VersionConflict("shared_document", _latest(document))
    await enforce_policy(
        session,
        principal_for_admin(workspace),
        resource_type="document",
        key=doc_key,
        action="write",
        payload=request.frontmatter or document.frontmatter or {},
        content_bytes=(
            len(request.content.encode("utf-8"))
            if request.content is not None
            else None
        ),
    )
    before = document_snapshot(document)
    if request.title is not None:
        document.title = request.title
    if request.content is not None:
        normalized_content, _ = normalize_notion_content(request.content)
        ensure_content_size(normalized_content)
        new_hash = content_hash(normalized_content)
        if new_hash != (document.content_hash or content_hash(document.content)):
            document.verification_status = "DRAFT"
            document.verified_version = None
            document.verified_hash = None
        document.content = normalized_content
        document.content_hash = new_hash
        document.embedding, document.embedding_model = await _document_embedding(
            workspace.id, normalized_content
        )
    if request.frontmatter is not None:
        document.frontmatter = request.frontmatter
    if request.unlock:
        document.locked_by_human = False
    else:
        document.locked_by_human = (
            request.lock if request.lock is not None else True
        )
    if request.human_note is not None:
        document.human_note = request.human_note
    document.version += 1
    document.updated_by = actor
    document.updated_at = utcnow()
    await session.flush()
    await refresh_document_taint(session, document)
    await indexer.after_write(session, document)
    await _record_version(
        session,
        document,
        author=actor,
        batch_id=batch,
        change_summary=(
            "human edit" if request.content is not None else "human metadata edit"
        ),
    )
    await record_audit(
        session,
        workspace_id=workspace.id,
        session_batch_id=batch,
        agent_signature=actor,
        target_table="shared_documents",
        target_record_id=document.id,
        operation="UPDATE",
        before=before,
        after=document_snapshot(document),
    )
    await propagate_from_document(session, document)
    await append_event(
        session,
        workspace_id=workspace.id,
        agent=actor,
        event_type="HUMAN_OVERRIDE",
        payload={
            "resource": "shared_document",
            "doc_key": doc_key,
            "version": document.version,
            "locked_by_human": document.locked_by_human,
            "note": request.human_note,
        },
        namespace=document.namespace,
    )
    await session.commit()
    return DocumentMutationResponse(
        document=document_out(document), session_batch_id=batch
    )


async def bind_task_artifact(
    session: AsyncSession,
    principal: Principal,
    task: CollabTask,
    doc_key: str,
) -> tuple[int, str]:
    """Validate a handed-off ``doc://`` artifact and return its (version, hash).

    The artifact must exist in the task's namespace and be readable by the
    handing-off principal; review approval later verifies only this exact
    version, so edits made after the handoff are never verified implicitly.
    """
    require_permission(principal, "doc:read")
    document = await _get_document(session, principal.workspace.id, doc_key)
    ensure_namespace(principal, document.namespace)
    if document.namespace != task.namespace:
        raise Forbidden(
            "artifact_uri must reference a document in the task's namespace "
            f"('{task.namespace}')"
        )
    if document.archived_at is not None:
        raise ApiError(
            409, "artifact_archived", "artifact_uri references an archived document"
        )
    return document.version, document.content_hash or content_hash(document.content)


async def verify_artifact_for_review(
    session: AsyncSession,
    principal: Principal,
    doc_key: str,
    *,
    actor: str,
    batch: uuid.UUID,
    task_key: str,
    note: str | None = None,
    expected_version: int | None = None,
    expected_hash: str | None = None,
    require_binding: bool = True,
) -> bool:
    """Mark a reviewed task artifact VERIFIED without committing the transaction.

    Tainted artifacts stay untrusted and require an explicit human verification.
    The review itself must not fail if the artifact no longer exists. Unless
    ``require_binding`` is disabled (admin repair), only the version and hash
    pinned at handoff are verified; a changed artifact stays unverified.
    """
    if not can(principal, "doc:verify"):
        return False
    if require_binding and (expected_version is None or expected_hash is None):
        return False
    try:
        document = await _get_document(
            session, principal.workspace.id, doc_key, for_update=True
        )
    except NotFound:
        return False
    ensure_namespace(principal, document.namespace, write=True)
    if document.tainted:
        return False
    if require_binding:
        current_hash = document.content_hash or content_hash(document.content)
        if document.version != expected_version or not hmac.compare_digest(
            current_hash, expected_hash or ""
        ):
            return False
    before = document_snapshot(document)
    digest = document.content_hash or content_hash(document.content)
    document.content_hash = digest
    document.verification_status = "VERIFIED"
    document.trust_level = "TRUSTED"
    document.verified_version = document.version
    document.verified_hash = digest
    document.updated_by = actor
    document.updated_at = utcnow()
    await session.flush()
    latest_version = (
        await session.execute(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.version == document.version,
            )
        )
    ).scalar_one_or_none()
    if latest_version is not None:
        latest_version.verification_status = "VERIFIED"
        latest_version.trust_level = "TRUSTED"
        latest_version.tainted = document.tainted
        latest_version.taint_sources = document.taint_sources or []
        latest_version.content_hash = digest
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=actor,
        target_table="shared_documents",
        target_record_id=document.id,
        operation="UPDATE",
        before=before,
        after=document_snapshot(document),
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=actor,
        event_type="DOC_VERIFIED",
        payload={
            "doc_key": doc_key,
            "version": document.version,
            "content_hash": digest,
            "note": note,
            "task_key": task_key,
            "source": "task_review",
        },
        namespace=document.namespace,
    )
    await recompute_downstream_document_taint(
        session, document.workspace_id, document.id
    )
    return True


async def verify_document(
    session: AsyncSession,
    principal: Principal,
    doc_key: str,
    request: DocumentVerifyRequest,
) -> DocumentMutationResponse:
    require_permission(principal, "doc:verify")
    batch = request.session_batch_id or uuid.uuid4()
    agent = principal.agent_signature
    document = await _get_document(
        session, principal.workspace.id, doc_key, for_update=True
    )
    ensure_namespace(principal, document.namespace, write=True)
    onboarding.ensure_system_writable(principal, document.namespace, document.doc_key)
    ensure_not_human_locked(
        principal, "shared_document", document.locked_by_human, document.human_note
    )
    if document.version != request.expected_version:
        raise VersionConflict("shared_document", _latest(document))
    before = document_snapshot(document)
    if document.tainted:
        if not principal.is_human:
            raise TaintBlocked(doc_key, document.taint_sources or [])
        await clear_document_taint(
            session, document, actor=agent, note=request.note
        )
    document.content_hash = content_hash(document.content)
    document.verification_status = "VERIFIED"
    document.trust_level = "TRUSTED"
    document.verified_version = document.version
    document.verified_hash = document.content_hash
    if request.note:
        document.human_note = request.note
    document.updated_by = agent
    document.updated_at = utcnow()
    await session.flush()
    latest_version = (
        await session.execute(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.version == document.version,
            )
        )
    ).scalar_one_or_none()
    if latest_version is not None:
        latest_version.verification_status = "VERIFIED"
        latest_version.trust_level = "TRUSTED"
        latest_version.tainted = document.tainted
        latest_version.taint_sources = document.taint_sources or []
        latest_version.content_hash = document.content_hash
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=agent,
        target_table="shared_documents",
        target_record_id=document.id,
        operation="UPDATE",
        before=before,
        after=document_snapshot(document),
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=agent,
        event_type="DOC_VERIFIED",
        payload={
            "doc_key": doc_key,
            "version": document.version,
            "content_hash": document.content_hash,
            "note": request.note,
            "tainted": document.tainted,
            "taint_cleared": before.get("tainted", False),
        },
        namespace=document.namespace,
    )
    await recompute_downstream_document_taint(
        session, document.workspace_id, document.id
    )
    await session.commit()
    return DocumentMutationResponse(
        document=document_out(document), session_batch_id=batch
    )


async def set_document_status(
    session: AsyncSession,
    principal: Principal,
    doc_key: str,
    request: DocumentStatusRequest,
    *,
    action: str,
) -> DocumentMutationResponse:
    if action == "submit_review":
        require_permission(principal, "doc:write")
        new_status = "PENDING_REVIEW"
    elif action == "deprecate":
        require_permission(principal, "doc:verify")
        new_status = "DEPRECATED"
    else:
        raise NotFound("document_action", f"unknown document action '{action}'")
    batch = request.session_batch_id or uuid.uuid4()
    agent = principal.agent_signature
    document = await _get_document(
        session, principal.workspace.id, doc_key, for_update=True
    )
    ensure_namespace(principal, document.namespace, write=True)
    onboarding.ensure_system_writable(principal, document.namespace, document.doc_key)
    ensure_not_human_locked(
        principal, "shared_document", document.locked_by_human, document.human_note
    )
    if document.version != request.expected_version:
        raise VersionConflict("shared_document", _latest(document))
    before = document_snapshot(document)
    document.verification_status = new_status
    if request.note:
        document.human_note = request.note
    document.updated_by = agent
    document.updated_at = utcnow()
    await session.flush()
    latest_version = (
        await session.execute(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.version == document.version,
            )
        )
    ).scalar_one_or_none()
    if latest_version is not None:
        latest_version.verification_status = new_status
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=agent,
        target_table="shared_documents",
        target_record_id=document.id,
        operation="UPDATE",
        before=before,
        after=document_snapshot(document),
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=agent,
        event_type="DOC_STATUS_CHANGED",
        payload={
            "doc_key": doc_key,
            "version": document.version,
            "status": new_status,
            "note": request.note,
        },
        namespace=document.namespace,
    )
    await session.commit()
    return DocumentMutationResponse(
        document=document_out(document), session_batch_id=batch
    )


async def list_document_versions(
    session: AsyncSession,
    principal: Principal,
    doc_key: str,
    *,
    include_unverified: bool = False,
    include_tainted: bool = False,
) -> DocumentVersionListResponse:
    require_permission(principal, "doc:read")
    document = await _get_document(session, principal.workspace.id, doc_key)
    ensure_namespace(principal, document.namespace)
    _ensure_document_visible(
        principal,
        document,
        include_unverified=include_unverified,
        include_tainted=include_tainted,
    )
    versions = (
        await session.execute(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document.id)
            .order_by(DocumentVersion.version.desc())
        )
    ).scalars().all()
    return DocumentVersionListResponse(
        versions=[_version_summary(version) for version in versions]
    )


@access_log.reads("document")
async def get_document_version(
    session: AsyncSession,
    principal: Principal,
    doc_key: str,
    version_number: int,
    *,
    include_unverified: bool = False,
    include_tainted: bool = False,
) -> DocumentVersionOut:
    require_permission(principal, "doc:read")
    document = await _get_document(session, principal.workspace.id, doc_key)
    ensure_namespace(principal, document.namespace)
    _ensure_document_visible(
        principal,
        document,
        include_unverified=include_unverified,
        include_tainted=include_tainted,
    )
    version = (
        await session.execute(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.version == version_number,
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise NotFound("document_version", f"version {version_number} not found")
    return _version_out(version)


async def set_document_taint(
    session: AsyncSession,
    principal: Principal,
    doc_key: str,
    request: DocumentTaintRequest,
) -> DocumentMutationResponse:
    if not principal.is_human or not principal.is_commander:
        raise Forbidden("a human owner/reviewer must change taint state")
    batch = uuid.uuid4()
    document = await _get_document(
        session, principal.workspace.id, doc_key, for_update=True
    )
    ensure_namespace(principal, document.namespace, write=True)
    if (
        request.expected_version is not None
        and request.expected_version != document.version
    ):
        raise VersionConflict("shared_document", _latest(document))
    before = document_snapshot(document)
    await mark_document_taint(
        session,
        document,
        tainted=request.tainted,
        sources=request.sources,
        actor=principal.agent_signature,
        note=request.note,
    )
    document.updated_by = principal.agent_signature
    document.updated_at = utcnow()
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=principal.agent_signature,
        target_table="shared_documents",
        target_record_id=document.id,
        operation="UPDATE",
        before=before,
        after=document_snapshot(document),
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=principal.agent_signature,
        event_type="DOC_TAINT_CHANGED",
        payload={
            "doc_key": doc_key,
            "tainted": document.tainted,
            "sources": document.taint_sources or [],
            "note": request.note,
        },
        namespace=document.namespace,
    )
    await session.commit()
    return DocumentMutationResponse(
        document=document_out(document), session_batch_id=batch
    )

async def clear_taint(
    session: AsyncSession,
    principal: Principal,
    doc_key: str,
    *,
    namespace: str | None,
    version: int | None,
    reason: str,
) -> DocumentMutationResponse:
    """AG-TC-01 point 3: only people clear taint, as a new untainted version."""
    if not principal.is_human:
        raise Forbidden("only a human can clear taint")
    batch = uuid.uuid4()
    document = await _get_document(session, principal.workspace.id, doc_key, for_update=True)
    if namespace and document.namespace != namespace:
        raise NotFound("shared_document", f"document '{doc_key}' not found in '{namespace}'")
    ensure_namespace(principal, document.namespace, write=True)
    if version is not None and version != document.version:
        raise VersionConflict("shared_document", _latest(document), expected_version=version)
    before = document_snapshot(document)
    actor = principal.agent_signature
    document.version += 1
    document.updated_by = actor
    document.updated_at = utcnow()
    await clear_document_taint(session, document, actor=actor, note=None)
    await _record_version(session, document, author=actor, batch_id=batch,
                          change_summary=f"taint cleared: {reason}"[:500])
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=actor,
        target_table="shared_documents",
        target_record_id=document.id,
        operation="UPDATE",
        before=before,
        after={**document_snapshot(document), "reason": reason},
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=actor,
        event_type="DOC_TAINT_CHANGED",
        payload={"doc_key": document.doc_key, "tainted": False, "version": document.version,
                 "reason": reason},
        namespace=document.namespace,
    )
    await session.commit()
    return DocumentMutationResponse(document=document_out(document), session_batch_id=batch)


async def normalize_document_html(
    session: AsyncSession,
    workspace: Workspace,
    doc_key: str,
    *,
    actor: str = "human_commander",
) -> DocumentNormalizeResponse:
    """Convert legacy Notion/HTML content to Markdown and record a version."""
    document = await _get_document(session, workspace.id, doc_key, for_update=True)
    content, converted = normalize_notion_content(document.content)
    if not converted:
        return DocumentNormalizeResponse(
            converted=False, document=document_out(document)
        )
    batch = uuid.uuid4()
    before = document_snapshot(document)
    document.content = content
    document.content_hash = content_hash(content)
    document.verification_status = "DRAFT"
    document.verified_version = None
    document.verified_hash = None
    document.embedding, document.embedding_model = await _document_embedding(
        workspace.id, content
    )
    document.version += 1
    document.updated_by = actor
    document.updated_at = utcnow()
    await session.flush()
    await refresh_document_taint(session, document)
    await indexer.after_write(session, document)
    await _record_version(
        session,
        document,
        author=actor,
        batch_id=batch,
        change_summary="normalized Notion/HTML to Markdown",
    )
    await record_audit(
        session,
        workspace_id=workspace.id,
        session_batch_id=batch,
        agent_signature=actor,
        target_table="shared_documents",
        target_record_id=document.id,
        operation="UPDATE",
        before=before,
        after=document_snapshot(document),
    )
    await propagate_from_document(session, document)
    await append_event(
        session,
        workspace_id=workspace.id,
        agent=actor,
        event_type="DOC_HTML_NORMALIZED",
        payload={"doc_key": doc_key, "version": document.version},
        namespace=document.namespace,
    )
    await session.commit()
    return DocumentNormalizeResponse(
        converted=True, document=document_out(document)
    )


async def normalize_workspace_html_documents(
    session: AsyncSession,
    workspace: Workspace,
    *,
    actor: str = "human_commander",
    limit: int = 1_000,
) -> int:
    documents = (
        await session.execute(
            select(SharedDocument)
            .where(SharedDocument.workspace_id == workspace.id)
            .limit(limit)
        )
    ).scalars().all()
    converted = 0
    for document in documents:
        result = await normalize_document_html(
            session, workspace, document.doc_key, actor=actor
        )
        if result.converted:
            converted += 1
    return converted


async def backfill_verified_artifacts(
    session: AsyncSession,
    principal: Principal,
    workspace_id: uuid.UUID,
) -> dict[str, int]:
    """Mark completed task artifacts verified, clearing downstream taint.

    This is an idempotent admin repair for artifacts approved before automatic
    verification existed. It runs multiple passes so a downstream document that
    was waiting on upstream verification can be verified in a later pass.
    """
    require_permission(principal, "doc:verify")
    tasks = (
        await session.execute(
            select(CollabTask).where(
                CollabTask.workspace_id == workspace_id,
                CollabTask.status == TaskStatus.DONE,
                CollabTask.artifact_uri.is_not(None),
            )
        )
    ).scalars().all()
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for task in tasks:
        if not task.artifact_uri or not task.artifact_uri.startswith("doc://"):
            continue
        doc_key = task.artifact_uri.removeprefix("doc://")
        if doc_key in seen:
            continue
        seen.add(doc_key)
        entries.append((doc_key, task.task_key))

    verified: set[str] = set()
    missing: set[str] = set()
    tainted: set[str] = set()
    already: set[str] = set()
    for _ in range(3):
        progress = False
        for doc_key, task_key in entries:
            document = await session.scalar(
                select(SharedDocument).where(
                    SharedDocument.workspace_id == workspace_id,
                    SharedDocument.doc_key == doc_key,
                )
            )
            if document is None:
                missing.add(doc_key)
                continue
            if document.verification_status == "VERIFIED":
                already.add(doc_key)
                continue
            if document.tainted:
                tainted.add(doc_key)
                continue
            ok = await verify_artifact_for_review(
                session,
                principal,
                doc_key,
                actor=principal.agent_signature,
                batch=uuid.uuid4(),
                task_key=task_key,
                note="admin backfill of approved task artifact",
                require_binding=False,
            )
            if ok:
                verified.add(doc_key)
                tainted.discard(doc_key)
                progress = True
        if not progress:
            break
    await session.commit()
    return {
        "candidates": len(entries),
        "verified": len(verified),
        "already_verified": len(already),
        "tainted_skipped": len(tainted),
        "missing": len(missing),
    }


async def rename_document(
    session: AsyncSession,
    principal: Principal,
    ref: str,
    request: DocumentRenameRequest,
) -> DocumentMutationResponse:
    """Change a document's alias; the id stays and the old alias redirects forever."""
    require_permission(principal, "doc:write")
    document = await _get_document(session, principal.workspace.id, ref, for_update=True)
    ensure_namespace(principal, document.namespace, write=True)
    if not _can_manage_document(principal, document):
        raise Forbidden("only the creator or a workspace commander can rename this document")
    ensure_not_human_locked(
        principal, "shared_document", document.locked_by_human, document.human_note
    )
    if request.expected_version is not None and document.version != request.expected_version:
        raise VersionConflict("shared_document", _latest(document))
    new_alias = request.new_alias.strip()
    onboarding.ensure_system_writable(principal, document.namespace, document.doc_key)
    onboarding.ensure_system_writable(principal, document.namespace, new_alias)
    violation = doc_key_violation(new_alias)
    if violation is not None:
        raise address_service.invalid_address(violation)
    if new_alias == document.doc_key:
        return DocumentMutationResponse(
            document=document_out(document),
            session_batch_id=request.session_batch_id or uuid.uuid4(),
        )
    taken = await address_service.find_document(session, principal.workspace.id, new_alias)
    if taken is not None and taken.id != document.id:
        raise ApiError(
            409,
            "alias_taken",
            f"document alias '{new_alias}' already exists",
            details={"alias": new_alias, "id": taken.canonical_id},
        )
    await address_service.ensure_alias_available(
        session, principal.workspace.id, "document", new_alias
    )
    batch = request.session_batch_id or uuid.uuid4()
    before = document_snapshot(document)
    old_alias = document.doc_key
    document.doc_key = new_alias
    document.alias_legacy = is_legacy_alias(document.namespace, new_alias)
    document.updated_by = principal.agent_signature
    document.updated_at = utcnow()
    await address_service.add_redirect(
        session,
        principal.workspace.id,
        "document",
        old_alias,
        document.canonical_id,
        principal.agent_signature,
    )
    await session.execute(
        update(DocumentChunk)
        .where(DocumentChunk.document_id == document.id)
        .values(doc_key=new_alias)
    )
    await session.execute(
        update(DocumentProposal)
        .where(
            DocumentProposal.workspace_id == principal.workspace.id,
            DocumentProposal.doc_key == old_alias,
        )
        .values(doc_key=new_alias)
    )
    await session.flush()
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=principal.agent_signature,
        target_table="shared_documents",
        target_record_id=document.id,
        operation="UPDATE",
        before=before,
        after=document_snapshot(document),
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=principal.agent_signature,
        event_type="DOC_RENAMED",
        payload={
            "id": document.canonical_id,
            "doc_key": new_alias,
            "previous_alias": old_alias,
        },
        namespace=document.namespace,
    )
    await session.commit()
    return DocumentMutationResponse(document=document_out(document), session_batch_id=batch)


async def purge_document(
    session: AsyncSession,
    principal: Principal,
    ref: str,
    request: DocumentPurgeRequest,
) -> TombstoneOut:
    """Physically delete an archived document, leaving a tombstone (human only)."""
    if not principal.is_human or not principal.is_commander:
        raise Forbidden("only a human owner or reviewer can purge documents")
    document = await _get_document(session, principal.workspace.id, ref, for_update=True)
    if request.confirm != document.doc_key:
        raise ApiError(
            409,
            "workspace_confirmation_mismatch",
            "confirm must exactly match the document alias",
            details={"alias": document.doc_key},
        )
    if document.archived_at is None:
        raise ApiError(
            409,
            "retention_not_met",
            "archive the document before purging it",
            details={"id": document.canonical_id, "action": "archive_document"},
        )
    tombstone = await address_service.tombstone_document(
        session, document, purged_by=principal.agent_signature, reason=request.reason
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=principal.agent_signature,
        event_type="DOC_PURGED",
        payload={"id": document.canonical_id, "doc_key": document.doc_key},
        namespace=document.namespace,
    )
    await session.delete(document)
    await session.commit()
    return TombstoneOut(
        id=tombstone.canonical_id,
        alias=tombstone.alias,
        type=tombstone.object_type,
        last_version=tombstone.last_version,
        last_content_hash=tombstone.last_content_hash,
        purged_by=tombstone.purged_by,
        purged_at=tombstone.purged_at,
        reason=tombstone.reason,
    )


async def register_parts(
    session: AsyncSession,
    principal: Principal,
    parent_ref: str,
    request: RegisterPartsRequest,
) -> dict[str, Any]:
    """Register existing documents as ordered parts of a parent (PRD AG-ST-03).

    Only frontmatter changes (parent ``parts``, each part ``part_of`` and
    ``part_index``); no content moves, so references and history stay intact.
    """
    require_permission(principal, "doc:write")
    batch = request.session_batch_id or uuid.uuid4()
    parent = await _get_document(session, principal.workspace.id, parent_ref)
    parts = [
        await _get_document(session, principal.workspace.id, ref) for ref in request.part_refs
    ]
    if any(part.id == parent.id for part in parts):
        raise ApiError(422, "validation_error", "a document cannot be its own part",
                       details={"field": "part_refs"})
    for index, part in enumerate(parts):
        frontmatter = {
            **(part.frontmatter or {}),
            "part_of": {"$ref": parent.canonical_id},
            "part_index": index,
        }
        await upsert_document(
            session,
            principal,
            part.doc_key,
            DocumentUpsertRequest(
                content=part.content,
                title=part.title,
                frontmatter=frontmatter,
                expected_version=part.version,
                namespace=part.namespace,
                session_batch_id=batch,
            ),
            commit=False,
        )
    parent_frontmatter = {
        **(parent.frontmatter or {}),
        "parts": [
            {"$ref": part.canonical_id, "index": index} for index, part in enumerate(parts)
        ],
    }
    result = await upsert_document(
        session,
        principal,
        parent.doc_key,
        DocumentUpsertRequest(
            content=parent.content,
            title=parent.title,
            frontmatter=parent_frontmatter,
            expected_version=parent.version,
            namespace=parent.namespace,
            session_batch_id=batch,
        ),
        commit=False,
    )
    await session.commit()
    return {
        "parent": result.document.model_dump(mode="json"),
        "parts": [
            {"index": index, "id": part.canonical_id, "alias": part.doc_key}
            for index, part in enumerate(parts)
        ],
        "session_batch_id": str(batch),
    }

