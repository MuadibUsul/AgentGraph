"""Organisation routes: members and roles (AG-ID-10), rights requests (AG-TC-07).

Both are human work: agent tokens get 403. ``{tenant_id}`` may be ``current``
for the node of the caller's workspace. The work crosses tenants (people from
other nodes, inherited roles), so it runs on the platform session rather than
the request's row-scoped one; the services check every permission.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from app.auth import Principal, get_principal
from app.database import async_session_factory
from app.errors import Forbidden
from app.schemas import (
    AdminAccessRequest,
    ChildTenantCreateRequest,
    MemberInviteRequest,
    MemberUpdateRequest,
    NodeDeleteRequest,
    NodeDetachRequest,
    RightsCloseRequest,
    RightsEraseRequest,
    RightsExportRequest,
    RightsRequestCreate,
    ShareRequest,
    TenantAttachRequest,
    TenantMoveRequest,
    TenantPolicyRequest,
    TenantQuotaRequest,
)
from app.services import members, rights, sharing, tree

router = APIRouter(prefix="/v1", tags=["Organisation"])


def _people_only(principal: Principal) -> Principal:
    if not principal.is_human:
        raise Forbidden("this is human work; agent tokens cannot call it")
    return principal


@router.get("/tenants/{tenant_id}/members",
            summary="Members of a node, with owners and admins inherited from ancestors")
async def list_members(
    tenant_id: str,
    include_inherited: bool = Query(default=True),
    principal: Principal = Depends(get_principal),
) -> dict:
    async with async_session_factory() as session:
        return await members.list_members(session, _people_only(principal), tenant_id,
                                          include_inherited=include_inherited)


@router.post("/tenants/{tenant_id}/members", status_code=201,
             summary="Invite a member (people without an account get an email invitation)")
async def invite_member(
    tenant_id: str,
    body: MemberInviteRequest,
    principal: Principal = Depends(get_principal),
) -> dict:
    async with async_session_factory() as session:
        return await members.invite_member(session, _people_only(principal), tenant_id,
                                           email=body.email, role=body.role,
                                           expires_at=body.expires_at)


@router.patch("/tenants/{tenant_id}/members/{principal_id}", summary="Change a member's role")
async def set_member_role(
    tenant_id: str,
    principal_id: str,
    body: MemberUpdateRequest,
    principal: Principal = Depends(get_principal),
) -> dict:
    async with async_session_factory() as session:
        return await members.set_member_role(session, _people_only(principal), tenant_id,
                                             principal_id, role=body.role,
                                             expires_at=body.expires_at)


@router.delete("/tenants/{tenant_id}/members/{principal_id}",
               summary="Remove a member; their tokens are revoked unless transferred")
async def remove_member(
    tenant_id: str,
    principal_id: str,
    transfer_tokens_to: str | None = Query(default=None, max_length=64),
    principal: Principal = Depends(get_principal),
) -> dict:
    async with async_session_factory() as session:
        return await members.remove_member(session, _people_only(principal), tenant_id,
                                           principal_id, transfer_tokens_to=transfer_tokens_to)


# --------------------------------------------------------- the tree (AG-ID-09, ID-11)


@router.post("/tenants/{tenant_id}/children", status_code=201,
             summary="Create a child node (owners and admins of the parent or above)")
async def create_child_node(
    tenant_id: str,
    body: ChildTenantCreateRequest,
    principal: Principal = Depends(get_principal),
) -> dict:
    return await tree.create_child(None, principal, tenant_id, tenant_type=body.tenant_type,
                                   display_name=body.display_name, alias=body.alias,
                                   workspace_name=body.workspace_name)


@router.get("/tenants/{tenant_id}/tree", summary="The subtree, as far as you may see it")
async def tenant_tree(
    tenant_id: str,
    depth: int | None = Query(default=None, ge=0, le=3),
    principal: Principal = Depends(get_principal),
) -> dict:
    return await tree.get_tree(None, principal, tenant_id, depth)


@router.post("/tenants/{tenant_id}/move",
             summary="Move a subtree inside its tree (both parents' owners confirm)")
async def move_node(
    tenant_id: str,
    body: TenantMoveRequest,
    principal: Principal = Depends(get_principal),
) -> dict:
    return await tree.move(None, _people_only(principal), tenant_id, body.new_parent,
                           confirmation_id=body.confirmation_id)


@router.post("/tenants/{tenant_id}/attach",
             summary="Attach an independent root under a node (both owners confirm)")
async def attach_node(
    tenant_id: str,
    body: TenantAttachRequest,
    principal: Principal = Depends(get_principal),
) -> dict:
    return await tree.attach(None, _people_only(principal), tenant_id, body.parent,
                             confirmation_id=body.confirmation_id)


@router.put("/tenants/{tenant_id}/policies/{key}",
            summary="Set a policy (only ever tighter than the ancestors'; lockable)")
async def set_node_policy(
    tenant_id: str,
    key: str,
    body: TenantPolicyRequest,
    principal: Principal = Depends(get_principal),
) -> dict:
    return await tree.set_policy(None, _people_only(principal), tenant_id, key, body.value,
                                 body.locked)


@router.get("/tenants/{tenant_id}/effective-policy",
            summary="Effective policies with the node each value comes from")
async def effective_policy(
    tenant_id: str,
    principal: Principal = Depends(get_principal),
) -> dict:
    return await tree.get_effective_policy(None, principal, tenant_id)


@router.put("/tenants/{tenant_id}/quotas/{dimension}",
            summary="A node's daily quota (never above its parent's)")
async def set_node_quota(
    tenant_id: str,
    dimension: str,
    body: TenantQuotaRequest,
    principal: Principal = Depends(get_principal),
) -> dict:
    return await tree.set_quota(None, _people_only(principal), tenant_id, dimension, body.limit)


@router.get("/tenants/{tenant_id}/usage", summary="Usage per node, with the subtree's total")
async def node_usage(
    tenant_id: str,
    include_subtree: bool = Query(default=True),
    window_days: int = Query(default=1, ge=1, le=31),
    principal: Principal = Depends(get_principal),
) -> dict:
    return await tree.usage_by_node(None, _people_only(principal), tenant_id,
                                    include_subtree=include_subtree, window_days=window_days)


@router.post("/tenants/{tenant_id}/delete",
             summary="Delete a node below the root (cascade for its subtree; 30 days' grace)")
async def delete_node(
    tenant_id: str,
    body: NodeDeleteRequest,
    principal: Principal = Depends(get_principal),
) -> dict:
    return await tree.delete_node(None, _people_only(principal), tenant_id,
                                  confirm=body.confirm, cascade=body.cascade)


@router.post("/tenants/{tenant_id}/restore", summary="Cancel a node's deletion (with its subtree)")
async def restore_node(
    tenant_id: str,
    principal: Principal = Depends(get_principal),
) -> dict:
    return await tree.restore_node(None, _people_only(principal), tenant_id)


@router.post("/tenants/{tenant_id}/detach",
             summary="Make a subtree its own tree (the root's and the node's owners confirm)")
async def detach_node(
    tenant_id: str,
    body: NodeDetachRequest | None = None,
    principal: Principal = Depends(get_principal),
) -> dict:
    return await tree.detach(None, _people_only(principal), tenant_id,
                             confirmation_id=body.confirmation_id if body else None)


# --------------------------------------------------------- sharing (AG-ID-12)


@router.post("/shares", status_code=201,
             summary="Share a document, namespace, workspace or task inside the tree")
async def create_share(
    body: ShareRequest,
    principal: Principal = Depends(get_principal),
) -> dict:
    return await sharing.share(None, principal, resource_kind=body.resource_kind,
                               resource_ref=body.resource_ref, grantee=body.grantee,
                               access=body.access, expires_at=body.expires_at)


@router.get("/shares", summary="Shares made by this node and shares granted to you")
async def list_shares(principal: Principal = Depends(get_principal)) -> dict:
    return await sharing.list_shares(None, principal)


@router.delete("/shares/{share_id}", summary="Revoke a share (applies to the next request)")
async def revoke_share(
    share_id: str,
    principal: Principal = Depends(get_principal),
) -> dict:
    return await sharing.revoke_share(None, principal, share_id)


@router.post("/tenants/{tenant_id}/access-sessions", status_code=201,
             summary="Open audited, time-boxed content access to a subtree (ancestors' admins)")
async def open_admin_access(
    tenant_id: str,
    body: AdminAccessRequest,
    principal: Principal = Depends(get_principal),
) -> dict:
    return await sharing.open_admin_access(None, _people_only(principal), tenant_id,
                                           reason=body.reason, duration_s=body.duration_s)


@router.post("/access-sessions/{access_session_id}/close",
             summary="Close an admin access session")
async def close_admin_access(
    access_session_id: str,
    principal: Principal = Depends(get_principal),
) -> dict:
    return await sharing.close_admin_access(None, _people_only(principal), access_session_id)


# --------------------------------------------------------- rights requests


@router.post("/tenants/{tenant_id}/rights-requests", status_code=201,
             summary="Open a personal rights request (owners and admins)")
async def create_rights_request(
    tenant_id: str,
    body: RightsRequestCreate,
    principal: Principal = Depends(get_principal),
) -> dict:
    async with async_session_factory() as session:
        return await rights.create_request(session, _people_only(principal), tenant_id,
                                           body.model_dump())


@router.get("/tenants/{tenant_id}/rights-requests", summary="Rights requests of a node")
async def list_rights_requests(
    tenant_id: str,
    principal: Principal = Depends(get_principal),
) -> dict:
    async with async_session_factory() as session:
        return await rights.list_requests(session, _people_only(principal), tenant_id)


@router.get("/rights-requests/{request_id}", summary="A request with its inventory and timeline")
async def get_rights_request(
    request_id: str,
    principal: Principal = Depends(get_principal),
) -> dict:
    async with async_session_factory() as session:
        return await rights.get_request(session, _people_only(principal), request_id)


@router.post("/rights-requests/{request_id}/locate",
             summary="Locate the person's data: structured records and exact text matches")
async def locate_personal_data(
    request_id: str,
    principal: Principal = Depends(get_principal),
) -> dict:
    async with async_session_factory() as session:
        return await rights.locate(session, _people_only(principal), request_id)


@router.post("/rights-requests/{request_id}/export",
             summary="Build the access/portability export (agentgraph-export/1 subset)")
async def export_personal_data(
    request_id: str,
    body: RightsExportRequest | None = None,
    principal: Principal = Depends(get_principal),
) -> dict:
    async with async_session_factory() as session:
        return await rights.export(session, _people_only(principal), request_id,
                                   body.items if body else None)


@router.get("/rights-requests/{request_id}/export/download",
            summary="Download the export (the request's handler, for 7 days)")
async def download_personal_data(
    request_id: str,
    principal: Principal = Depends(get_principal),
) -> Response:
    async with async_session_factory() as session:
        data = await rights.download_export(session, _people_only(principal), request_id)
    return Response(content=data, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{request_id}.zip"'})


@router.post("/rights-requests/{request_id}/erase",
             summary="Pseudonymise, redact or delete located items")
async def erase_personal_data(
    request_id: str,
    body: RightsEraseRequest,
    principal: Principal = Depends(get_principal),
) -> dict:
    async with async_session_factory() as session:
        return await rights.erase(session, _people_only(principal), request_id, mode=body.mode,
                                  items=body.items)


@router.get("/rights-requests/{request_id}/explain",
            summary="How the person's data was processed: categories, agents, purposes")
async def explain_processing(
    request_id: str,
    principal: Principal = Depends(get_principal),
) -> dict:
    async with async_session_factory() as session:
        return await rights.explain(session, _people_only(principal), request_id)


@router.post("/rights-requests/{request_id}/close", summary="Close the request")
async def close_rights_request(
    request_id: str,
    body: RightsCloseRequest | None = None,
    principal: Principal = Depends(get_principal),
) -> dict:
    async with async_session_factory() as session:
        return await rights.close_request(
            session, _people_only(principal), request_id,
            outcome=body.outcome if body else "fulfilled", notes=body.notes if body else None)
