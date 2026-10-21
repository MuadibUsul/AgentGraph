"""Human commander console API (admin-token protected)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.auth import principal_for_admin, require_admin
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
    BatchUndoResponse,
    BudgetOut,
    BudgetUpdateRequest,
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
    PolicyCreateRequest,
    PolicyListResponse,
    PolicyOut,
    PolicyUpdateRequest,
    ProposalApplyRequest,
    ProposalApplyResponse,
    ProposalListResponse,
    ProposalOut,
    ProposalRejectRequest,
    SlotListResponse,
    SlotMutationResponse,
    TaskActionResponse,
    TaskCreateRequest,
    TaskEscalateRequest,
    TaskListResponse,
    TaskOut,
    TaskReleaseRequest,
    TaskReviewRequest,
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
    session: AsyncSession = Depends(get_db),
) -> DocumentListResponse:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.list_documents(
        session,
        principal_for_admin(workspace),
        namespace=namespace,
        limit=limit,
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
    session: AsyncSession = Depends(get_db),
) -> AuditListResponse:
    workspace = await services.get_workspace(session, workspace_id)
    return await services.list_audit(
        session, workspace, limit=limit, batch_id=batch_id
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
    return await services.list_actions(session, workspace_id)


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
