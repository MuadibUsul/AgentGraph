"""v3.2.1 tenant/agent API: tasks, state grid, documents, links, timeline."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.auth import Principal, get_principal, require_permission
from app.database import get_db
from app.errors import ApiError
from app.models import TaskStatus
from app.schemas import (
    AuditListResponse,
    DereferenceRequest,
    DereferenceResponse,
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
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> WorkspaceMapResponse:
    return await services.workspace_map(session, principal)


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
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentListResponse:
    return await services.list_documents(
        session, principal, namespace=namespace, limit=limit
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
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentVersionListResponse:
    return await services.list_document_versions(session, principal, doc_key)


@router.get(
    "/documents/{doc_key:path}/versions/{version}",
    response_model=DocumentVersionOut,
    summary="Read one immutable document version",
)
async def get_document_version(
    doc_key: str,
    version: int,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentVersionOut:
    return await services.get_document_version(
        session, principal, doc_key, version
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
        workspace_id=principal.workspace.id,
        actor=effective_agent(principal, body.agent),
        request=body,
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
        session, principal.workspace.id, limit=limit
    )


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
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentOut:
    return await services.get_document(session, principal, doc_key)


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
    return await services.dereference(session, principal, body.uri)


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
        session, principal.workspace, limit=limit, batch_id=batch_id
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
