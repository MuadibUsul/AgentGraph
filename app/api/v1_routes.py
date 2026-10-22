"""v3.2.1 tenant/agent API: tasks, state grid, documents, links, timeline."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

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
    AgentRegisterRequest,
    AgentStatusRequest,
    AgentUpdateRequest,
    AlertAckRequest,
    AppendEntriesRequest,
    AttestationRequest,
    AuditListResponse,
    ClearTaintRequest,
    ConstitutionAckRequest,
    ContinuationRequest,
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
    DocumentSupersedeRequest,
    DocumentTaintRequest,
    DocumentUpsertRequest,
    DocumentVerifyRequest,
    DocumentVersionListResponse,
    DocumentVersionOut,
    EscalationCreateRequest,
    EscalationResolveRequest,
    ExportRequest,
    ExternalActionCreateRequest,
    ExternalActionListResponse,
    ExternalActionOut,
    InboxAckRequest,
    LeaseHeartbeatRequest,
    LeaseHeartbeatResponse,
    LeaseListResponse,
    LeaseOut,
    LeaseReleaseRequest,
    LinkCreateRequest,
    LinkListResponse,
    LinkOut,
    NamespaceRegisterRequest,
    PeerReviewRequest,
    PeerReviewSubmitRequest,
    PolicyListResponse,
    ProposalApplyRequest,
    ProposalApplyResponse,
    ProposalCreateRequest,
    ProposalListResponse,
    ProposalOut,
    ProposalRejectRequest,
    ProposalVoteRequest,
    RecallDismissRequest,
    RecallRequest,
    RegisterPartsRequest,
    ResourceAcquireRequest,
    ResourceDefineRequest,
    SlotListResponse,
    SlotMutationResponse,
    SlotOut,
    SlotPatchRequest,
    SlotReplaceRequest,
    SubscriptionRequest,
    SubtasksRequest,
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
    TokenBudgetRequest,
    TokenIssueRequest,
    TokenRenewRequest,
    TokenRevokeRequest,
    UsageOut,
    WatermarkSetRequest,
    WebhookRuleCreateRequest,
    WebhookRuleListResponse,
    WebhookRuleOut,
    WhoAmIResponse,
    WorkspaceMapResponse,
    WorkspaceStatsResponse,
)
from app.services import addresses as address_service
from app.services import budget as budget_service
from app.services import (
    contention,
    digest,
    directory,
    indexer,
    lifecycle,
    resources,
    reviews,
    situation,
    token_budget,
    token_tree,
)
from app.services import documents as document_service
from app.services import events as event_service
from app.services import exports as export_service
from app.services import leases as lease_service
from app.services import lineage as lineage_service
from app.services import logs as log_service
from app.services import orientation as orientation_service
from app.services import recall as recall_service
from app.services import tasks as task_service

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
    since: str | None = Query(
        default=None, max_length=200,
        description="Event cursor or release version: return only what changed since",
    ),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    wanted = [item.strip() for item in sections.split(",") if item.strip()] if sections else None
    return await orientation_service.build(
        session, principal, sections=wanted, max_bytes=max_bytes, since=since
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
    assignee: str | None = Query(default=None, max_length=160,
                                 description="agent:<id>, human:<id> or me"),
    required_role: str | None = Query(default=None, max_length=32),
    parent: str | None = Query(default=None, max_length=128,
                               description="children of this task"),
    kind: str | None = Query(default=None, max_length=16),
    claimable_by_me: bool = Query(default=False,
                                  description="claimable now by the caller (role, assignee)"),
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
        assignee=assignee,
        required_role=required_role,
        parent=parent,
        kind=kind,
        claimable_by_me=claimable_by_me,
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


@router.post("/tasks/{task_key}/continuation",
             summary="Write the task's continuation pack (optionally hand off)")
async def write_continuation(
    task_key: str,
    body: ContinuationRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await task_service.write_continuation(session, principal, task_key, body)


@router.post("/tasks/{task_key}/subtasks", status_code=201,
             summary="Split a task into up to 20 subtasks (holder or lead)")
async def create_subtasks(
    task_key: str,
    body: SubtasksRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await task_service.create_subtasks(session, principal, task_key, body)


@router.get("/tasks/{task_key}/tree", summary="The task tree under a task (at most 500 nodes)")
async def get_task_tree(
    task_key: str,
    depth: int = Query(default=2, ge=1, le=4),
    include: str = Query(default="summary", description="comma list: summary, rollup"),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await task_service.get_task_tree(
        session, principal, task_key, depth=depth,
        include=[item.strip() for item in include.split(",") if item.strip()])


@router.post("/escalations", status_code=201,
             summary="Ask a human a question that blocks no task")
async def create_escalation(
    body: EscalationCreateRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await task_service.create_escalation(session, principal, body)


@router.get("/escalations", summary="Escalations of the workspace")
async def list_escalations(
    status: str | None = Query(default=None, pattern="^(open|resolved)$"),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await task_service.list_escalations(session, principal, status=status)


@router.post("/escalations/{escalation_id}/resolve",
             summary="Decide an escalation (humans only); the task resumes")
async def resolve_escalation(
    escalation_id: str,
    body: EscalationResolveRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await task_service.resolve_escalation(session, principal, escalation_id, body)


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
    review_verdict: Literal["pass", "pass_with_notes", "fail"] | None = Query(
        default=None, description="Only documents with a peer review of this verdict"
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
        review_verdict=review_verdict,
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


@router.post("/tokens", status_code=201,
             summary="Issue a child token (only narrows the issuer's grant; secret shown once)")
async def issue_child_token(
    body: TokenIssueRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await token_tree.issue_child_token(session, principal, body.model_dump())


@router.post("/tokens/{ref}/revoke",
             summary="Revoke a token and its whole subtree (optionally taint their writes)")
async def revoke_token(
    ref: str,
    body: TokenRevokeRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await token_tree.revoke_token(session, principal, ref, reason=body.reason,
                                         taint_writes_since=body.taint_writes_since)


@router.post("/tokens/{ref}/renew", summary="Renew a token (its parent or an ancestor renews)")
async def renew_token(
    ref: str,
    body: TokenRenewRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await token_tree.renew_token(session, principal, ref, body.ttl_seconds)


# --------------------------------------------------------- directory (AG-ID-06)


@router.post("/agents/register", status_code=201,
             summary="Register the calling token's agent in the directory")
async def register_agent(
    body: AgentRegisterRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await directory.register_agent(session, principal, body.model_dump())


@router.get("/agents", summary="The collaborator directory")
async def list_agents(
    role: str | None = Query(default=None, max_length=32),
    capability: str | None = Query(default=None, max_length=64),
    status: str | None = Query(default=None, pattern="^(active|idle|offline|retired)$"),
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=100, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    require_permission(principal, "workspace:read")
    return await directory.list_agents(session, principal, role=role, capability=capability,
                                       status=status, cursor=cursor, limit=limit)


@router.post("/agents/me/status", summary="Set your own status (active or idle)")
async def set_agent_status(
    body: AgentStatusRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await directory.set_status(session, principal, body.status, body.note)


@router.get("/agents/{ref}", summary="One directory entry (agent_id, alias or prn_ id)")
async def get_agent(
    ref: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    require_permission(principal, "workspace:read")
    return await directory.get_agent(session, principal, ref)


@router.patch("/agents/{ref}", summary="Change directory fields (per-field permissions)")
async def update_agent(
    ref: str,
    body: AgentUpdateRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await directory.update_agent(session, principal, ref, body.fields,
                                        body.expected_version)


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


@router.post(
    "/documents/{doc_key:path}/entries", status_code=201,
    summary="Append entries to a log document (no expected version, no conflicts)",
)
async def append_log_entries(
    doc_key: str,
    body: AppendEntriesRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await log_service.append_entries(session, principal, namespace=body.namespace,
                                            doc_key=doc_key, entries=body.entries)


@router.get("/documents/{doc_key:path}/entries", summary="Read a log document's entries")
async def read_log_entries(
    doc_key: str,
    namespace: str | None = Query(default=None, max_length=64),
    after_seq: int | None = Query(default=None, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await log_service.read_entries(session, principal, namespace=namespace,
                                          doc_key=doc_key, after_seq=after_seq, limit=limit)


@router.get("/documents/{doc_key:path}/sections",
            summary="Headings with section_hash, for patch_doc_section without a version")
async def document_sections(
    doc_key: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    document = await services.get_document(session, principal, doc_key,
                                           include_unverified=True, include_tainted=True)
    return {"doc_key": document.doc_key, "version": document.version,
            "sections": contention.list_sections(document.content or "")}


def _webhook_manager(principal: Principal) -> None:
    """Commanders, or agents granted the webhooks:manage scope (never by default)."""
    if principal.is_commander or "webhooks:manage" in (principal.scopes or ()):
        return
    raise ApiError(403, "forbidden", "managing webhooks needs the webhooks:manage scope",
                   details={"scope": "webhooks:manage"})


@router.get("/webhooks", response_model=WebhookRuleListResponse,
            summary="Webhooks of the workspace (scope webhooks:manage)")
async def list_webhooks(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> WebhookRuleListResponse:
    _webhook_manager(principal)
    return await services.list_rules(session, principal.workspace.id)


@router.post("/webhooks", response_model=WebhookRuleOut, status_code=201,
             summary="Create a webhook; a generated secret is returned once")
async def create_webhook(
    body: WebhookRuleCreateRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> WebhookRuleOut:
    _webhook_manager(principal)
    return await services.create_rule(session, principal.workspace.id, body)


@router.delete("/webhooks/{rule_id}", status_code=204, summary="Delete a webhook")
async def delete_webhook(
    rule_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> Response:
    _webhook_manager(principal)
    await services.delete_rule(session, principal.workspace.id, rule_id)
    return Response(status_code=204)


# ------------------------------------------------------------------ inbox


@router.get("/inbox", summary="Changes addressed to you, unacknowledged first by priority")
async def list_inbox(
    status: Literal["unacked", "all"] = Query(default="unacked"),
    limit: int = Query(default=50, ge=1, le=100_000),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import inbox

    return await inbox.summary(session, principal, status=status, limit=limit)


@router.post("/inbox/ack", summary="Acknowledge your inbox up to an event seq (only forward)")
async def ack_inbox(
    body: InboxAckRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import inbox

    return await inbox.ack(session, principal, body.up_to_event_seq)


# ------------------------------------------------------------------ phase 3


@router.post("/lineage/attestations",
             summary="A signed, pseudonymous lineage chain for another tenant (AG-ID-08)")
async def lineage_attestation(
    body: AttestationRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import attestation

    return await attestation.attest(
        session, principal, audience=body.audience, token_id=body.subject.token_id,
        doc_id=body.subject.doc_id, version=body.subject.version,
        ttl_seconds=body.ttl_seconds)


@router.post("/supersessions", summary="Mark a document superseded by another (AG-ST-11)")
async def supersede_document(
    body: DocumentSupersedeRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await lifecycle.supersede(session, principal, old=body.old.model_dump(),
                                     new=body.new.model_dump(), reason=body.reason)


@router.put("/watermarks/{name}", summary="Set a named event cursor you own (AG-EV-04)")
async def set_watermark(
    name: str,
    body: WatermarkSetRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await digest.set_watermark(session, principal, name, body.cursor)


@router.get("/watermarks/{name}", summary="Read a named event cursor you own")
async def get_watermark(
    name: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await digest.get_watermark(session, principal, name)


@router.get("/digest", summary="What happened since a cursor or watermark, summarised")
async def get_digest(
    since: str | None = Query(default=None, max_length=200,
                              description="An event cursor or watermark:<name>"),
    until: str | None = Query(default=None, max_length=200),
    for_me_first: bool = Query(default=True),
    max_items: int = Query(default=50, ge=1, le=200),
    advance_watermark: str | None = Query(default=None, max_length=64),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await digest.get_digest(session, principal, since=since, until=until,
                                   for_me_first=for_me_first, max_items=max_items,
                                   advance_watermark=advance_watermark)


@router.post("/peer-reviews", status_code=201,
             summary="Ask for a peer review of one document version (AG-CO-06)")
async def request_peer_review(
    body: PeerReviewRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await reviews.request_peer_review(
        session, principal, output=body.output, reviewer=body.reviewer, kind=body.kind,
        rubric=body.rubric, independent=body.independent, due_at=body.due_at)


@router.post("/peer-reviews/{task_key}/submit", summary="Submit your peer review")
async def submit_peer_review(
    task_key: str,
    body: PeerReviewSubmitRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await reviews.submit_peer_review(
        session, principal, task_key, verdict=body.verdict,
        findings=[item.model_dump() for item in body.findings], summary=body.summary)


@router.get("/resources", summary="Scarce resources, with holders and queue length (AG-CO-07)")
async def list_resources(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await resources.list_resources(session, principal)


@router.post("/resources", status_code=201, summary="Define a scarce resource (humans only)")
async def define_resource(
    body: ResourceDefineRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await resources.define_resource(
        session, principal, resource_key=body.resource_key, capacity=body.capacity,
        description=body.description, max_hold_seconds=body.max_hold_seconds)


@router.post("/resources/acquire",
             summary="Take a slot of a resource, or queue for one (confirm with queue_ticket)")
async def acquire_resource(
    body: ResourceAcquireRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await resources.acquire_resource(
        session, principal, resource_key=body.resource_key, ttl_seconds=body.ttl_seconds,
        wait_seconds=body.wait_seconds, queue=body.queue, queue_ticket=body.queue_ticket,
        agent=body.agent)


@router.post("/resources/release/{lease_id}", summary="Give a resource slot back")
async def release_resource(
    lease_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await resources.release_resource(session, principal, lease_id)


@router.get("/alerts", summary="Situation alerts (people: all; agents: about themselves)")
async def list_situation_alerts(
    state: str | None = Query(default=None, pattern="^(open|acked|resolved)$"),
    severity: str | None = Query(default=None, pattern="^(high|medium|low)$"),
    cursor: str | None = Query(default=None, max_length=40),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await situation.list_alerts(session, principal, state=state, severity=severity,
                                       cursor=cursor)


@router.post("/alerts/{alert_id}/ack", summary="Acknowledge an alert (people only)")
async def ack_situation_alert(
    alert_id: str,
    body: AlertAckRequest | None = None,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await situation.ack_alert(session, principal, alert_id, body.note if body else None)


@router.get("/metrics", summary="Metrics of a principal, document or task (agents: their own)")
async def get_situation_metrics(
    principal_ref: str | None = Query(default=None, alias="principal", max_length=256),
    doc: str | None = Query(default=None, max_length=256),
    task: str | None = Query(default=None, max_length=128),
    window: str = Query(default="1h"),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await situation.get_metrics(session, principal, principal_ref=principal_ref,
                                       doc_ref=doc, task_ref=task, window=window)


@router.post(
    "/documents/{doc_key:path}/clear-taint",
    response_model=DocumentMutationResponse,
    summary="Clear taint as a new untainted version (people only, AG-TC-01)",
)
async def clear_document_taint(
    doc_key: str,
    body: ClearTaintRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentMutationResponse:
    return await document_service.clear_taint(
        session, principal, doc_key, namespace=body.namespace, version=body.version,
        reason=body.reason)


@router.post(
    "/documents/{doc_key:path}/recall",
    summary="Recall versions; downstream versions are marked and their authors told (AG-TC-02)",
)
async def recall_document(
    doc_key: str,
    body: RecallRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    document = await address_service.find_document(session, principal.workspace.id, doc_key)
    namespace = body.namespace or (document.namespace if document else "default")
    return await recall_service.recall_document(
        session, principal, namespace=namespace,
        doc_key=document.doc_key if document else doc_key, versions=body.versions,
        reason=body.reason, severity=body.severity, max_depth=body.max_depth)


@router.get("/recalls/{recall_id}", summary="A recall and its tree of affected versions")
async def get_recall(
    recall_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    require_permission(principal, "doc:read")
    return await recall_service.get_recall(session, principal, recall_id)


@router.post("/recalls/{recall_id}/dismiss",
             summary="Mark a node as not affected (people only)")
async def dismiss_recall_node(
    recall_id: str,
    body: RecallDismissRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await recall_service.dismiss_node(
        session, principal, recall_id, namespace=body.namespace, doc_key=body.doc_key,
        version=body.version, note=body.note)


@router.get("/tokens/{token_ref}/budget",
            summary="A token's daily budget with its ancestors' state (AG-ST-16)")
async def get_token_budget(
    token_ref: str,
    principal: Principal = Depends(get_principal),
) -> dict:
    return await token_budget.get_budget(principal, token_ref)


@router.put("/tokens/{token_ref}/budget",
            summary="Set a token's daily limits (an ancestor or a person)")
async def set_token_budget(
    token_ref: str,
    body: TokenBudgetRequest,
    principal: Principal = Depends(get_principal),
) -> dict:
    return await token_budget.set_budget(principal, token_ref, body.model_dump())


@router.get("/budgets", summary="Budgets of a token's subtree")
async def list_token_budgets(
    subtree_of: str | None = Query(default=None, max_length=64),
    principal: Principal = Depends(get_principal),
) -> dict:
    return await token_budget.list_budgets(principal, subtree_of)


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
    "/documents:batchUpsert",
    response_model=DocumentBatchUpsertResponse,
    summary="Create or update up to 25 shared documents (AG-ST-07 path)",
)
@router.post(
    "/documents/batch",
    response_model=DocumentBatchUpsertResponse,
    summary="Create or update up to 25 shared documents (atomic=true: all or nothing)",
)
async def batch_upsert_documents(
    body: DocumentBatchUpsertRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentBatchUpsertResponse:
    return await services.batch_upsert_documents(session, principal, body)


@router.post(
    "/documents:batchGet",
    response_model=DereferenceBatchResponse,
    summary="Read up to 25 documents by key (and version) in one call (AG-ST-07 path)",
)
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
        taint_mode=body.taint_mode,
        keys=body.keys,
        fields=body.fields,
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
    taint_mode: str = Query(default="annotate", pattern="^(annotate|separate|exclude)$"),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentOut:
    return await services.get_document(
        session,
        principal,
        doc_key,
        include_unverified=include_unverified,
        include_tainted=include_tainted,
        taint_mode=taint_mode,
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
        taint_mode=body.taint_mode,
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
