"""Workspace-scoped human console API using account sessions."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.auth import Principal, get_human_account, get_human_principal, get_human_writer
from app.database import get_db
from app.errors import Forbidden
from app.schemas import (
    AdminDocumentCreateRequest,
    AgentTokenCreatedResponse,
    AgentTokenCreateRequest,
    AgentTokenListResponse,
    AgentTokenUpdateRequest,
    AuditListResponse,
    BatchUndoResponse,
    BudgetOut,
    BudgetUpdateRequest,
    DocumentHumanUpdateRequest,
    DocumentMutationResponse,
    DocumentStatusRequest,
    DocumentTaintRequest,
    DocumentVerifyRequest,
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
    SlotMutationResponse,
    TaskActionResponse,
    TaskEscalateRequest,
    TaskReviewRequest,
    UsageOut,
    WebhookDeliveryListResponse,
    WebhookRuleCreateRequest,
    WebhookRuleListResponse,
    WebhookRuleOut,
    WebhookRuleUpdateRequest,
    WorkspaceStatsResponse,
)
from app.services.common import agent_token_out
from app.services.retrieval import backfill_workspace_chunks

router = APIRouter(prefix="/v1/human", tags=["Human Console"])


async def _owner(
    principal: Principal = Depends(get_human_account),
) -> Principal:
    if principal.human_role != "OWNER":
        raise Forbidden("workspace OWNER role is required")
    return principal


@router.get(
    "/agent-tokens",
    response_model=AgentTokenListResponse,
    summary="List scoped agent tokens",
)
async def list_agent_tokens(
    principal: Principal = Depends(get_human_writer),
    session: AsyncSession = Depends(get_db),
) -> AgentTokenListResponse:
    return await services.list_agent_tokens(session, principal.workspace)


@router.post(
    "/agent-tokens",
    response_model=AgentTokenCreatedResponse,
    status_code=201,
    summary="Mint a scoped agent token (shown once)",
)
async def create_agent_token(
    body: AgentTokenCreateRequest,
    principal: Principal = Depends(_owner),
    session: AsyncSession = Depends(get_db),
) -> AgentTokenCreatedResponse:
    return await services.create_agent_token(session, principal.workspace, body)


@router.patch(
    "/agent-tokens/{token_id}",
    summary="Update an agent token's role, namespaces or active state",
)
async def update_agent_token(
    token_id: uuid.UUID,
    body: AgentTokenUpdateRequest,
    principal: Principal = Depends(_owner),
    session: AsyncSession = Depends(get_db),
):
    token = await services.update_agent_token(
        session, principal.workspace, token_id, body
    )
    return agent_token_out(token)


@router.get(
    "/workspace",
    response_model=WorkspaceStatsResponse,
    summary="Workspace overview for the signed-in human",
)
async def workspace_overview(
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> WorkspaceStatsResponse:
    return await services.workspace_stats(session, principal.workspace)


@router.post(
    "/tasks/{task_key}/review",
    response_model=TaskActionResponse,
    summary="Approve or reject a task",
)
async def review_task(
    task_key: str,
    body: TaskReviewRequest,
    principal: Principal = Depends(get_human_writer),
    session: AsyncSession = Depends(get_db),
) -> TaskActionResponse:
    return await services.review_task(session, principal, task_key, body)


@router.post(
    "/tasks/{task_key}/escalate",
    response_model=TaskActionResponse,
    summary="Escalate a task",
)
async def escalate_task(
    task_key: str,
    body: TaskEscalateRequest,
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> TaskActionResponse:
    return await services.escalate_task(session, principal, task_key, body)


@router.put(
    "/slots/{slot_key:path}",
    response_model=SlotMutationResponse,
    summary="Human override and lock a state slot",
)
async def human_update_slot(
    slot_key: str,
    body: HumanSlotUpdateRequest,
    principal: Principal = Depends(get_human_writer),
    session: AsyncSession = Depends(get_db),
) -> SlotMutationResponse:
    return await services.human_update_slot(
        session, principal.workspace, slot_key, body
    )


@router.post(
    "/documents",
    response_model=DocumentMutationResponse,
    status_code=201,
    summary="Create a shared document as the human owner",
)
async def create_document(
    body: AdminDocumentCreateRequest,
    principal: Principal = Depends(get_human_writer),
    session: AsyncSession = Depends(get_db),
) -> DocumentMutationResponse:
    return await services.upsert_document(
        session, principal, body.doc_key, body
    )


@router.put(
    "/documents/{doc_key:path}",
    response_model=DocumentMutationResponse,
    summary="Human edit and lock a document",
)
async def human_update_document(
    doc_key: str,
    body: DocumentHumanUpdateRequest,
    principal: Principal = Depends(get_human_writer),
    session: AsyncSession = Depends(get_db),
) -> DocumentMutationResponse:
    return await services.human_update_document(
        session,
        principal.workspace,
        doc_key,
        body,
        actor=principal.agent_signature,
    )


@router.post(
    "/documents/{doc_key:path}/verify",
    response_model=DocumentMutationResponse,
    summary="Verify a document version",
)
async def verify_document(
    doc_key: str,
    body: DocumentVerifyRequest,
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentMutationResponse:
    return await services.verify_document(session, principal, doc_key, body)


@router.post(
    "/documents/{doc_key:path}/submit-review",
    response_model=DocumentMutationResponse,
    summary="Submit a document for review",
)
async def submit_document_review(
    doc_key: str,
    body: DocumentStatusRequest,
    principal: Principal = Depends(get_human_principal),
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
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentMutationResponse:
    return await services.set_document_status(
        session, principal, doc_key, body, action="deprecate"
    )


@router.post(
    "/documents/{doc_key:path}/taint",
    response_model=DocumentMutationResponse,
    summary="Mark or clear document provenance taint",
)
async def change_document_taint(
    doc_key: str,
    body: DocumentTaintRequest,
    principal: Principal = Depends(get_human_writer),
    session: AsyncSession = Depends(get_db),
) -> DocumentMutationResponse:
    return await services.set_document_taint(
        session, principal, doc_key, body
    )


@router.post(
    "/batches/{batch_id}/undo",
    response_model=BatchUndoResponse,
    summary="Undo an agent session batch",
)
async def undo_batch(
    batch_id: uuid.UUID,
    force: bool = Query(default=False),
    principal: Principal = Depends(get_human_writer),
    session: AsyncSession = Depends(get_db),
) -> BatchUndoResponse:
    return await services.undo_batch(
        session,
        principal.workspace,
        batch_id,
        actor=principal.agent_signature,
        force=force,
    )


@router.get(
    "/audit",
    response_model=AuditListResponse,
    summary="Session batches and audit diffs",
)
async def audit(
    limit: int = Query(default=50, ge=1, le=500),
    batch_id: uuid.UUID | None = Query(default=None),
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> AuditListResponse:
    return await services.list_audit(
        session, principal.workspace, limit=limit, batch_id=batch_id
    )


@router.get(
    "/proposals",
    response_model=ProposalListResponse,
    summary="List workspace proposals",
)
async def proposals(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> ProposalListResponse:
    return await services.list_proposals(
        session, principal, status=status, limit=limit
    )


@router.get(
    "/proposals/{proposal_id}",
    response_model=ProposalOut,
    summary="Read one proposal",
)
async def proposal(
    proposal_id: uuid.UUID,
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> ProposalOut:
    return await services.get_proposal(session, principal, proposal_id)


@router.post(
    "/proposals/{proposal_id}/apply",
    response_model=ProposalApplyResponse,
    summary="Apply a proposal",
)
async def apply_proposal(
    proposal_id: uuid.UUID,
    body: ProposalApplyRequest,
    principal: Principal = Depends(get_human_writer),
    session: AsyncSession = Depends(get_db),
) -> ProposalApplyResponse:
    return await services.apply_proposal(
        session, principal, proposal_id, body
    )


@router.post(
    "/proposals/{proposal_id}/reject",
    response_model=ProposalOut,
    summary="Reject a proposal",
)
async def reject_proposal(
    proposal_id: uuid.UUID,
    body: ProposalRejectRequest,
    principal: Principal = Depends(get_human_writer),
    session: AsyncSession = Depends(get_db),
) -> ProposalOut:
    return await services.reject_proposal(
        session, principal, proposal_id, body
    )


@router.get(
    "/external-actions",
    response_model=ExternalActionListResponse,
    summary="List outbound actions",
)
async def external_actions(
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> ExternalActionListResponse:
    return await services.list_actions(
        session,
        principal.workspace.id,
        limit=limit,
        include_payload=True,
    )


@router.post(
    "/external-actions/{action_id}/approve",
    response_model=ExternalActionOut,
    summary="Approve an external action",
)
async def approve_external_action(
    action_id: uuid.UUID,
    principal: Principal = Depends(get_human_writer),
    session: AsyncSession = Depends(get_db),
) -> ExternalActionOut:
    return await services.approve_action(
        session,
        workspace_id=principal.workspace.id,
        action_id=action_id,
        actor=principal.agent_signature,
    )


@router.post(
    "/external-actions/{action_id}/reject",
    response_model=ExternalActionOut,
    summary="Reject an external action",
)
async def reject_external_action(
    action_id: uuid.UUID,
    body: ExternalActionRejectRequest,
    principal: Principal = Depends(get_human_writer),
    session: AsyncSession = Depends(get_db),
) -> ExternalActionOut:
    return await services.reject_action(
        session,
        workspace_id=principal.workspace.id,
        action_id=action_id,
        actor=principal.agent_signature,
        request=body,
    )


@router.post(
    "/external-actions/{action_id}/compensate",
    response_model=ExternalActionOut,
    summary="Queue a compensation action",
)
async def compensate_external_action(
    action_id: uuid.UUID,
    body: ExternalActionCompensateRequest,
    principal: Principal = Depends(get_human_writer),
    session: AsyncSession = Depends(get_db),
) -> ExternalActionOut:
    return await services.compensate_action(
        session,
        workspace_id=principal.workspace.id,
        action_id=action_id,
        actor=principal.agent_signature,
        request=body,
    )


@router.get("/usage", response_model=UsageOut, summary="Budget usage")
async def usage(
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> UsageOut:
    return UsageOut(
        **await services.month_usage(session, principal.workspace.id)
    )


@router.get("/budget", response_model=BudgetOut, summary="Budget limits")
async def budget(
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> BudgetOut:
    row = await services.get_budget(session, principal.workspace.id)
    return BudgetOut(
        workspace_id=row.workspace_id,
        monthly_token_budget=int(row.monthly_token_budget),
        monthly_cost_budget_usd=float(row.monthly_cost_budget_usd),
        max_requests_per_minute=int(row.max_requests_per_minute),
        max_concurrent_leases=int(row.max_concurrent_leases),
        updated_at=row.updated_at,
    )


@router.put("/budget", response_model=BudgetOut, summary="Update budget limits")
async def update_budget(
    body: BudgetUpdateRequest,
    principal: Principal = Depends(_owner),
    session: AsyncSession = Depends(get_db),
) -> BudgetOut:
    row = await services.set_budget(
        session,
        principal.workspace.id,
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
    "/accounts",
    response_model=HumanUserListResponse,
    summary="List human accounts",
)
async def accounts(
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> HumanUserListResponse:
    if principal.human_role != "OWNER":
        from app.errors import Forbidden

        raise Forbidden("owner role required to list accounts")
    return HumanUserListResponse(
        items=await services.list_human_users(session, principal.workspace.id)
    )


@router.post(
    "/accounts",
    response_model=HumanUserCreatedResponse,
    status_code=201,
    summary="Create a human account",
)
async def create_account(
    body: HumanUserCreateRequest,
    principal: Principal = Depends(get_human_writer),
    session: AsyncSession = Depends(get_db),
) -> HumanUserCreatedResponse:
    if principal.human_role != "OWNER":
        from app.errors import Forbidden

        raise Forbidden("owner role required to create accounts")
    user, password = await services.create_human_user(
        session, principal.workspace, body
    )
    return HumanUserCreatedResponse(
        user=user, temporary_password=password
    )


@router.patch(
    "/accounts/{user_id}",
    response_model=HumanUserOut,
    summary="Update a human account",
)
async def update_account(
    user_id: uuid.UUID,
    body: HumanUserUpdateRequest,
    principal: Principal = Depends(get_human_writer),
    session: AsyncSession = Depends(get_db),
) -> HumanUserOut:
    if principal.human_role != "OWNER":
        from app.errors import Forbidden

        raise Forbidden("owner role required to update accounts")
    return await services.update_human_user(
        session, principal.workspace.id, user_id, body
    )


@router.get(
    "/policies",
    response_model=PolicyListResponse,
    summary="List path policies",
)
async def policies(
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> PolicyListResponse:
    return PolicyListResponse(
        items=await services.list_policies(session, principal.workspace.id)
    )


@router.post(
    "/policies",
    response_model=PolicyOut,
    status_code=201,
    summary="Create a path policy",
)
async def create_policy(
    body: PolicyCreateRequest,
    principal: Principal = Depends(_owner),
    session: AsyncSession = Depends(get_db),
) -> PolicyOut:
    return await services.create_policy(
        session, principal.workspace.id, body
    )


@router.patch(
    "/policies/{policy_id}",
    response_model=PolicyOut,
    summary="Update a path policy",
)
async def update_policy(
    policy_id: uuid.UUID,
    body: PolicyUpdateRequest,
    principal: Principal = Depends(_owner),
    session: AsyncSession = Depends(get_db),
) -> PolicyOut:
    return await services.update_policy(
        session, principal.workspace.id, policy_id, body
    )


@router.delete(
    "/policies/{policy_id}",
    status_code=204,
    summary="Delete a path policy",
)
async def delete_policy(
    policy_id: uuid.UUID,
    principal: Principal = Depends(_owner),
    session: AsyncSession = Depends(get_db),
) -> None:
    await services.delete_policy(
        session, principal.workspace.id, policy_id
    )


@router.get(
    "/webhooks",
    response_model=WebhookRuleListResponse,
    summary="List webhook rules",
)
async def webhooks(
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> WebhookRuleListResponse:
    return await services.list_rules(session, principal.workspace.id)


@router.post(
    "/webhooks",
    response_model=WebhookRuleOut,
    status_code=201,
    summary="Create a webhook rule",
)
async def create_webhook(
    body: WebhookRuleCreateRequest,
    principal: Principal = Depends(_owner),
    session: AsyncSession = Depends(get_db),
) -> WebhookRuleOut:
    return await services.create_rule(
        session, principal.workspace.id, body
    )


@router.patch(
    "/webhooks/{rule_id}",
    response_model=WebhookRuleOut,
    summary="Update a webhook rule",
)
async def update_webhook(
    rule_id: uuid.UUID,
    body: WebhookRuleUpdateRequest,
    principal: Principal = Depends(_owner),
    session: AsyncSession = Depends(get_db),
) -> WebhookRuleOut:
    return await services.update_rule(
        session, principal.workspace.id, rule_id, body
    )


@router.delete(
    "/webhooks/{rule_id}",
    status_code=204,
    summary="Delete a webhook rule",
)
async def delete_webhook(
    rule_id: uuid.UUID,
    principal: Principal = Depends(_owner),
    session: AsyncSession = Depends(get_db),
) -> None:
    await services.delete_rule(
        session, principal.workspace.id, rule_id
    )


@router.post(
    "/webhooks/{rule_id}/test",
    summary="Send a signed test webhook",
)
async def test_webhook(
    rule_id: uuid.UUID,
    principal: Principal = Depends(_owner),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await services.test_rule(
        session, principal.workspace.id, rule_id
    )


@router.get(
    "/webhooks/{rule_id}/deliveries",
    response_model=WebhookDeliveryListResponse,
    summary="Webhook delivery history",
)
async def webhook_deliveries(
    rule_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> WebhookDeliveryListResponse:
    return await services.list_deliveries(
        session, principal.workspace.id, rule_id=rule_id, limit=limit
    )


@router.post(
    "/reindex",
    summary="Rebuild chunk indexes for this workspace",
)
async def reindex(
    principal: Principal = Depends(_owner),
    session: AsyncSession = Depends(get_db),
) -> dict:
    count = await backfill_workspace_chunks(
        session, principal.workspace.id
    )
    return {"chunks": count}
