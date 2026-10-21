"""v3.2.1 tenant/agent API: tasks, state grid, documents, links, timeline."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.auth import Principal, get_principal, require_permission
from app.database import get_db
from app.errors import ApiError
from app.models import TaskStatus
from app.schemas import (
    AuditListResponse,
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
    DocumentSectionPatchRequest,
    DocumentStatusRequest,
    DocumentTaintRequest,
    DocumentUpsertRequest,
    DocumentVerifyRequest,
    DocumentVersionListResponse,
    DocumentVersionOut,
    ExternalActionCreateRequest,
    ExternalActionListResponse,
    ExternalActionOut,
    LinkCreateRequest,
    LinkListResponse,
    LinkOut,
    PolicyListResponse,
    ProposalApplyRequest,
    ProposalApplyResponse,
    ProposalCreateRequest,
    ProposalListResponse,
    ProposalOut,
    ProposalRejectRequest,
    ProposalVoteRequest,
    SlotListResponse,
    SlotMutationResponse,
    SlotOut,
    SlotPatchRequest,
    SlotReplaceRequest,
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


@router.get("/whoami", response_model=WhoAmIResponse, summary="Principal identity")
async def whoami(principal: Principal = Depends(get_principal)) -> WhoAmIResponse:
    return await services.whoami(principal)


@router.get(
    "/workspace/map",
    response_model=WorkspaceMapResponse,
    summary="L0 workspace map: tasks, slot/doc tree, enums (no raw payloads)",
)
async def workspace_map(
    since: str | None = Query(default=None),
    include_unverified: bool = Query(default=False),
    include_tainted: bool = Query(default=False),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> WorkspaceMapResponse:
    return await services.workspace_map(
        session,
        principal,
        since=since,
        include_unverified=include_unverified,
        include_tainted=include_tainted,
    )


@router.get(
    "/agent-docs",
    summary="Agent-readable API contract, connection guide and OpenAPI schema",
)
async def agent_docs(
    request: Request,
    principal: Principal = Depends(get_principal),
) -> dict:
    """Read-only API/MCP documentation available to every workspace token."""
    openapi = request.app.openapi()
    paths = {
        path: operation
        for path, operation in openapi.get("paths", {}).items()
        if path.startswith("/v1") and not path.startswith("/v1/admin")
    }
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
            "errors": {"error": {"code": "string", "message": "string", "details": "optional"}},
            "pagination": (
                "Pass next_cursor back as cursor; "
                "next_cursor=null means the listing is complete."
            ),
            "version_conflict": "The 409 details include current_version, current_hash and latest.",
            "batch_limits": {"upsert": 25, "dereference": 25},
        },
        "openapi": {
            "openapi": openapi.get("openapi"),
            "info": openapi.get("info"),
            "components": openapi.get("components"),
            "paths": paths,
        },
    }


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
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    include_archived: bool = Query(default=False),
    include_unverified: bool = Query(default=False),
    include_tainted: bool = Query(default=False),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentListResponse:
    return await services.list_documents(
        session,
        principal,
        namespace=namespace,
        limit=limit,
        cursor=cursor,
        include_archived=include_archived,
        include_unverified=include_unverified,
        include_tainted=include_tainted,
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
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> AuditListResponse:
    require_permission(principal, "audit:read")
    return await services.list_audit(
        session,
        principal.workspace,
        limit=limit,
        batch_id=batch_id,
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
    return await services.workspace_stats(session, principal.workspace)
