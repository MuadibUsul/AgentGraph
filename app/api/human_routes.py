"""Workspace-scoped human console API using account sessions."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app import schemas as q_schemas
from app import services
from app.auth import Principal, get_human_account, get_human_principal, get_human_writer
from app.database import get_db
from app.errors import ApiError, Forbidden
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
    DocumentPurgeRequest,
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
    InboxAckRequest,
    LegacyMergeRequest,
    OnboardingUpdate,
    PolicyCreateRequest,
    PolicyListResponse,
    PolicyOut,
    PolicyUpdateRequest,
    ProposalApplyRequest,
    ProposalApplyResponse,
    ProposalListResponse,
    ProposalOut,
    ProposalRejectRequest,
    ReindexRequest,
    SecurityContactsRequest,
    SlotMutationResponse,
    TaskActionResponse,
    TaskEscalateRequest,
    TaskReviewRequest,
    TenantDeleteRequest,
    TenantOut,
    TombstoneOut,
    TransferResponsibilitiesRequest,
    TransferResponsibilitiesResponse,
    UsageOut,
    WebhookDeliveryListResponse,
    WebhookRuleCreateRequest,
    WebhookRuleListResponse,
    WebhookRuleOut,
    WebhookRuleUpdateRequest,
    WorkspaceStatsResponse,
)
from app.services import addresses as address_service
from app.services import agent_onboarding as agent_onboarding_service
from app.services import budget as budget_service
from app.services import exports as export_service
from app.services import human_onboarding as onboarding_service
from app.services import incidents as incident_service
from app.services import leases as lease_service
from app.services import product_events
from app.services import tenants as tenant_service
from app.services.common import agent_token_out

router = APIRouter(prefix="/v1/human", tags=["Human Console"])


async def _owner(
    principal: Principal = Depends(get_human_account),
) -> Principal:
    """Owners and admins of the node (AG-ID-10 role table)."""
    if principal.human_role not in ("OWNER", "ADMIN"):
        raise Forbidden("an owner or admin of this organisation is required")
    return principal


@router.get(
    "/namespaces",
    summary="Namespaces visible to this human console, with document activity",
)
async def list_human_namespaces(
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await address_service.list_namespaces(session, principal)


@router.get(
    "/agent-tokens",
    response_model=AgentTokenListResponse,
    summary="List scoped agent tokens",
)
async def list_agent_tokens(
    issued_by: Literal["human", "agent", "all"] = Query(default="human"),
    parent: str | None = Query(default=None, max_length=40),
    principal: Principal = Depends(get_human_writer),
    session: AsyncSession = Depends(get_db),
) -> AgentTokenListResponse:
    return await services.list_agent_tokens(session, principal.workspace, issued_by, parent)


@router.get(
    "/agent-onboarding",
    summary="Onboarding state of every agent token: steps, first action, last activity",
)
async def agent_onboarding_overview(
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await agent_onboarding_service.overview(session, principal.workspace.id)


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


@router.post(
    "/agent-tokens/{token_id}/revoke-delegated",
    summary="Take back every token this agent delegated; the agent's own token keeps working",
)
async def revoke_delegated_tokens(
    token_id: uuid.UUID,
    body: q_schemas.DelegatedRevokeRequest,
    principal: Principal = Depends(_owner),
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import token_tree

    return await token_tree.revoke_delegated(session, principal, token_id, reason=body.reason)


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
    result = await services.review_task(session, principal, task_key, body)
    await product_events.human_decision(principal, "task_review", body.decision)
    return result


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
    result = await services.verify_document(session, principal, doc_key, body)
    await product_events.human_decision(principal, "document_verify", "verify")
    return result


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
    result = await services.undo_batch(
        session,
        principal.workspace,
        batch_id,
        actor=principal.agent_signature,
        force=force,
    )
    await product_events.human_decision(principal, "batch_undo", "undo")
    return result


@router.get(
    "/audit",
    response_model=AuditListResponse,
    summary="Session batches and audit diffs",
)
async def audit(
    limit: int = Query(default=50, ge=1, le=500),
    batch_id: uuid.UUID | None = Query(default=None),
    cursor: str | None = Query(default=None),
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> AuditListResponse:
    return await services.list_audit(
        session,
        principal.workspace,
        limit=limit,
        batch_id=batch_id,
        cursor=cursor,
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
    result = await services.apply_proposal(
        session, principal, proposal_id, body
    )
    await product_events.human_decision(principal, "proposal", "apply")
    return result


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
    result = await services.reject_proposal(
        session, principal, proposal_id, body
    )
    await product_events.human_decision(principal, "proposal", "reject")
    return result


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
    result = await services.approve_action(
        session,
        workspace_id=principal.workspace.id,
        action_id=action_id,
        actor=principal.agent_signature,
    )
    await product_events.human_decision(principal, "external_action", "approve")
    return result


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
    result = await services.reject_action(
        session,
        workspace_id=principal.workspace.id,
        action_id=action_id,
        actor=principal.agent_signature,
        request=body,
    )
    await product_events.human_decision(principal, "external_action", "reject")
    return result


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
    if principal.human_role not in ("OWNER", "ADMIN"):
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
    if principal.human_role not in ("OWNER", "ADMIN"):
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
    if principal.human_role not in ("OWNER", "ADMIN"):
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


@router.get("/inbox", summary="The people's shared inbox (human gates and verdicts)")
async def human_inbox(
    status: str = Query(default="unacked", pattern="^(unacked|all)$"),
    limit: int = Query(default=50, ge=1, le=100_000),
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import inbox

    return await inbox.summary(session, principal, human=True, status=status, limit=limit)


@router.post("/inbox/ack", summary="Acknowledge the people's inbox up to an event seq")
async def human_inbox_ack(
    body: InboxAckRequest,
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import inbox

    return await inbox.ack(session, principal, body.up_to_event_seq, human=True)


@router.post(
    "/reindex",
    summary="Repair broken indexes (default) or queue a confirmed full rebuild",
)
async def reindex(
    body: ReindexRequest | None = None,
    principal: Principal = Depends(_owner),
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import indexer

    body = body or ReindexRequest()
    return await indexer.repair(session, principal.workspace.id, mode=body.mode,
                                confirm=body.confirm)


# ---------------------------------------------------------------- tenant


@router.get("/tenant", response_model=TenantOut, summary="This workspace's tenant")
async def tenant(
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> TenantOut:
    return await tenant_service.describe_tenant(session, principal.tenant_id)


@router.get("/onboarding", summary="Getting-started checklist for the signed-in person")
async def onboarding_status(
    principal: Principal = Depends(get_human_account),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await onboarding_service.status(session, principal)


@router.post("/onboarding", summary="Tick, skip, close or reopen the getting-started checklist")
async def onboarding_update(
    body: OnboardingUpdate,
    principal: Principal = Depends(get_human_account),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await onboarding_service.update(session, principal, action=body.action, step=body.step)


@router.get(
    "/tenant/final-export",
    summary="The final export taken when deletion was confirmed (?download=true for the zip)",
)
async def tenant_final_export(
    download: bool = Query(default=False),
    principal: Principal = Depends(get_human_account),
    session: AsyncSession = Depends(get_db),
):
    if principal.human_role != "OWNER":
        raise Forbidden("only a tenant owner may download the final export")
    tenant = await tenant_service.describe_tenant(session, principal.tenant_id)
    if not tenant.final_export_id:
        raise ApiError(404, "not_found", "no final export: deletion has not been confirmed",
                       details={"tenant": principal.tenant_id})
    if not download:
        return await export_service.get_export(principal, tenant.final_export_id)
    data, row = await export_service.download(principal, tenant.final_export_id)
    return Response(content=data, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{row["id"]}.zip"',
        "X-Manifest-SHA256": row["manifest_sha256"] or "",
    })


@router.post(
    "/tenant/delete",
    response_model=TenantOut,
    summary="Owner confirmation of tenant deletion (the operator must also confirm)",
)
async def delete_tenant(
    body: TenantDeleteRequest,
    principal: Principal = Depends(get_human_account),
    session: AsyncSession = Depends(get_db),
) -> TenantOut:
    return await tenant_service.owner_confirm_deletion(session, principal, body.confirm)


@router.post(
    "/tenant/restore",
    response_model=TenantOut,
    summary="Cancel a pending or in-grace tenant deletion (owner)",
)
async def restore_tenant(
    principal: Principal = Depends(get_human_account),
    session: AsyncSession = Depends(get_db),
) -> TenantOut:
    return await tenant_service.owner_restore_tenant(session, principal)


@router.post(
    "/documents/{doc_key:path}/purge",
    response_model=TombstoneOut,
    summary="Physically delete an archived document; a tombstone keeps its address",
)
async def purge_document(
    doc_key: str,
    body: DocumentPurgeRequest,
    principal: Principal = Depends(get_human_writer),
    session: AsyncSession = Depends(get_db),
) -> TombstoneOut:
    return await services.purge_document(session, principal, doc_key, body)


@router.post(
    "/transfer-responsibilities",
    response_model=TransferResponsibilitiesResponse,
    summary="Move a principal's open tasks, reviews and maintained docs to a successor",
)
async def transfer_responsibilities(
    body: TransferResponsibilitiesRequest,
    principal: Principal = Depends(get_human_writer),
    session: AsyncSession = Depends(get_db),
) -> TransferResponsibilitiesResponse:
    return await lease_service.transfer_responsibilities(session, principal, body)


@router.post(
    "/budget/circuits/{principal_key}/clear",
    summary="Clear a runaway-loop circuit breaker for one token (owner)",
)
async def clear_circuit(
    principal_key: str,
    principal: Principal = Depends(get_human_account),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await budget_service.clear_circuit(session, principal, principal_key)



# ------------------------------------------------- security incidents (AG-TC-06)


@router.get(
    "/incident-notices",
    summary="Security incident notices sent to your organisation (owners and admins)",
)
async def incident_notices(principal: Principal = Depends(get_human_account)) -> dict:
    return await incident_service.notices_for(principal)


@router.get("/tenant/security-contacts", summary="Extra recipients of incident notices")
async def get_security_contacts(principal: Principal = Depends(get_human_account)) -> dict:
    return await incident_service.get_security_contacts(principal)


@router.put(
    "/tenant/security-contacts",
    summary="Set the root organisation's security contacts (owners only)",
)
async def set_security_contacts(
    body: SecurityContactsRequest, principal: Principal = Depends(get_human_account)
) -> dict:
    return await incident_service.set_security_contacts(principal, body.emails)


# ---------------------------------------------------- token tree and directory


@router.post(
    "/root-token:rotate",
    summary="Rotate the workspace root token's secret (owners); the token id stays",
)
async def rotate_root_token(
    principal: Principal = Depends(get_human_account),
    session: AsyncSession = Depends(get_db),
) -> dict:
    if principal.human_role != "OWNER":
        raise Forbidden("only an owner rotates the root token")
    from app.services import token_tree

    return await token_tree.rotate_root(session, principal.workspace.id)


@router.post(
    "/agents/merge-legacy",
    summary="Claim a pre-directory updated_by label for a registered agent",
)
async def merge_legacy_identity(
    body: LegacyMergeRequest,
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import directory

    return await directory.merge_legacy_identity(session, principal, body.legacy, body.agent_id)


# -------------------------------------------------------- quality control 1.0


@router.put("/quality/tiers", summary="Set a domain's autonomy tier (promotion is a person's act)")
async def set_quality_tier(
    body: q_schemas.AutonomyTierRequest,
    principal: Principal = Depends(get_human_writer),
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import quality

    return await quality.set_tier(session, principal, body)


@router.post("/quality/circuit/{scope_key:path}/close",
             summary="Close a domain's quality circuit (the next change is supervised)")
async def close_quality_circuit(
    scope_key: str,
    body: q_schemas.CircuitCloseRequest,
    principal: Principal = Depends(get_human_writer),
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import quality

    return await quality.close_circuit(session, principal, scope_key, body.note)


@router.get("/quality/samples", summary="The sampling queue")
async def list_human_samples(
    assigned: str | None = Query(default="human", max_length=256),
    verdict: str | None = Query(default="PENDING", pattern="^(PENDING|OK|DEFECT)$"),
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import quality

    return await quality.list_samples(session, principal, assigned=assigned, verdict=verdict)


@router.post("/quality/samples/{sample_id}/verdict",
             summary="Judge a sample; DEFECT records an escape")
async def human_sample_verdict(
    sample_id: uuid.UUID,
    body: q_schemas.SampleVerdictRequest,
    principal: Principal = Depends(get_human_writer),
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import quality

    result = await quality.sample_verdict(session, principal, sample_id, body)
    await product_events.human_decision(principal, "quality_sample", body.verdict)
    return result


@router.post("/quality/escapes", status_code=201, summary="Record a defect that escaped")
async def create_quality_escape(
    body: q_schemas.EscapeCreateRequest,
    principal: Principal = Depends(get_human_writer),
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import quality

    return await quality.create_escape(session, principal, body)


@router.post("/quality/escapes/{escape_id}/resolve",
             summary="Close an escape: FIXED with the gate fix, or WAIVED with a reason")
async def resolve_quality_escape(
    escape_id: uuid.UUID,
    body: q_schemas.EscapeResolveRequest,
    principal: Principal = Depends(get_human_writer),
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import quality

    return await quality.resolve_escape(session, principal, escape_id, body)


@router.post("/quality/poison", status_code=201,
             summary="Plant a defect: a normal task whose mark agents never see")
async def create_quality_poison(
    body: q_schemas.PoisonTaskRequest,
    principal: Principal = Depends(get_human_writer),
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import quality

    return await quality.create_poison(session, principal, body)


@router.get("/quality/poison", summary="Planted defects and whether they were caught")
async def list_quality_poison(
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import quality

    return await quality.list_poison(session, principal)


@router.get("/quality/report", summary="Quality report per domain")
async def human_quality_report(
    window: str = Query(default="30d", pattern="^(7d|30d)$"),
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import quality

    return await quality.report(session, principal, window)


@router.get("/document-parts",
            summary="Hand-split documents read as one (frontmatter part_of / part_index)")
async def human_document_parts(
    part_of: str = Query(min_length=1, max_length=256),
    namespace: str | None = Query(default=None, max_length=64),
    principal: Principal = Depends(get_human_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.services.documents import document_parts

    return await document_parts(session, principal, part_of, namespace=namespace)


@router.post("/product-events", status_code=204,
             summary="Record a console product moment (the upgrade dialog)")
async def record_product_event(
    body: q_schemas.ProductEventRequest,
    principal: Principal = Depends(get_human_principal),
) -> Response:
    await product_events.record(
        body.name,
        workspace_id=principal.workspace.id,
        user_id=principal.user_id,
        source=body.source,
        plan=body.plan,
    )
    return Response(status_code=204)
