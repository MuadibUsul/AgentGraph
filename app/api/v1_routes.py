"""v3.2.1 tenant/agent API: tasks, state grid, documents, links, timeline."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app import agent_guide, services
from app.auth import Principal, ensure_namespace, get_principal, require_permission
from app.database import get_db
from app.error_registry import protocol_docs
from app.errors import ApiError
from app.models import TaskStatus
from app.schemas import (
    AuditListResponse,
    ConstitutionAckRequest,
    DereferenceBatchRequest,
    DereferenceBatchResponse,
    DereferenceRequest,
    DereferenceResponse,
    DocumentArchiveRequest,
    DocumentBatchUpsertRequest,
    DocumentBatchUpsertResponse,
    DocumentListResponse,
    DocumentMutationResponse,
    DocumentOut,
    DocumentQueryRequest,
    DocumentQueryResponse,
    DocumentRenameRequest,
    DocumentSectionPatchRequest,
    DocumentStatusRequest,
    DocumentTaintRequest,
    DocumentUpsertRequest,
    DocumentVerifyRequest,
    DocumentVersionListResponse,
    DocumentVersionOut,
    ExportRequest,
    ExternalActionCreateRequest,
    ExternalActionListResponse,
    ExternalActionOut,
    LeaseHeartbeatRequest,
    LeaseHeartbeatResponse,
    LeaseListResponse,
    LeaseOut,
    LeaseReleaseRequest,
    LinkCreateRequest,
    LinkListResponse,
    LinkOut,
    NamespaceRegisterRequest,
    PolicyListResponse,
    ProposalApplyRequest,
    ProposalApplyResponse,
    ProposalCreateRequest,
    ProposalListResponse,
    ProposalOut,
    ProposalRejectRequest,
    ProposalVoteRequest,
    RegisterPartsRequest,
    SlotListResponse,
    SlotMutationResponse,
    SlotOut,
    SlotPatchRequest,
    SlotReplaceRequest,
    SubscriptionRequest,
    TaskActionResponse,
    TaskClaimRequest,
    TaskContextResponse,
    TaskCreateRequest,
    TaskEscalateRequest,
    TaskHandoffRequest,
    TaskListResponse,
    TaskOut,
    TaskReleaseRequest,
    TaskRenewRequest,
    TaskReviewRequest,
    TimelineEventRequest,
    TimelineItemOut,
    TimelineListResponse,
    UsageOut,
    WhoAmIResponse,
    WorkspaceMapResponse,
    WorkspaceStatsResponse,
)
from app.services import addresses as address_service
from app.services import budget as budget_service
from app.services import events as event_service
from app.services import exports as export_service
from app.services import indexer
from app.services import leases as lease_service
from app.services import lineage as lineage_service
from app.services import orientation as orientation_service

router = APIRouter(prefix="/v1", tags=["AgentGraph v3"])


def _statuses(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [item.strip().upper() for item in value.split(",") if item.strip()]
    valid = {status.value for status in TaskStatus}
    invalid = [item for item in items if item not in valid]
    if invalid:
        raise ApiError(
            422,
            "invalid_task_status",
            f"unknown task status: {', '.join(invalid)}",
        )
    return items or None


@router.get(
    "/orientation",
    summary="First call: identity, constitution, namespaces, tasks and next calls",
)
async def orientation(
    sections: str | None = Query(default=None, description="Comma-separated section names"),
    max_bytes: int = Query(default=orientation_service.DEFAULT_MAX_BYTES),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    wanted = [item.strip() for item in sections.split(",") if item.strip()] if sections else None
    return await orientation_service.build(
        session, principal, sections=wanted, max_bytes=max_bytes
    )


@router.post("/orientation/ack", summary="Record that you read this constitution version")
async def ack_constitution(
    body: ConstitutionAckRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await orientation_service.ack_constitution(session, principal, body.version)


@router.get("/whoami", response_model=WhoAmIResponse, summary="Principal identity")
async def whoami(principal: Principal = Depends(get_principal)) -> WhoAmIResponse:
    return await services.whoami(principal)


@router.get(
    "/workspace/map",
    response_model=WorkspaceMapResponse,
    summary="L0 workspace map: tasks, slot/doc tree, enums (no raw payloads)",
)
@router.get(
    "/sync",
    response_model=WorkspaceMapResponse,
    summary="Map (since=null) or changes after an event cursor (AG-EV-01)",
)
async def workspace_map(
    since: str | None = Query(default=None),
    include_unverified: bool = Query(default=False),
    include_tainted: bool = Query(default=False),
    namespaces: list[str] | None = Query(default=None),
    include_archived: bool = Query(default=False),
    limit: int | None = Query(default=None, ge=1, le=2_000),
    page_cursor: str | None = Query(default=None),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> WorkspaceMapResponse:
    return await services.workspace_map(
        session,
        principal,
        since=since,
        include_unverified=include_unverified,
        include_tainted=include_tainted,
        namespaces=namespaces,
        include_archived=include_archived,
        limit=limit,
        page_cursor=page_cursor,
    )


@router.get(
    "/events",
    summary="poll_events: events after a cursor, optionally waiting up to 25 s (AG-EV-01)",
)
async def poll_events(
    cursor: str | None = Query(default=None, max_length=256),
    subscription_id: str | None = Query(default=None, max_length=40),
    types: list[str] | None = Query(default=None),
    namespaces: list[str] | None = Query(default=None),
    for_me: bool = Query(default=False),
    written_by_subtree: str | None = Query(default=None, max_length=40),
    wait_seconds: int = Query(default=0, ge=0, le=100),
    limit: int = Query(default=100, ge=1, le=100_000),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    # A long poll must not hold this request's connection while it waits.
    await session.close()
    return await event_service.poll_events(
        principal, cursor=cursor, subscription_id=subscription_id, types=types,
        namespaces=namespaces, for_me=for_me, written_by_subtree=written_by_subtree,
        wait_seconds=wait_seconds, limit=limit,
    )


@router.post("/subscriptions", status_code=201,
             summary="Subscribe to namespace/doc_key prefixes (AG-EV-02)")
async def subscribe(
    body: SubscriptionRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await event_service.subscribe(
        session, principal, prefixes=body.prefixes, types=body.types, name=body.name
    )


@router.get("/subscriptions", summary="Your subscriptions")
async def list_subscriptions(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await event_service.list_subscriptions(session, principal)


@router.delete("/subscriptions/{subscription_id}", status_code=204,
               summary="Remove a subscription")
async def delete_subscription(
    subscription_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> Response:
    await event_service.delete_subscription(session, principal, subscription_id)
    return Response(status_code=204)


@router.get(
    "/agent-docs",
    summary="Agent-readable API contract, connection guide and OpenAPI schema",
)
async def agent_docs(
    request: Request,
    format: str = Query(default="json", pattern="^(json|markdown)$"),
    principal: Principal = Depends(get_principal),
):
    """Read-only API/MCP documentation available to every workspace token."""
    if format == "markdown":
        return PlainTextResponse(agent_guide.render(), media_type="text/markdown")
    openapi = request.app.openapi()
    paths = _agent_paths(openapi)
    base = str(request.base_url).rstrip("/")
    return {
        "name": "AgentGraph",
        "version": openapi.get("info", {}).get("version"),
        "base_url": f"{base}/v1",
        "mcp_endpoint": f"{base}/mcp/",
        "authentication": {
            "rest": "Authorization: Bearer <workspace-or-agent-key>",
            "mcp": "Authorization: Bearer <workspace-or-agent-key> (streamable HTTP)",
            "admin": "Admin Basic auth is separate and not required for agent tools.",
        },
        "connection_guide": [
            "Call GET /v1/whoami to confirm workspace, role, namespaces and permissions.",
            "Call GET /v1/workspace/map for L0 counts and keys; use GET /v1/documents "
            "to enumerate a namespace completely with cursor pagination.",
            "Use POST /v1/query for semantic/structured discovery and "
            "GET /v1/documents/{doc_key} or POST /v1/dereference for L2 content.",
            "Write documents with PUT /v1/documents/{doc_key} or "
            "POST /v1/documents/batch (up to 25, all-or-nothing).",
            "Agent-created documents start as DRAFT/UNTRUSTED; your own drafts are "
            "visible to you in queries, other agents' unverified drafts are not.",
            "Use POST /v1/documents/{doc_key}/archive and /restore instead of hard deletion.",
            "Poll incrementally with GET /v1/workspace/map?since=<next_cursor>.",
        ],
        "conventions": {
            "errors": protocol_docs("summary")["error_shape"],
            "protocol": protocol_docs("summary"),
            "error_codes": protocol_docs("errors")["codes"],
            "pagination": (
                "Pass next_cursor back as cursor; "
                "next_cursor=null means the listing is complete."
            ),
            "version_conflict": "The 409 details include current_version, current_hash and latest.",
            "batch_limits": {"upsert": 25, "dereference": 25},
        },
        "guide": agent_guide.render(),
        "limits": agent_guide.limits(),
        "trust_defaults": agent_guide.trust_defaults(),
        "openapi": {
            "openapi": openapi.get("openapi"),
            "info": openapi.get("info"),
            "components": openapi.get("components"),
            "paths": paths,
        },
    }


def _agent_paths(openapi: dict) -> dict:
    return {
        path: operation
        for path, operation in openapi.get("paths", {}).items()
        if path.startswith("/v1") and not path.startswith("/v1/admin")
    }


@router.get(
    "/openapi.json",
    summary="Read-only OpenAPI document for agents (admin endpoints removed)",
)
async def agent_openapi(
    request: Request,
    principal: Principal = Depends(get_principal),
) -> dict:
    openapi = request.app.openapi()
    return {**openapi, "paths": _agent_paths(openapi)}


# ------------------------------------------------------------------ tasks


@router.get("/tasks", response_model=TaskListResponse, summary="List tasks")
async def list_tasks(
    status: str | None = Query(default=None, description="Comma-separated statuses"),
    assigned_role: str | None = Query(default=None),
    namespace: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> TaskListResponse:
    return await services.list_tasks(
        session,
        principal,
        statuses=_statuses(status),
        assigned_role=assigned_role,
        namespace=namespace,
        limit=limit,
    )


@router.post(
    "/tasks", response_model=TaskOut, status_code=201, summary="Create a task"
)
async def create_task(
    body: TaskCreateRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> TaskOut:
    return await services.create_task(session, principal, body)


@router.get("/tasks/{task_key}", response_model=TaskOut, summary="Read a task")
async def get_task(
    task_key: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> TaskOut:
    return await services.get_task(session, principal, task_key)


@router.post(
    "/tasks/{task_key}/claim",
    response_model=TaskContextResponse,
    summary="Claim a task with a TTL lease and load its L1 context",
)
async def claim_task(
    task_key: str,
    body: TaskClaimRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> TaskContextResponse:
    return await services.claim_task(session, principal, task_key, body)


@router.post(
    "/tasks/{task_key}/renew",
    response_model=TaskOut,
    summary="Renew a task lease heartbeat",
)
async def renew_task(
    task_key: str,
    body: TaskRenewRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> TaskOut:
    return await services.renew_task(session, principal, task_key, body)


@router.post(
    "/leases/{lease_id}/heartbeat",
    response_model=LeaseHeartbeatResponse,
    summary="Extend a lease to now + ttl; send every heartbeat_interval_s (AG-CO-03)",
)
async def lease_heartbeat(
    lease_id: str,
    body: LeaseHeartbeatRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> LeaseHeartbeatResponse:
    return await lease_service.heartbeat(session, principal, lease_id, body)


@router.post(
    "/leases/{lease_id}/release",
    response_model=LeaseOut,
    summary="Give up a lease; the task returns to READY",
)
async def release_lease(
    lease_id: str,
    body: LeaseReleaseRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> LeaseOut:
    return await lease_service.release(
        session, principal, lease_id, reason=body.reason, agent=body.agent
    )


@router.get(
    "/leases",
    response_model=LeaseListResponse,
    summary="Leases by holder (agents see their own) and state",
)
async def list_leases(
    holder: str | None = Query(default="me", max_length=160),
    state: str | None = Query(default=None, pattern="^(active|released|expired|revoked)$"),
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> LeaseListResponse:
    return await lease_service.list_leases(
        session, principal, holder=holder, state=state, cursor=cursor, limit=limit
    )


@router.post(
    "/tasks/{task_key}/release",
    response_model=TaskOut,
    summary="Release a claimed task back to READY",
)
async def release_task(
    task_key: str,
    body: TaskReleaseRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> TaskOut:
    return await services.release_task(session, principal, task_key, body)


@router.post(
    "/tasks/{task_key}/handoff",
    response_model=TaskActionResponse,
    summary="Hand off with an L1 brief and optional L2 artifact",
)
async def handoff_task(
    task_key: str,
    body: TaskHandoffRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> TaskActionResponse:
    return await services.handoff_task(session, principal, task_key, body)


@router.post(
    "/tasks/{task_key}/review",
    response_model=TaskActionResponse,
    summary="Approve or reject a task (ping-pong circuit breaker)",
)
async def review_task(
    task_key: str,
    body: TaskReviewRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> TaskActionResponse:
    return await services.review_task(session, principal, task_key, body)


@router.post(
    "/tasks/{task_key}/escalate",
    response_model=TaskActionResponse,
    summary="Escalate a task to a human commander",
)
async def escalate_task(
    task_key: str,
    body: TaskEscalateRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> TaskActionResponse:
    return await services.escalate_task(session, principal, task_key, body)


# ------------------------------------------------------------------ slots


@router.get("/slots", response_model=SlotListResponse, summary="Read the state grid")
async def list_slots(
    namespace: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> SlotListResponse:
    return await services.list_slots(
        session, principal, namespace=namespace, limit=limit
    )


@router.post(
    "/slots/{slot_key:path}/patch",
    response_model=SlotMutationResponse,
    summary="Apply an RFC 6902 JSON Patch to a state slot",
)
async def patch_slot(
    slot_key: str,
    body: SlotPatchRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> SlotMutationResponse:
    return await services.patch_slot(session, principal, slot_key, body)


@router.put(
    "/slots/{slot_key:path}",
    response_model=SlotMutationResponse,
    summary="Replace a state slot (create with expected_version=0)",
)
async def replace_slot(
    slot_key: str,
    body: SlotReplaceRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> SlotMutationResponse:
    return await services.replace_slot(session, principal, slot_key, body)


@router.get("/slots/{slot_key:path}", response_model=SlotOut, summary="Read one slot")
async def get_slot(
    slot_key: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> SlotOut:
    return await services.get_slot(session, principal, slot_key)


# -------------------------------------------------------------- documents


@router.get(
    "/documents",
    response_model=DocumentListResponse,
    summary="List shared document summaries",
)
async def list_documents(
    namespace: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=100_000),
    cursor: str | None = Query(default=None),
    offset: int | None = Query(default=None, ge=0),
    include_archived: bool = Query(default=False),
    include_unverified: bool | None = Query(
        default=None, description="Default true for the census order, false for sort"
    ),
    include_tainted: bool | None = Query(default=None),
    include_cold: bool = Query(default=True, description="Accepted; no cold tier yet"),
    verification_status: str | None = Query(default=None),
    tainted: bool | None = Query(default=None),
    updated_after: datetime | None = Query(default=None),
    updated_before: datetime | None = Query(default=None),
    sort: str | None = Query(
        default=None, description="Legacy listing: updated_desc | updated_asc"
    ),
    order: str | None = Query(
        default=None,
        description="Census: doc_key_asc | updated_at_asc (default under 2026-11)",
    ),
    fields: list[str] | None = Query(
        default=None, description="Return only these fields (repeat or comma-separate)"
    ),
    written_by_subtree: str | None = Query(
        default=None, description="tok_… id: documents written by this token or its subtree"
    ),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentListResponse:
    if offset is not None:
        raise ApiError(
            422,
            "offset_not_supported",
            "offset pagination is not supported; pass next_cursor as cursor",
        )
    if fields is not None:
        fields = [item.strip() for value in fields for item in value.split(",") if item.strip()]
    return await services.list_documents(
        session,
        principal,
        namespace=namespace,
        limit=limit,
        cursor=cursor,
        include_archived=include_archived,
        include_unverified=include_unverified,
        include_tainted=include_tainted,
        verification_status=verification_status,
        tainted=tainted,
        updated_after=updated_after,
        updated_before=updated_before,
        sort=sort,
        order=order,
        fields=fields,
        written_by_subtree=written_by_subtree,
    )


@router.post(
    "/documents/{doc_key:path}/verify",
    response_model=DocumentMutationResponse,
    summary="Bind document verification to the current content version",
)
async def verify_document(
    doc_key: str,
    body: DocumentVerifyRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentMutationResponse:
    return await services.verify_document(session, principal, doc_key, body)


@router.post(
    "/documents/{doc_key:path}/submit-review",
    response_model=DocumentMutationResponse,
    summary="Submit a document for human review",
)
async def submit_document_review(
    doc_key: str,
    body: DocumentStatusRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentMutationResponse:
    return await services.set_document_status(
        session, principal, doc_key, body, action="submit_review"
    )


@router.post(
    "/documents/{doc_key:path}/deprecate",
    response_model=DocumentMutationResponse,
    summary="Deprecate a document",
)
async def deprecate_document(
    doc_key: str,
    body: DocumentStatusRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentMutationResponse:
    return await services.set_document_status(
        session, principal, doc_key, body, action="deprecate"
    )


@router.get(
    "/documents/{doc_key:path}/explain",
    summary="Who wrote a version, who issued their token and what it could do then",
)
async def explain_write(
    doc_key: str,
    version: int | None = Query(default=None, ge=1),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    require_permission(principal, "doc:read")
    return await lineage_service.explain_write(session, principal, doc_key, version)


@router.get("/tokens/{ref}", summary="Inspect a token (self, an ancestor or a descendant)")
async def get_token(
    ref: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await lineage_service.get_token(session, principal, ref)


@router.get("/tokens/{ref}/lineage", summary="Token chain from the root token")
async def get_lineage(
    ref: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await lineage_service.get_lineage(session, principal, ref)


@router.get("/tokens/{ref}/tree", summary="Descendant tokens (own subtree only)")
async def list_token_tree(
    ref: str,
    include_inactive: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await lineage_service.list_token_tree(
        session,
        principal,
        ref,
        include_inactive=include_inactive,
        limit=limit,
        cursor=cursor,
    )


@router.get(
    "/documents/{doc_key:path}/versions",
    response_model=DocumentVersionListResponse,
    summary="Document version history",
)
async def list_document_versions(
    doc_key: str,
    include_unverified: bool = Query(default=False),
    include_tainted: bool = Query(default=False),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentVersionListResponse:
    return await services.list_document_versions(
        session,
        principal,
        doc_key,
        include_unverified=include_unverified,
        include_tainted=include_tainted,
    )


@router.get(
    "/documents/{doc_key:path}/versions/{version}",
    response_model=DocumentVersionOut,
    summary="Read one immutable document version",
)
async def get_document_version(
    doc_key: str,
    version: int,
    include_unverified: bool = Query(default=False),
    include_tainted: bool = Query(default=False),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentVersionOut:
    return await services.get_document_version(
        session,
        principal,
        doc_key,
        version,
        include_unverified=include_unverified,
        include_tainted=include_tainted,
    )


@router.post(
    "/documents/{doc_key:path}/patch-section",
    response_model=DocumentMutationResponse,
    summary="Patch one Markdown section by heading",
)
async def patch_document_section(
    doc_key: str,
    body: DocumentSectionPatchRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentMutationResponse:
    return await services.patch_document_section(
        session, principal, doc_key, body
    )


# ---------------------------------------------------- proposals / arena


@router.post(
    "/documents/{doc_key:path}/proposals",
    response_model=ProposalOut,
    status_code=201,
    summary="Submit a reviewed document proposal",
)
async def create_document_proposal(
    doc_key: str,
    body: ProposalCreateRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> ProposalOut:
    return await services.create_proposal(session, principal, doc_key, body)


@router.get(
    "/documents/{doc_key:path}/proposals",
    response_model=ProposalListResponse,
    summary="List proposals for a document",
)
async def list_document_proposals(
    doc_key: str,
    status: str | None = Query(default=None),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> ProposalListResponse:
    return await services.list_proposals(
        session, principal, doc_key=doc_key, status=status
    )


@router.get(
    "/proposals",
    response_model=ProposalListResponse,
    summary="List workspace proposals",
)
async def list_workspace_proposals(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> ProposalListResponse:
    return await services.list_proposals(
        session, principal, status=status, limit=limit
    )


@router.get(
    "/proposals/{proposal_id}",
    response_model=ProposalOut,
    summary="Read a proposal and its arena votes",
)
async def get_document_proposal(
    proposal_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> ProposalOut:
    return await services.get_proposal(session, principal, proposal_id)


@router.post(
    "/proposals/{proposal_id}/vote",
    response_model=ProposalOut,
    summary="Vote on a Proposal Arena entry",
)
async def vote_document_proposal(
    proposal_id: uuid.UUID,
    body: ProposalVoteRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> ProposalOut:
    return await services.vote_proposal(
        session, principal, proposal_id, body
    )


@router.post(
    "/proposals/{proposal_id}/apply",
    response_model=ProposalApplyResponse,
    summary="Human apply a proposal with three-way rebase",
)
async def apply_document_proposal(
    proposal_id: uuid.UUID,
    body: ProposalApplyRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> ProposalApplyResponse:
    return await services.apply_proposal(
        session, principal, proposal_id, body
    )


@router.post(
    "/proposals/{proposal_id}/reject",
    response_model=ProposalOut,
    summary="Human reject a proposal",
)
async def reject_document_proposal(
    proposal_id: uuid.UUID,
    body: ProposalRejectRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> ProposalOut:
    return await services.reject_proposal(
        session, principal, proposal_id, body
    )


@router.post(
    "/documents/{doc_key:path}/taint",
    response_model=DocumentMutationResponse,
    summary="Human mark or clear document provenance taint",
)
async def change_document_taint(
    doc_key: str,
    body: DocumentTaintRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentMutationResponse:
    return await services.set_document_taint(
        session, principal, doc_key, body
    )


@router.get(
    "/policies",
    response_model=PolicyListResponse,
    summary="Read path policies that apply to this workspace",
)
async def list_workspace_policies(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> PolicyListResponse:
    require_permission(principal, "workspace:read")
    return PolicyListResponse(
        items=await services.list_policies(session, principal.workspace.id)
    )


@router.get(
    "/usage",
    response_model=UsageOut,
    summary="Workspace token, cost and request budget status",
)
async def workspace_usage(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> UsageOut:
    require_permission(principal, "budget:read")
    return UsageOut(
        **await services.month_usage(session, principal.workspace.id)
    )


@router.post(
    "/exports",
    status_code=202,
    summary="Start a tenant/workspace/namespace export (AG-ST-02); poll GET /v1/exports/{id}",
)
async def create_export(
    body: ExportRequest, principal: Principal = Depends(get_principal)
) -> dict:
    return await export_service.request_export(principal, **body.model_dump())


@router.get("/exports", summary="Exports of your tenant, newest first")
async def list_exports(
    scope: str | None = Query(default=None, pattern="^(tenant|workspace|namespace)$"),
    cursor: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
) -> dict:
    return await export_service.list_exports(principal, scope=scope, cursor=cursor, limit=limit)


@router.get("/exports/{export_id}", summary="Export status, counts and download link")
async def get_export(export_id: str, principal: Principal = Depends(get_principal)) -> dict:
    return await export_service.get_export(principal, export_id)


@router.get("/exports/{export_id}/download", summary="The export archive (zip)")
async def download_export(
    export_id: str, principal: Principal = Depends(get_principal)
) -> Response:
    data, row = await export_service.download(principal, export_id)
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{export_id}.zip"',
            "X-Manifest-SHA256": row["manifest_sha256"] or "",
        },
    )


@router.post(
    "/imports",
    status_code=201,
    summary="Import an export archive (zip body) as a new workspace/tenant or an increment",
)
async def create_import(
    request: Request,
    target: str = Query(default="new_workspace",
                        pattern="^(new_workspace|new_tenant|increment)$"),
    verify: bool = Query(default=True),
    principal: Principal = Depends(get_principal),
) -> dict:
    data = await request.body()
    return await export_service.import_archive(principal, data, target=target, verify=verify)


@router.get(
    "/index/status",
    summary="Indexing backlog: pending, failed, oldest pending age, p95 lag (AG-ST-13)",
)
async def index_status(
    namespace: str | None = Query(default=None),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    require_permission(principal, "doc:read")
    if namespace:
        ensure_namespace(principal, namespace)
    return await indexer.index_status(session, principal.workspace.id, namespace)


@router.get(
    "/usage/embedding",
    summary="Embedding tokens, writes, would-be limits and open circuits (AG-ST-15)",
)
async def embedding_usage(
    window: str = Query(default="day", pattern="^(day|week|month)$"),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await budget_service.get_usage(session, principal, window)


# ----------------------------------------------------- external actions


@router.post(
    "/external-actions",
    response_model=ExternalActionOut,
    status_code=201,
    summary="Propose an external action (requires human approval by default)",
)
async def propose_external_action(
    body: ExternalActionCreateRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> ExternalActionOut:
    require_permission(principal, "action:propose")
    from app.auth import effective_agent

    return await services.create_action(
        session,
        principal,
        body,
        actor=effective_agent(principal, body.agent),
    )


@router.get(
    "/external-actions",
    response_model=ExternalActionListResponse,
    summary="List external actions and their dispatch state",
)
async def list_workspace_actions(
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> ExternalActionListResponse:
    require_permission(principal, "action:read")
    return await services.list_actions(
        session,
        principal.workspace.id,
        limit=limit,
        allowed_namespaces=(
            None
            if principal.is_commander or "*" in principal.allowed_namespaces
            else principal.allowed_namespaces
        ),
    )


@router.post(
    "/documents/batch",
    response_model=DocumentBatchUpsertResponse,
    summary="Atomically create or update up to 25 shared documents",
)
async def batch_upsert_documents(
    body: DocumentBatchUpsertRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentBatchUpsertResponse:
    return await services.batch_upsert_documents(session, principal, body)


@router.post(
    "/dereference-many",
    response_model=DereferenceBatchResponse,
    summary="Resolve up to 25 L2 artifact URIs in one call",
)
async def batch_dereference(
    body: DereferenceBatchRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DereferenceBatchResponse:
    return await services.batch_dereference(
        session,
        principal,
        body.uris,
        include_unverified=body.include_unverified,
        include_tainted=body.include_tainted,
    )


@router.post(
    "/documents/{doc_key:path}/archive",
    response_model=DocumentMutationResponse,
    summary="Soft-archive a shared document (creator or commander)",
)
async def archive_document(
    doc_key: str,
    body: DocumentArchiveRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentMutationResponse:
    return await services.archive_document(session, principal, doc_key, body)


@router.post(
    "/documents/{doc_key:path}/rename",
    response_model=DocumentMutationResponse,
    summary="Change a document alias; the canonical id stays and the old alias redirects",
)
async def rename_document(
    doc_key: str,
    body: DocumentRenameRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentMutationResponse:
    return await services.rename_document(session, principal, doc_key, body)


@router.post(
    "/documents/{doc_key:path}/parts",
    summary="Register existing documents as ordered parts of this document",
)
async def register_parts(
    doc_key: str,
    body: RegisterPartsRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await services.register_parts(session, principal, doc_key, body)


@router.get("/namespaces", summary="List registered namespaces")
async def list_namespaces(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    require_permission(principal, "workspace:read")
    return await address_service.list_namespaces(session, principal)


@router.post("/namespaces", status_code=201, summary="Register a namespace before writing to it")
async def register_namespace(
    body: NamespaceRegisterRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await address_service.register_namespace(
        session,
        principal,
        namespace=body.namespace,
        purpose=body.purpose,
        doc_type_default=body.doc_type_default,
        write_policy=body.write_policy,
    )


@router.post(
    "/documents/{doc_key:path}/restore",
    response_model=DocumentMutationResponse,
    summary="Restore a soft-archived shared document (creator or commander)",
)
async def restore_document(
    doc_key: str,
    body: DocumentArchiveRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentMutationResponse:
    return await services.restore_document(session, principal, doc_key, body)


@router.put(
    "/documents/{doc_key:path}",
    response_model=DocumentMutationResponse,
    summary="Create or update a shared document",
)
async def upsert_document(
    doc_key: str,
    body: DocumentUpsertRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentMutationResponse:
    return await services.upsert_document(session, principal, doc_key, body)


@router.get(
    "/documents/{doc_key:path}",
    response_model=DocumentOut,
    summary="Read a shared document (frontmatter + Markdown)",
)
async def get_document(
    doc_key: str,
    include_unverified: bool = Query(default=False),
    include_tainted: bool = Query(default=False),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentOut:
    return await services.get_document(
        session,
        principal,
        doc_key,
        include_unverified=include_unverified,
        include_tainted=include_tainted,
    )


@router.post(
    "/query",
    response_model=DocumentQueryResponse,
    summary="Structured + semantic document query",
)
async def query_documents(
    body: DocumentQueryRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentQueryResponse:
    return await services.query_documents(session, principal, body)


@router.post(
    "/dereference",
    response_model=DereferenceResponse,
    summary="Resolve an L2 artifact URI (doc://<doc_key>)",
)
async def dereference(
    body: DereferenceRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DereferenceResponse:
    return await services.dereference(
        session,
        principal,
        body.uri,
        include_unverified=body.include_unverified,
        include_tainted=body.include_tainted,
        expand_parts=body.expand_parts,
        content_offset=body.content_offset,
        content_limit=body.content_limit,
    )


# ------------------------------------------------------------------ links


@router.post(
    "/links", response_model=LinkOut, status_code=201, summary="Create a workspace link"
)
async def create_link(
    body: LinkCreateRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> LinkOut:
    link = await services.create_link(session, principal, body)
    from app.services.common import link_out

    return link_out(link)


@router.get("/links", response_model=LinkListResponse, summary="List workspace links")
async def list_links(
    node_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> LinkListResponse:
    return await services.list_links(
        session, principal, node_id=node_id, limit=limit
    )


# --------------------------------------------------------------- timeline


@router.post(
    "/timeline",
    response_model=TimelineItemOut,
    status_code=201,
    summary="Append an event to the shared timeline",
)
async def append_timeline_event(
    body: TimelineEventRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> TimelineItemOut:
    from app.services.timeline import record_timeline_event

    return await record_timeline_event(session, principal, body)


@router.get(
    "/timeline",
    response_model=TimelineListResponse,
    summary="Read recent timeline events",
)
async def list_timeline(
    limit: int = Query(default=50, ge=1, le=500),
    hours: int = Query(default=24, ge=1, le=8760),
    event_type: str | None = Query(default=None, max_length=64),
    namespace: str | None = Query(default=None),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> TimelineListResponse:
    require_permission(principal, "timeline:read")
    return TimelineListResponse(
        items=await services.list_timeline(
            session,
            principal.workspace,
            limit=limit,
            hours=hours,
            event_type=event_type,
            namespace=namespace,
            allowed_namespaces=(
                None
                if principal.is_commander or "*" in principal.allowed_namespaces
                else principal.allowed_namespaces
            ),
        )
    )


@router.get(
    "/audit",
    response_model=AuditListResponse,
    summary="Session batches and before/after diffs (AUDITOR/COMMANDER)",
)
async def audit(
    limit: int = Query(default=50, ge=1, le=500),
    batch_id: uuid.UUID | None = Query(default=None),
    cursor: str | None = Query(default=None),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> AuditListResponse:
    require_permission(principal, "audit:read")
    return await services.list_audit(
        session,
        principal.workspace,
        limit=limit,
        batch_id=batch_id,
        cursor=cursor,
        allowed_namespaces=(
            None
            if principal.is_commander or "*" in principal.allowed_namespaces
            else principal.allowed_namespaces
        ),
    )


@router.get(
    "/stats",
    response_model=WorkspaceStatsResponse,
    summary="Workspace usage statistics",
)
async def stats(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> WorkspaceStatsResponse:
    require_permission(principal, "workspace:read")
    return await services.workspace_stats(
        session,
        principal.workspace,
        allowed_namespaces=(
            None
            if principal.is_commander or "*" in principal.allowed_namespaces
            else principal.allowed_namespaces
        ),
    )
