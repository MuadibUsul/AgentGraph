"""Human commander console API (admin-token protected)."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.auth import principal_for_admin, require_admin
from app.config import get_settings
from app.database import get_db
from app.errors import ApiError
from app.models import TaskStatus
from app.schemas import (
    AdminDocumentCreateRequest,
    AdminTaskCancelRequest,
    AdminTaskReassignRequest,
    AdminWorkspaceOut,
    AgentTokenCreatedResponse,
    AgentTokenCreateRequest,
    AgentTokenListResponse,
    AgentTokenOut,
    AgentTokenUpdateRequest,
    ApiKeyRotatedResponse,
    AuditListResponse,
    BackupRunRequest,
    BatchUndoResponse,
    BudgetLimitsUpdate,
    BudgetOut,
    BudgetUpdateRequest,
    ChildTenantCreateRequest,
    CreateWorkspaceRequest,
    DocumentHumanUpdateRequest,
    DocumentListResponse,
    DocumentMutationResponse,
    DocumentNormalizeResponse,
    DocumentOut,
    DocumentStatusRequest,
    DocumentTaintRequest,
    DocumentUpsertRequest,
    DocumentVerifyRequest,
    DocumentVersionListResponse,
    DocumentVersionOut,
    ExportRequest,
    ExternalActionCompensateRequest,
    ExternalActionListResponse,
    ExternalActionOut,
    ExternalActionRejectRequest,
    HumanSlotUpdateRequest,
    HumanUserCreatedResponse,
    HumanUserCreateRequest,
    HumanUserListResponse,
    HumanUserOut,
    HumanUserUpdateRequest,
    IncidentCreateRequest,
    IncidentFollowUpRequest,
    IncidentNoticeRequest,
    IncidentScopeRequest,
    IncidentUpdateRequest,
    PolicyCreateRequest,
    PolicyListResponse,
    PolicyOut,
    PolicyUpdateRequest,
    ProposalApplyRequest,
    ProposalApplyResponse,
    ProposalListResponse,
    ProposalOut,
    ProposalRejectRequest,
    RestoreRequest,
    RetrievalEvalRequest,
    RetrievalGateRequest,
    RightsEraseRequest,
    RightsRequestCreate,
    SlotListResponse,
    SlotMutationResponse,
    TaskActionResponse,
    TaskCreateRequest,
    TaskEscalateRequest,
    TaskListResponse,
    TaskOut,
    TaskReleaseRequest,
    TaskReviewRequest,
    TenantDeleteRequest,
    TenantListResponse,
    TenantOut,
    TenantSuspendRequest,
    TimelineListResponse,
    UpdateWorkspaceRequest,
    UsageOut,
    WebhookDeliveryListResponse,
    WebhookRuleCreateRequest,
    WebhookRuleListResponse,
    WebhookRuleOut,
    WebhookRuleUpdateRequest,
    WorkspaceCreatedResponse,
    WorkspaceInfo,
    WorkspaceStatsResponse,
)
from app.services import addresses as address_service
from app.services import agent_onboarding as agent_onboarding_service
from app.services import alerts as alert_service
from app.services import backups as backup_service
from app.services import budget as budget_service
from app.services import exports as export_service
from app.services import incidents as incident_service
from app.services import tenants as tenant_service
from app.services.common import agent_token_out
from app.services.timeline import list_timeline

router = APIRouter(
    prefix="/v1/admin", tags=["Human Commander"], dependencies=[Depends(require_admin)]
)


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


@router.post(
    "/workspaces",
    response_model=WorkspaceCreatedResponse,
    status_code=201,
    summary="Create a workspace and mint its commander key",
)
async def create_workspace(
    body: CreateWorkspaceRequest,
    session: AsyncSession = Depends(get_db),
) -> WorkspaceCreatedResponse:
    return await services.create_workspace(session, name=body.name, tier=body.tier)


@router.get(
    "/workspaces",
    response_model=list[AdminWorkspaceOut],
    summary="List workspaces with counts",
)
async def list_workspaces(
    session: AsyncSession = Depends(get_db),
) -> list[AdminWorkspaceOut]:
    return await services.list_workspaces(session)


@router.patch(
    "/workspaces/{workspace_id}",
    response_model=WorkspaceInfo,
    summary="Update workspace name or tier",
)
async def update_workspace(
    workspace_id: uuid.UUID,
    body: UpdateWorkspaceRequest,
    session: AsyncSession = Depends(get_db),
) -> WorkspaceInfo:
    return await services.update_workspace(
        session, workspace_id, name=body.name, tier=body.tier
    )


@router.post(
    "/workspaces/{workspace_id}/rotate-key",
    response_model=ApiKeyRotatedResponse,
    summary="Rotate the workspace commander key",
)
async def rotate_key(
    workspace_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> ApiKeyRotatedResponse:
    workspace, api_key = await services.rotate_workspace_key(session, workspace_id)
    return ApiKeyRotatedResponse(workspace=workspace, api_key=api_key)


@router.get(
    "/workspaces/{workspace_id}/stats",
    response_model=WorkspaceStatsResponse,
    summary="Workspace statistics",
)
async def workspace_stats(
    workspace_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> WorkspaceStatsResponse:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.workspace_stats(session, workspace)


@router.get(
    "/workspaces/{workspace_id}/timeline",
    response_model=TimelineListResponse,
    summary="Recent timeline events",
)
async def workspace_timeline(
    workspace_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=500),
    hours: int = Query(default=168, ge=1, le=8760),
    event_type: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db),
) -> TimelineListResponse:
    workspace = await services.get_workspace(session, workspace_id)
    return TimelineListResponse(
        items=await list_timeline(
            session, workspace, limit=limit, hours=hours, event_type=event_type
        )
    )


# ------------------------------------------------------------------ tasks


@router.get(
    "/workspaces/{workspace_id}/tasks",
    response_model=TaskListResponse,
    summary="Task board",
)
async def list_tasks(
    workspace_id: uuid.UUID,
    status: str | None = Query(default=None),
    assigned_role: str | None = Query(default=None),
    namespace: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    session: AsyncSession = Depends(get_db),
) -> TaskListResponse:
    workspace = await services.get_workspace(session, workspace_id)
    principal = principal_for_admin(workspace)
    return await services.list_tasks(
        session,
        principal,
        statuses=_statuses(status),
        assigned_role=assigned_role,
        namespace=namespace,
        limit=limit,
    )


@router.post(
    "/workspaces/{workspace_id}/tasks",
    response_model=TaskOut,
    status_code=201,
    summary="Create a task from the console",
)
async def create_task(
    workspace_id: uuid.UUID,
    body: TaskCreateRequest,
    session: AsyncSession = Depends(get_db),
) -> TaskOut:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.create_task(session, principal_for_admin(workspace), body)


@router.get(
    "/workspaces/{workspace_id}/tasks/{task_key}",
    response_model=TaskOut,
    summary="Read one task",
)
async def get_task(
    workspace_id: uuid.UUID,
    task_key: str,
    session: AsyncSession = Depends(get_db),
) -> TaskOut:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.get_task(session, principal_for_admin(workspace), task_key)


@router.post(
    "/workspaces/{workspace_id}/tasks/{task_key}/review",
    response_model=TaskActionResponse,
    summary="Approve, reject or resolve a task as the human commander",
)
async def review_task(
    workspace_id: uuid.UUID,
    task_key: str,
    body: TaskReviewRequest,
    session: AsyncSession = Depends(get_db),
) -> TaskActionResponse:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.review_task(
        session, principal_for_admin(workspace), task_key, body
    )


@router.post(
    "/workspaces/{workspace_id}/tasks/{task_key}/escalate",
    response_model=TaskActionResponse,
    summary="Escalate a task from the console",
)
async def escalate_task(
    workspace_id: uuid.UUID,
    task_key: str,
    body: TaskEscalateRequest,
    session: AsyncSession = Depends(get_db),
) -> TaskActionResponse:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.escalate_task(
        session, principal_for_admin(workspace), task_key, body
    )


# ------------------------------------------------------------------ slots


@router.get(
    "/workspaces/{workspace_id}/slots",
    response_model=SlotListResponse,
    summary="State grid",
)
async def list_slots(
    workspace_id: uuid.UUID,
    namespace: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    session: AsyncSession = Depends(get_db),
) -> SlotListResponse:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.list_slots(
        session,
        principal_for_admin(workspace),
        namespace=namespace,
        limit=limit,
    )


@router.put(
    "/workspaces/{workspace_id}/slots/{slot_key:path}",
    response_model=SlotMutationResponse,
    summary="Human override + lock a state slot",
)
async def human_update_slot(
    workspace_id: uuid.UUID,
    slot_key: str,
    body: HumanSlotUpdateRequest,
    session: AsyncSession = Depends(get_db),
) -> SlotMutationResponse:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.human_update_slot(session, workspace, slot_key, body)


# -------------------------------------------------------------- documents


@router.get(
    "/workspaces/{workspace_id}/documents",
    response_model=DocumentListResponse,
    summary="Document library",
)
async def list_documents(
    workspace_id: uuid.UUID,
    namespace: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    cursor: str | None = Query(default=None),
    offset: int | None = Query(default=None, ge=0),
    include_archived: bool = Query(default=False),
    include_unverified: bool = Query(default=True),
    include_tainted: bool = Query(default=True),
    verification_status: str | None = Query(default=None),
    tainted: bool | None = Query(default=None),
    updated_after: datetime | None = Query(default=None),
    updated_before: datetime | None = Query(default=None),
    sort: str = Query(default="updated_desc"),
    session: AsyncSession = Depends(get_db),
) -> DocumentListResponse:
    if offset is not None:
        raise ApiError(
            422,
            "offset_not_supported",
            "offset pagination is not supported; pass next_cursor as cursor",
        )
    workspace = await services.get_workspace(session, workspace_id)
    return await services.list_documents(
        session,
        principal_for_admin(workspace),
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
    )


@router.post(
    "/workspaces/{workspace_id}/documents",
    response_model=DocumentMutationResponse,
    status_code=201,
    summary="Create a document from the console",
)
async def create_document(
    workspace_id: uuid.UUID,
    body: AdminDocumentCreateRequest,
    session: AsyncSession = Depends(get_db),
) -> DocumentMutationResponse:
    workspace = await services.get_workspace(session, workspace_id)
    payload = body.model_dump(exclude={"doc_key"})
    request = DocumentUpsertRequest(**payload)
    return await services.upsert_document(
        session, principal_for_admin(workspace), body.doc_key, request
    )


@router.post(
    "/workspaces/{workspace_id}/documents/{doc_key:path}/verify",
    response_model=DocumentMutationResponse,
    summary="Verify the current document version",
)
async def verify_document(
    workspace_id: uuid.UUID,
    doc_key: str,
    body: DocumentVerifyRequest,
    session: AsyncSession = Depends(get_db),
) -> DocumentMutationResponse:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.verify_document(
        session, principal_for_admin(workspace), doc_key, body
    )


@router.post(
    "/workspaces/{workspace_id}/documents/{doc_key:path}/submit-review",
    response_model=DocumentMutationResponse,
    summary="Submit a document for review",
)
async def submit_document_review(
    workspace_id: uuid.UUID,
    doc_key: str,
    body: DocumentStatusRequest,
    session: AsyncSession = Depends(get_db),
) -> DocumentMutationResponse:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.set_document_status(
        session,
        principal_for_admin(workspace),
        doc_key,
        body,
        action="submit_review",
    )


@router.post(
    "/workspaces/{workspace_id}/documents/{doc_key:path}/deprecate",
    response_model=DocumentMutationResponse,
    summary="Deprecate a document",
)
async def deprecate_document(
    workspace_id: uuid.UUID,
    doc_key: str,
    body: DocumentStatusRequest,
    session: AsyncSession = Depends(get_db),
) -> DocumentMutationResponse:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.set_document_status(
        session,
        principal_for_admin(workspace),
        doc_key,
        body,
        action="deprecate",
    )


@router.get(
    "/workspaces/{workspace_id}/documents/{doc_key:path}/versions",
    response_model=DocumentVersionListResponse,
    summary="Document version history",
)
async def list_document_versions(
    workspace_id: uuid.UUID,
    doc_key: str,
    session: AsyncSession = Depends(get_db),
) -> DocumentVersionListResponse:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.list_document_versions(
        session, principal_for_admin(workspace), doc_key
    )


@router.get(
    "/workspaces/{workspace_id}/documents/{doc_key:path}/versions/{version}",
    response_model=DocumentVersionOut,
    summary="Read one document version",
)
async def get_document_version(
    workspace_id: uuid.UUID,
    doc_key: str,
    version: int,
    session: AsyncSession = Depends(get_db),
) -> DocumentVersionOut:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.get_document_version(
        session, principal_for_admin(workspace), doc_key, version
    )


@router.get(
    "/workspaces/{workspace_id}/documents/{doc_key:path}",
    response_model=DocumentOut,
    summary="Read a document",
)
async def get_document(
    workspace_id: uuid.UUID,
    doc_key: str,
    session: AsyncSession = Depends(get_db),
) -> DocumentOut:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.get_document(
        session, principal_for_admin(workspace), doc_key
    )


@router.put(
    "/workspaces/{workspace_id}/documents/{doc_key:path}",
    response_model=DocumentMutationResponse,
    summary="Human edit + lock a document",
)
async def human_update_document(
    workspace_id: uuid.UUID,
    doc_key: str,
    body: DocumentHumanUpdateRequest,
    session: AsyncSession = Depends(get_db),
) -> DocumentMutationResponse:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.human_update_document(session, workspace, doc_key, body)


# ------------------------------------------------------------- agent tokens


@router.get(
    "/workspaces/{workspace_id}/agent-onboarding",
    summary="Onboarding state of every agent token in a workspace",
)
async def agent_onboarding_overview(
    workspace_id: uuid.UUID, session: AsyncSession = Depends(get_db)
) -> dict:
    await services.get_workspace(session, workspace_id)
    return await agent_onboarding_service.overview(session, workspace_id)


@router.get(
    "/workspaces/{workspace_id}/namespaces",
    summary="Namespaces visible in a workspace, with document activity",
)
async def list_workspace_namespaces(
    workspace_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> dict:
    workspace = await services.get_workspace(session, workspace_id)
    return await address_service.list_namespaces(session, principal_for_admin(workspace))


@router.get(
    "/workspaces/{workspace_id}/agent-tokens",
    response_model=AgentTokenListResponse,
    summary="List scoped agent tokens",
)
async def list_agent_tokens(
    workspace_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> AgentTokenListResponse:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.list_agent_tokens(session, workspace)


@router.post(
    "/workspaces/{workspace_id}/agent-tokens",
    response_model=AgentTokenCreatedResponse,
    status_code=201,
    summary="Mint a scoped agent token (shown once)",
)
async def create_agent_token(
    workspace_id: uuid.UUID,
    body: AgentTokenCreateRequest,
    session: AsyncSession = Depends(get_db),
) -> AgentTokenCreatedResponse:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.create_agent_token(session, workspace, body)


@router.post(
    "/workspaces/{workspace_id}/agent-tokens/{token_id}/rotate",
    response_model=AgentTokenCreatedResponse,
    summary="Rotate an agent token's secret (new key shown once)",
)
async def rotate_agent_token(
    workspace_id: uuid.UUID,
    token_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> AgentTokenCreatedResponse:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.rotate_agent_token(session, workspace, token_id)


@router.patch(
    "/workspaces/{workspace_id}/agent-tokens/{token_id}",
    response_model=AgentTokenOut,
    summary="Update an agent token's role, namespaces or active state",
)
async def update_agent_token(
    workspace_id: uuid.UUID,
    token_id: uuid.UUID,
    body: AgentTokenUpdateRequest,
    session: AsyncSession = Depends(get_db),
) -> AgentTokenOut:
    workspace = await services.get_workspace(session, workspace_id)
    token = await services.update_agent_token(session, workspace, token_id, body)
    return agent_token_out(token)


# ------------------------------------------------------------------- audit


@router.get(
    "/workspaces/{workspace_id}/audit",
    response_model=AuditListResponse,
    summary="Session batches and before/after diffs",
)
async def audit(
    workspace_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=500),
    batch_id: uuid.UUID | None = Query(default=None),
    cursor: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db),
) -> AuditListResponse:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.list_audit(
        session, workspace, limit=limit, batch_id=batch_id, cursor=cursor
    )


@router.post(
    "/workspaces/{workspace_id}/batches/{batch_id}/undo",
    response_model=BatchUndoResponse,
    summary="Time-travel: revert every mutation in a session batch",
)
async def undo_batch(
    workspace_id: uuid.UUID,
    batch_id: uuid.UUID,
    force: bool = Query(default=False),
    session: AsyncSession = Depends(get_db),
) -> BatchUndoResponse:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.undo_batch(
        session, workspace, batch_id, force=force
    )


# --------------------------------------------------- v3.2 policies / budget


@router.get(
    "/workspaces/{workspace_id}/policies",
    response_model=PolicyListResponse,
    summary="List path policies",
)
async def list_policies(
    workspace_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> PolicyListResponse:
    await services.get_workspace(session, workspace_id)
    return PolicyListResponse(
        items=await services.list_policies(session, workspace_id)
    )


@router.post(
    "/workspaces/{workspace_id}/policies",
    response_model=PolicyOut,
    status_code=201,
    summary="Create a path policy",
)
async def create_policy(
    workspace_id: uuid.UUID,
    body: PolicyCreateRequest,
    session: AsyncSession = Depends(get_db),
) -> PolicyOut:
    await services.get_workspace(session, workspace_id)
    return await services.create_policy(session, workspace_id, body)


@router.patch(
    "/workspaces/{workspace_id}/policies/{policy_id}",
    response_model=PolicyOut,
    summary="Update a path policy",
)
async def update_policy(
    workspace_id: uuid.UUID,
    policy_id: uuid.UUID,
    body: PolicyUpdateRequest,
    session: AsyncSession = Depends(get_db),
) -> PolicyOut:
    await services.get_workspace(session, workspace_id)
    return await services.update_policy(
        session, workspace_id, policy_id, body
    )


@router.delete(
    "/workspaces/{workspace_id}/policies/{policy_id}",
    status_code=204,
    summary="Delete a path policy",
)
async def delete_policy(
    workspace_id: uuid.UUID,
    policy_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> None:
    await services.get_workspace(session, workspace_id)
    await services.delete_policy(session, workspace_id, policy_id)


@router.get(
    "/workspaces/{workspace_id}/budget",
    response_model=BudgetOut,
    summary="Workspace budget limits",
)
async def get_budget(
    workspace_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> BudgetOut:
    await services.get_workspace(session, workspace_id)
    row = await services.get_budget(session, workspace_id)
    return BudgetOut(
        workspace_id=row.workspace_id,
        monthly_token_budget=int(row.monthly_token_budget),
        monthly_cost_budget_usd=float(row.monthly_cost_budget_usd),
        max_requests_per_minute=int(row.max_requests_per_minute),
        max_concurrent_leases=int(row.max_concurrent_leases),
        updated_at=row.updated_at,
    )


@router.put(
    "/workspaces/{workspace_id}/budget",
    response_model=BudgetOut,
    summary="Update workspace budget limits",
)
async def update_budget(
    workspace_id: uuid.UUID,
    body: BudgetUpdateRequest,
    session: AsyncSession = Depends(get_db),
) -> BudgetOut:
    await services.get_workspace(session, workspace_id)
    row = await services.set_budget(
        session,
        workspace_id,
        monthly_token_budget=body.monthly_token_budget,
        monthly_cost_budget_usd=body.monthly_cost_budget_usd,
        max_requests_per_minute=body.max_requests_per_minute,
        max_concurrent_leases=body.max_concurrent_leases,
    )
    return BudgetOut(
        workspace_id=row.workspace_id,
        monthly_token_budget=int(row.monthly_token_budget),
        monthly_cost_budget_usd=float(row.monthly_cost_budget_usd),
        max_requests_per_minute=int(row.max_requests_per_minute),
        max_concurrent_leases=int(row.max_concurrent_leases),
        updated_at=row.updated_at,
    )


@router.get(
    "/workspaces/{workspace_id}/usage",
    response_model=UsageOut,
    summary="Workspace usage ledger summary",
)
async def usage(
    workspace_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> UsageOut:
    await services.get_workspace(session, workspace_id)
    return UsageOut(**await services.month_usage(session, workspace_id))


@router.get("/workspaces/{workspace_id}/alerts", summary="Situation alerts (AG-TC-05)")
async def situation_alerts(
    workspace_id: uuid.UUID,
    state: str | None = Query(default=None, pattern="^(open|acked|resolved)$"),
    severity: str | None = Query(default=None, pattern="^(high|medium|low)$"),
    cursor: str | None = Query(default=None, max_length=40),
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import situation

    workspace = await services.get_workspace(session, workspace_id)
    return await situation.list_alerts(session, principal_for_admin(workspace), state=state,
                                       severity=severity, cursor=cursor)


@router.post("/workspaces/{workspace_id}/alerts/{alert_id}/ack",
             summary="Acknowledge a situation alert")
async def ack_situation_alert(
    workspace_id: uuid.UUID,
    alert_id: str,
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import situation

    workspace = await services.get_workspace(session, workspace_id)
    return await situation.ack_alert(session, principal_for_admin(workspace), alert_id, None)


@router.get("/workspaces/{workspace_id}/metrics",
            summary="Metrics of a principal, document or task")
async def situation_metrics(
    workspace_id: uuid.UUID,
    principal: str | None = Query(default=None, max_length=256),
    doc: str | None = Query(default=None, max_length=256),
    task: str | None = Query(default=None, max_length=128),
    window: str = Query(default="1h"),
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import situation

    workspace = await services.get_workspace(session, workspace_id)
    return await situation.get_metrics(session, principal_for_admin(workspace),
                                       principal_ref=principal, doc_ref=doc, task_ref=task,
                                       window=window)


# ---------------------------------------------------------- v3.2 accounts


@router.get(
    "/workspaces/{workspace_id}/human-users",
    response_model=HumanUserListResponse,
    summary="List human accounts",
)
async def list_human_users(
    workspace_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> HumanUserListResponse:
    await services.get_workspace(session, workspace_id)
    return HumanUserListResponse(
        items=await services.list_human_users(session, workspace_id)
    )


@router.post(
    "/workspaces/{workspace_id}/human-users",
    response_model=HumanUserCreatedResponse,
    status_code=201,
    summary="Create a human account and temporary password",
)
async def create_human_user(
    workspace_id: uuid.UUID,
    body: HumanUserCreateRequest,
    session: AsyncSession = Depends(get_db),
) -> HumanUserCreatedResponse:
    workspace = await services.get_workspace(session, workspace_id)
    user, password = await services.create_human_user(session, workspace, body)
    return HumanUserCreatedResponse(user=user, temporary_password=password)


@router.patch(
    "/workspaces/{workspace_id}/human-users/{user_id}",
    response_model=HumanUserOut,
    summary="Update a human account",
)
async def update_human_user(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    body: HumanUserUpdateRequest,
    session: AsyncSession = Depends(get_db),
) -> HumanUserOut:
    await services.get_workspace(session, workspace_id)
    return await services.update_human_user(
        session, workspace_id, user_id, body
    )


# ---------------------------------------------------------- v3.2 proposals


@router.get(
    "/workspaces/{workspace_id}/proposals",
    response_model=ProposalListResponse,
    summary="List proposals",
)
async def list_proposals(
    workspace_id: uuid.UUID,
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
) -> ProposalListResponse:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.list_proposals(
        session,
        principal_for_admin(workspace),
        status=status,
        limit=limit,
    )


@router.post(
    "/workspaces/{workspace_id}/proposals/{proposal_id}/apply",
    response_model=ProposalApplyResponse,
    summary="Apply a proposal with auto-rebase",
)
async def apply_proposal(
    workspace_id: uuid.UUID,
    proposal_id: uuid.UUID,
    body: ProposalApplyRequest,
    session: AsyncSession = Depends(get_db),
) -> ProposalApplyResponse:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.apply_proposal(
        session, principal_for_admin(workspace), proposal_id, body
    )


@router.post(
    "/workspaces/{workspace_id}/proposals/{proposal_id}/reject",
    response_model=ProposalOut,
    summary="Reject a proposal",
)
async def reject_proposal(
    workspace_id: uuid.UUID,
    proposal_id: uuid.UUID,
    body: ProposalRejectRequest,
    session: AsyncSession = Depends(get_db),
) -> ProposalOut:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.reject_proposal(
        session, principal_for_admin(workspace), proposal_id, body
    )


@router.post(
    "/workspaces/{workspace_id}/documents/{doc_key:path}/taint",
    response_model=DocumentMutationResponse,
    summary="Human mark or clear document taint",
)
async def change_document_taint(
    workspace_id: uuid.UUID,
    doc_key: str,
    body: DocumentTaintRequest,
    session: AsyncSession = Depends(get_db),
) -> DocumentMutationResponse:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.set_document_taint(
        session, principal_for_admin(workspace), doc_key, body
    )


@router.post(
    "/workspaces/{workspace_id}/reindex",
    summary="Rebuild chunk indexes",
)
async def reindex(
    workspace_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> dict:
    await services.get_workspace(session, workspace_id)
    from app.services.retrieval import backfill_workspace_chunks

    count = await backfill_workspace_chunks(session, workspace_id)
    return {"chunks": count}


# ------------------------------------------------------- v3.2 webhooks


@router.get(
    "/workspaces/{workspace_id}/webhooks",
    response_model=WebhookRuleListResponse,
    summary="List webhook rules",
)
async def list_webhooks(
    workspace_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> WebhookRuleListResponse:
    await services.get_workspace(session, workspace_id)
    return await services.list_rules(session, workspace_id)


@router.post(
    "/workspaces/{workspace_id}/webhooks",
    response_model=WebhookRuleOut,
    status_code=201,
    summary="Create a webhook rule",
)
async def create_webhook(
    workspace_id: uuid.UUID,
    body: WebhookRuleCreateRequest,
    session: AsyncSession = Depends(get_db),
) -> WebhookRuleOut:
    await services.get_workspace(session, workspace_id)
    return await services.create_rule(session, workspace_id, body)


@router.patch(
    "/workspaces/{workspace_id}/webhooks/{rule_id}",
    response_model=WebhookRuleOut,
    summary="Update a webhook rule",
)
async def update_webhook(
    workspace_id: uuid.UUID,
    rule_id: uuid.UUID,
    body: WebhookRuleUpdateRequest,
    session: AsyncSession = Depends(get_db),
) -> WebhookRuleOut:
    await services.get_workspace(session, workspace_id)
    return await services.update_rule(session, workspace_id, rule_id, body)


@router.delete(
    "/workspaces/{workspace_id}/webhooks/{rule_id}",
    status_code=204,
    summary="Delete a webhook rule",
)
async def delete_webhook(
    workspace_id: uuid.UUID,
    rule_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> None:
    await services.get_workspace(session, workspace_id)
    await services.delete_rule(session, workspace_id, rule_id)


@router.post(
    "/workspaces/{workspace_id}/webhooks/{rule_id}/test",
    summary="Send a signed test delivery",
)
async def test_webhook(
    workspace_id: uuid.UUID,
    rule_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> dict:
    await services.get_workspace(session, workspace_id)
    return await services.test_rule(session, workspace_id, rule_id)


@router.get(
    "/workspaces/{workspace_id}/webhooks/{rule_id}/deliveries",
    response_model=WebhookDeliveryListResponse,
    summary="Webhook delivery history",
)
async def webhook_deliveries(
    workspace_id: uuid.UUID,
    rule_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_db),
) -> WebhookDeliveryListResponse:
    await services.get_workspace(session, workspace_id)
    return await services.list_deliveries(
        session, workspace_id, rule_id=rule_id, limit=limit
    )


# ------------------------------------------------- v3.2 external actions


@router.get(
    "/workspaces/{workspace_id}/external-actions",
    response_model=ExternalActionListResponse,
    summary="List external actions",
)
async def list_external_actions(
    workspace_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> ExternalActionListResponse:
    await services.get_workspace(session, workspace_id)
    return await services.list_actions(
        session, workspace_id, include_payload=True
    )


@router.post(
    "/workspaces/{workspace_id}/external-actions/{action_id}/approve",
    response_model=ExternalActionOut,
    summary="Approve an external action",
)
async def approve_external_action(
    workspace_id: uuid.UUID,
    action_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> ExternalActionOut:
    await services.get_workspace(session, workspace_id)
    return await services.approve_action(
        session,
        workspace_id=workspace_id,
        action_id=action_id,
        actor="human_commander",
    )


@router.post(
    "/workspaces/{workspace_id}/external-actions/{action_id}/reject",
    response_model=ExternalActionOut,
    summary="Reject an external action",
)
async def reject_external_action(
    workspace_id: uuid.UUID,
    action_id: uuid.UUID,
    body: ExternalActionRejectRequest,
    session: AsyncSession = Depends(get_db),
) -> ExternalActionOut:
    await services.get_workspace(session, workspace_id)
    return await services.reject_action(
        session,
        workspace_id=workspace_id,
        action_id=action_id,
        actor="human_commander",
        request=body,
    )


@router.post(
    "/workspaces/{workspace_id}/external-actions/{action_id}/compensate",
    response_model=ExternalActionOut,
    summary="Queue a compensation action",
)
async def compensate_external_action(
    workspace_id: uuid.UUID,
    action_id: uuid.UUID,
    body: ExternalActionCompensateRequest,
    session: AsyncSession = Depends(get_db),
) -> ExternalActionOut:
    await services.get_workspace(session, workspace_id)
    return await services.compensate_action(
        session,
        workspace_id=workspace_id,
        action_id=action_id,
        actor="human_commander",
        request=body,
    )


# ------------------------------------------ Notion / HTML normalization


@router.post(
    "/workspaces/{workspace_id}/documents/backfill-verified",
    summary="Backfill verification for approved task artifacts",
)
async def backfill_verified_documents(
    workspace_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.backfill_verified_artifacts(
        session, principal_for_admin(workspace), workspace_id
    )


@router.post(
    "/workspaces/{workspace_id}/documents/normalize-html",
    summary="Convert every legacy Notion/HTML document in a workspace to Markdown",
)
async def normalize_workspace_html(
    workspace_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> dict:
    workspace = await services.get_workspace(session, workspace_id)
    converted = await services.normalize_workspace_html_documents(
        session, workspace, actor="human_commander"
    )
    return {"converted": converted}


@router.post(
    "/workspaces/{workspace_id}/documents/{doc_key:path}/normalize-html",
    response_model=DocumentNormalizeResponse,
    summary="Convert one legacy Notion/HTML document to Markdown",
)
async def normalize_document_html(
    workspace_id: uuid.UUID,
    doc_key: str,
    session: AsyncSession = Depends(get_db),
) -> DocumentNormalizeResponse:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.normalize_document_html(
        session, workspace, doc_key, actor="human_commander"
    )


@router.post(
    "/workspaces/{workspace_id}/tasks/{task_key}/force-release",
    response_model=TaskOut,
    summary="Force-release a stuck task with an audit entry",
)
async def force_release_task(
    workspace_id: uuid.UUID,
    task_key: str,
    body: TaskReleaseRequest,
    session: AsyncSession = Depends(get_db),
) -> TaskOut:
    workspace = await services.get_workspace(session, workspace_id)
    body = body.model_copy(update={"force": True})
    return await services.release_task(
        session, principal_for_admin(workspace), task_key, body
    )


@router.delete(
    "/workspaces/{workspace_id}",
    status_code=204,
    summary="Irreversibly delete a disposable workspace (exact name confirmation)",
)
async def delete_workspace(
    workspace_id: uuid.UUID,
    confirm: str = Query(min_length=1, max_length=128),
    session: AsyncSession = Depends(get_db),
) -> None:
    await services.delete_workspace(
        session, workspace_id, confirm_name=confirm
    )


@router.post(
    "/workspaces/{workspace_id}/tasks/{task_key}/reassign",
    response_model=TaskOut,
    summary="Reassign a stuck task to a claimable role and return it to READY",
)
async def reassign_task(
    workspace_id: uuid.UUID,
    task_key: str,
    body: AdminTaskReassignRequest,
    session: AsyncSession = Depends(get_db),
) -> TaskOut:
    await services.get_workspace(session, workspace_id)
    return await services.reassign_task(
        session,
        workspace_id,
        task_key,
        assigned_role=body.assigned_role,
        reason=body.reason,
        expected_version=body.expected_version,
        reset_to_ready=body.reset_to_ready,
    )


@router.post(
    "/workspaces/{workspace_id}/tasks/{task_key}/cancel",
    response_model=TaskOut,
    summary="Cancel a stuck task as FAILED with a reason",
)
async def cancel_task(
    workspace_id: uuid.UUID,
    task_key: str,
    body: AdminTaskCancelRequest,
    session: AsyncSession = Depends(get_db),
) -> TaskOut:
    await services.get_workspace(session, workspace_id)
    return await services.cancel_task(
        session,
        workspace_id,
        task_key,
        reason=body.reason,
        expected_version=body.expected_version,
    )


# ------------------------------------------------- cost limits (AG-ST-15)


async def _budget_scope(session: AsyncSession, scope_kind: str, scope_id: str) -> str:
    if scope_kind == "workspace":
        try:
            workspace_id = uuid.UUID(scope_id)
        except ValueError as exc:
            raise ApiError(422, "validation_error", "workspace scope_id must be a UUID",
                           details={"field": "scope_id"}) from exc
        await services.get_workspace(session, workspace_id)
        return str(workspace_id)
    if scope_kind == "tenant":
        await tenant_service.describe_tenant(session, scope_id)
        return scope_id
    raise ApiError(422, "validation_error", "scope_kind must be workspace or tenant",
                   details={"field": "scope_kind", "allowed": ["workspace", "tenant"]})


@router.get(
    "/budget-limits/{scope_kind}/{scope_id}",
    summary="Embedding quota and rate overrides for a workspace or tenant",
)
async def get_budget_limits(
    scope_kind: str, scope_id: str, session: AsyncSession = Depends(get_db)
) -> dict:
    scope_id = await _budget_scope(session, scope_kind, scope_id)
    return await budget_service.get_limits(scope_kind, scope_id)


@router.put(
    "/budget-limits/{scope_kind}/{scope_id}",
    summary="Set overrides; null falls back to the default. Tenants cap embedding only",
)
async def set_budget_limits(
    scope_kind: str,
    scope_id: str,
    body: BudgetLimitsUpdate,
    session: AsyncSession = Depends(get_db),
) -> dict:
    scope_id = await _budget_scope(session, scope_kind, scope_id)
    return await budget_service.set_limits(
        scope_kind, scope_id, body.model_dump(), updated_by="admin"
    )


# ---------------------------------------------------------------- exports


async def _tenant_principal(session: AsyncSession, tenant_id: str):
    await tenant_service.describe_tenant(session, tenant_id)
    workspace_id = await session.scalar(
        text("SELECT id FROM workspaces WHERE tenant_id = :tenant ORDER BY created_at LIMIT 1"),
        {"tenant": tenant_id},
    )
    if workspace_id is None:
        raise ApiError(409, "invalid_transition", "the tenant has no workspace to export",
                       details={"tenant": tenant_id})
    return principal_for_admin(await services.get_workspace(session, workspace_id))


@router.post(
    "/tenants/{tenant_id}/exports",
    status_code=202,
    summary="Export a tenant (and its subtree) from the console (AG-ST-02)",
)
async def admin_create_export(
    tenant_id: str, body: ExportRequest, session: AsyncSession = Depends(get_db)
) -> dict:
    principal = await _tenant_principal(session, tenant_id)
    values = body.model_dump()
    if values["scope"] == "tenant":
        values["scope_ref"] = tenant_id
    return await export_service.request_export(principal, **values)


@router.get("/exports/{export_id}", summary="Any export's status")
async def admin_get_export(export_id: str) -> dict:
    return await export_service.get_export_any(export_id)


@router.get("/exports/{export_id}/download", summary="Any export's archive")
async def admin_download_export(export_id: str) -> Response:
    data, row = await export_service.download(None, export_id)
    return Response(content=data, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{export_id}.zip"',
        "X-Manifest-SHA256": row["manifest_sha256"] or "",
    })


@router.post(
    "/imports",
    status_code=201,
    summary="Import an archive as a new tenant, into a tenant, or as an increment",
)
async def admin_import(
    request: Request,
    target: str = Query(default="new_tenant", pattern="^(new_workspace|new_tenant|increment)$"),
    verify: bool = Query(default=True),
    into_tenant: str | None = Query(default=None, max_length=40),
) -> dict:
    data = await request.body()
    return await export_service.import_archive(
        None, data, target=target, verify=verify, into_tenant_id=into_tenant
    )


# ---------------------------------------------------------------- tenants


@router.get("/tenants", response_model=TenantListResponse, summary="List tenants")
async def list_tenants(session: AsyncSession = Depends(get_db)) -> TenantListResponse:
    return await tenant_service.list_tenants(session)


@router.get("/tenants/{tenant_id}", response_model=TenantOut, summary="Read a tenant")
async def get_tenant(
    tenant_id: str, session: AsyncSession = Depends(get_db)
) -> TenantOut:
    return await tenant_service.describe_tenant(session, tenant_id)


@router.post(
    "/tenants/{tenant_id}/suspend",
    response_model=TenantOut,
    summary="Suspend a tenant: reads and exports keep working, writes are refused",
)
async def suspend_tenant(
    tenant_id: str,
    body: TenantSuspendRequest,
    session: AsyncSession = Depends(get_db),
) -> TenantOut:
    return await tenant_service.suspend_tenant(session, tenant_id, body.reason)


@router.post("/tenants/{tenant_id}/resume", response_model=TenantOut, summary="Resume")
async def resume_tenant(
    tenant_id: str, session: AsyncSession = Depends(get_db)
) -> TenantOut:
    return await tenant_service.resume_tenant(session, tenant_id)


@router.post(
    "/tenants/{tenant_id}/delete",
    response_model=TenantOut,
    summary="Operator confirmation of tenant deletion (an owner must also confirm)",
)
async def delete_tenant(
    tenant_id: str,
    body: TenantDeleteRequest,
    session: AsyncSession = Depends(get_db),
) -> TenantOut:
    return await tenant_service.admin_confirm_deletion(session, tenant_id, body.confirm)


@router.post(
    "/tenants/{tenant_id}/restore",
    response_model=TenantOut,
    summary="Cancel a pending or in-grace tenant deletion",
)
async def restore_tenant(
    tenant_id: str, session: AsyncSession = Depends(get_db)
) -> TenantOut:
    return await tenant_service.restore_tenant(session, tenant_id)


@router.post(
    "/tenants/{tenant_id}/children",
    status_code=201,
    summary="Create a child tenant with its first workspace (the key is shown once)",
)
async def create_child_tenant(
    tenant_id: str, body: ChildTenantCreateRequest, session: AsyncSession = Depends(get_db)
) -> dict:
    return await tenant_service.create_child_tenant(session, tenant_id, **body.model_dump())


# ---------------------------------------------------------------- backups (AG-ST-10)


@router.get("/backups", summary="Backups, newest first, plus WAL archiving state")
async def list_backups(limit: int = Query(default=100, ge=1, le=1_000)) -> dict:
    return await backup_service.list_backups(limit)


@router.post("/backups", status_code=201, summary="Take a backup now")
async def run_backup(body: BackupRunRequest | None = None) -> dict:
    return await backup_service.run_backup(
        type=body.type if body else None, trigger="manual",
        tenant_ids=body.tenant_ids if body else None,
    )


@router.get("/backups/{backup_id}", summary="One backup with its per-tenant archives")
async def get_backup(backup_id: str) -> dict:
    return await backup_service.get_backup(backup_id)


@router.post(
    "/restore",
    status_code=202,
    summary="Restore a backup or a point in time to staging, a new workspace, or in place",
)
async def restore(body: RestoreRequest) -> dict:
    return await backup_service.request_restore(**body.model_dump())


@router.get("/restores/{restore_id}", summary="A restore's status and result")
async def get_restore(restore_id: str) -> dict:
    return await backup_service.get_restore(restore_id)


@router.get("/restore-drills", summary="Restore drills, newest first")
async def list_restore_drills(limit: int = Query(default=50, ge=1, le=500)) -> dict:
    return await backup_service.list_drills(limit)


@router.post("/restore-drills", status_code=201, summary="Run a restore drill now (staging)")
async def run_restore_drill() -> dict:
    return await backup_service.run_drill(trigger="manual")


# ------------------------------------------------------------ alerts


@router.get("/alerts", summary="Platform alerts for the operator")
async def list_alerts(
    open_only: bool = Query(default=False), kind: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=1_000),
) -> dict:
    return await alert_service.list_alerts(open_only=open_only, kind=kind, limit=limit)


@router.post("/alerts/{alert_id}/ack", summary="Acknowledge an alert")
async def acknowledge_alert(alert_id: str) -> dict:
    alert = await alert_service.acknowledge(alert_id)
    if alert is None:
        raise ApiError(404, "not_found", "alert not found", details={"alert_id": alert_id})
    return alert


# --------------------------------------------------------- incidents (AG-TC-06)


@router.get("/incidents", summary="Security incidents, newest first")
async def list_incidents(limit: int = Query(default=100, ge=1, le=1_000)) -> dict:
    return await incident_service.list_incidents(limit)


@router.post("/incidents", status_code=201, summary="Open a security incident")
async def create_incident(body: IncidentCreateRequest) -> dict:
    return await incident_service.create_incident(body.model_dump(mode="json"))


@router.get("/incidents/{incident_id}", summary="An incident with its affected nodes")
async def get_incident(incident_id: str) -> dict:
    return await incident_service.get_incident(incident_id)


@router.patch("/incidents/{incident_id}", summary="Edit an incident's fields")
async def update_incident(incident_id: str, body: IncidentUpdateRequest) -> dict:
    return await incident_service.update_incident(incident_id, body.model_dump())


@router.post(
    "/incidents/{incident_id}/confirm",
    summary="Confirm the incident: the 72-hour notification clock starts",
)
async def confirm_incident(incident_id: str) -> dict:
    return await incident_service.confirm_incident(incident_id)


@router.post("/incidents/{incident_id}/close", summary="Close the incident")
async def close_incident(incident_id: str) -> dict:
    return await incident_service.close_incident(incident_id)


@router.post(
    "/incidents/{incident_id}/scope",
    summary="Resolve the affected tenant nodes from audit, lineage and access records",
)
async def resolve_incident_scope(
    incident_id: str, body: IncidentScopeRequest | None = None
) -> dict:
    inputs = body.scope_inputs.model_dump() if body and body.scope_inputs else None
    return await incident_service.resolve_scope(incident_id, inputs)


@router.post(
    "/incidents/{incident_id}/notices",
    summary="Notify affected nodes (dry_run previews to the operator only)",
)
async def send_incident_notices(
    incident_id: str, body: IncidentNoticeRequest | None = None
) -> dict:
    return await incident_service.send_notices(incident_id, dry_run=bool(body and body.dry_run))


@router.post("/incidents/{incident_id}/updates", summary="Send a follow-up to notified nodes")
async def send_incident_update(incident_id: str, body: IncidentFollowUpRequest) -> dict:
    return await incident_service.send_update(
        incident_id, body.message, measures_taken=body.measures_taken,
        recommended_actions=body.recommended_actions,
    )


@router.get(
    "/incidents/{incident_id}/record",
    summary="The full incident record: timeline, scope, notices and delivery",
)
async def incident_record(incident_id: str) -> dict:
    return await incident_service.export_record(incident_id)


_NOTICE_PATH = "/incidents/{incident_id}/notices/{notice_id}"
_APPEND_ONLY = "Notices are append-only: always 403"


@router.put(_NOTICE_PATH, summary=_APPEND_ONLY)
async def replace_incident_notice(incident_id: str, notice_id: str) -> dict:
    incident_service.refuse_notice_change()
    return {}


@router.patch(_NOTICE_PATH, summary=_APPEND_ONLY)
async def edit_incident_notice(incident_id: str, notice_id: str) -> dict:
    incident_service.refuse_notice_change()
    return {}


@router.delete(_NOTICE_PATH, summary=_APPEND_ONLY)
async def delete_incident_notice(incident_id: str, notice_id: str) -> dict:
    incident_service.refuse_notice_change()
    return {}


@router.post("/incidents/check-sla", summary="Run the 24/48/60/72-hour checks now")
async def check_incident_slas() -> dict:
    return await incident_service.check_slas()


@router.post(
    "/workspaces/{workspace_id}/root-token:rotate",
    summary="Rotate a workspace's root token secret (shown once; the token id stays)",
)
async def rotate_root_token(
    workspace_id: uuid.UUID, session: AsyncSession = Depends(get_db)
) -> dict:
    from app.services import token_tree

    return await token_tree.rotate_root(session, workspace_id)


# ------------------------------------------------- rights requests (AG-TC-07)


@router.post("/tenants/{tenant_id}/rights-requests", status_code=201,
             summary="Open a rights request, including for platform-held data")
async def admin_create_rights_request(
    tenant_id: str, body: RightsRequestCreate, session: AsyncSession = Depends(get_db)
) -> dict:
    from app.services import rights

    principal = await _tenant_principal(session, tenant_id)
    return await rights.create_request(session, principal, tenant_id, body.model_dump())


@router.post("/rights-requests/{request_id}/locate", summary="Locate (operator)")
async def admin_locate_personal_data(
    request_id: str, session: AsyncSession = Depends(get_db)
) -> dict:
    from app.services import rights

    return await rights.locate(session, await _rights_principal(session, request_id),
                               request_id)


@router.post("/rights-requests/{request_id}/erase", summary="Erase (operator)")
async def admin_erase_personal_data(
    request_id: str, body: RightsEraseRequest, session: AsyncSession = Depends(get_db)
) -> dict:
    from app.services import rights

    return await rights.erase(session, await _rights_principal(session, request_id),
                              request_id, mode=body.mode, items=body.items)


@router.get("/rights-requests/{request_id}", summary="A rights request (operator)")
async def admin_get_rights_request(
    request_id: str, session: AsyncSession = Depends(get_db)
) -> dict:
    from app.services import rights

    return await rights.get_request(session, await _rights_principal(session, request_id),
                                    request_id)


async def _rights_principal(session: AsyncSession, request_id: str):
    tenant_id = await session.scalar(
        text("SELECT tenant_id FROM rights_requests WHERE id = :id"), {"id": request_id})
    if tenant_id is None:
        raise ApiError(404, "not_found", "rights request not found",
                       details={"request_id": request_id})
    return await _tenant_principal(session, tenant_id)


# ------------------------------------------------------------------ R3.2


@router.post("/eval/retrieval", summary="Run the retrieval evaluation set (AG-ST-12)")
async def run_retrieval_eval(body: RetrievalEvalRequest) -> dict:
    from app.services import retrieval_eval

    return await retrieval_eval.run_eval(body.dataset, body.k)


@router.get("/eval/retrieval/runs", summary="Earlier evaluation runs, newest first")
async def retrieval_eval_runs(
    dataset: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    from app.services import retrieval_eval

    return await retrieval_eval.list_runs(dataset, limit)


@router.post("/eval/retrieval/gate",
             summary="Compare a run with a baseline (fails past the allowed drop)")
async def retrieval_eval_gate(body: RetrievalGateRequest) -> dict:
    from app.services import retrieval_eval

    return retrieval_eval.compare(body.baseline, body.result, metric=body.metric,
                                  max_drop=body.max_drop)


@router.post("/eval/self-recall/run", summary="Run the self_recall@50 probe for a workspace now")
async def run_self_recall(workspace_id: uuid.UUID = Query()) -> dict:
    from app.services import retrieval_eval

    result = await retrieval_eval.probe_workspace(workspace_id)
    if result is None:
        raise ApiError(409, "nothing_to_probe", "the workspace has no indexed documents",
                       details={"workspace_id": str(workspace_id)})
    return result


@router.get("/eval/self-recall", summary="self_recall@50 probe results")
async def self_recall_points(
    workspace_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict:
    from app.services import retrieval_eval

    return await retrieval_eval.probes(workspace_id, limit)


@router.post("/tenants/{tenant_id}/signing-keys/rotate",
             summary="Rotate a tenant's attestation signing key (AG-ID-08)")
async def rotate_signing_key(tenant_id: str) -> dict:
    from app.services import attestation

    return await attestation.rotate(None, tenant_id)


@router.post("/exports/scheduled/run", summary="Start the scheduled exports that are due now")
async def run_scheduled_exports() -> dict:
    from app.services import export_schedule

    return {"started": await export_schedule.run_due(),
            "enabled": get_settings().scheduled_exports}
