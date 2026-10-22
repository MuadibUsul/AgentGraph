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
    MemberInviteRequest,
    MemberUpdateRequest,
    RightsCloseRequest,
    RightsEraseRequest,
    RightsExportRequest,
    RightsRequestCreate,
)
from app.services import members, rights

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
