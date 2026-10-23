"""Human account login, identity, and logout."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.auth import Principal, get_human_account
from app.config import get_settings
from app.database import get_db
from app.schemas import (
    AcceptInviteRequest,
    AuthConfigResponse,
    CliSessionApproveRequest,
    CliSessionCreateRequest,
    CliSessionPollRequest,
    GoogleLinkRequest,
    GoogleLoginRequest,
    HumanLoginRequest,
    HumanMeResponse,
    HumanRegisterRequest,
    HumanSessionOut,
    HumanUserOut,
    PasswordChangeRequest,
    SwitchWorkspaceRequest,
)

router = APIRouter(prefix="/v1/auth", tags=["Human Auth"])
settings = get_settings()


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Caddy overwrites client-supplied values; taking the last hop avoids
        # trusting an attacker-appended left-most value behind a proxy chain.
        return forwarded.split(",")[-1].strip() or None
    return request.client.host if request.client else None


@router.get(
    "/config",
    response_model=AuthConfigResponse,
    summary="Public sign-in and signup capability",
)
async def auth_config() -> AuthConfigResponse:
    return AuthConfigResponse(
        signup_enabled=settings.human_signup_enabled,
        google_enabled=bool(settings.google_client_id),
        google_client_id=settings.google_client_id,
    )


@router.post(
    "/register",
    response_model=HumanSessionOut,
    status_code=201,
    summary="Create a workspace and owner account with email/password",
)
async def register(
    body: HumanRegisterRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> HumanSessionOut:
    return await services.human_register(
        session,
        body,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )


@router.post(
    "/google",
    response_model=HumanSessionOut,
    summary="Sign in or create an owner account with a Google ID token",
)
async def google_login(
    body: GoogleLoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> HumanSessionOut:
    return await services.human_google_login(
        session,
        body,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )


@router.post(
    "/password",
    status_code=204,
    summary="Change the signed-in user's password and revoke other sessions",
)
async def change_password(
    body: PasswordChangeRequest,
    principal: Principal = Depends(get_human_account),
    session: AsyncSession = Depends(get_db),
) -> None:
    await services.human_change_password(
        session,
        principal.user_id,
        body.current_password,
        body.new_password,
        current_session_id=principal.token_id,
    )


@router.post(
    "/google/link",
    response_model=HumanUserOut,
    summary="Link a Google identity to the signed-in password account",
)
async def google_link(
    body: GoogleLinkRequest,
    principal: Principal = Depends(get_human_account),
    session: AsyncSession = Depends(get_db),
) -> HumanUserOut:
    return await services.human_link_google(
        session, principal.user_id, body.credential
    )


@router.post(
    "/login",
    response_model=HumanSessionOut,
    summary="Exchange email/password for a short-lived human session",
)
async def login(
    body: HumanLoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> HumanSessionOut:
    return await services.human_login(
        session,
        body,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )


@router.get(
    "/me",
    response_model=HumanMeResponse,
    summary="Current human account and workspace",
)
async def me(
    principal: Principal = Depends(get_human_account),
    session: AsyncSession = Depends(get_db),
) -> HumanMeResponse:
    assert principal.user_id is not None
    return await services.human_me(
        session, principal.user_id, principal.workspace
    )


@router.post(
    "/logout",
    status_code=204,
    summary="Revoke the current human session",
)
async def logout(
    principal: Principal = Depends(get_human_account),
    session: AsyncSession = Depends(get_db),
) -> None:
    assert principal.user_id is not None and principal.token_id is not None
    await services.human_logout(
        session, principal.user_id, principal.token_id
    )


@router.post("/accept-invite", status_code=201,
             summary="Accept a member invitation: create the account and sign in")
async def accept_invite(
    body: AcceptInviteRequest, session: AsyncSession = Depends(get_db)
) -> dict:
    from app.services.members import accept_invitation

    return await accept_invitation(session, code=body.code, password=body.password,
                                   display_name=body.display_name)


@router.post("/switch-workspace",
             summary="A session for another workspace you may use (404 if you may not)")
async def switch_workspace(
    body: SwitchWorkspaceRequest,
    principal: Principal = Depends(get_human_account),
) -> dict:
    from app.database import async_session_factory
    from app.services.members import switch_workspace as switch

    async with async_session_factory() as session:
        return await switch(session, principal, body.workspace_id)


# ------------------------------------------------- npx veynod init (v3.34)


@router.post("/cli/sessions", status_code=201,
             summary="Start a CLI sign-in: a user code to approve in the console")
async def create_cli_session(
    body: CliSessionCreateRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import cli_setup
    from app.services.accounts import _rate_limit_login

    await _rate_limit_login(
        session, f"cli:{_client_ip(request) or 'unknown'}", None,
        limit=settings.cli_sessions_per_ip_per_minute,
    )
    return await cli_setup.create_session(session, body, base_url=str(request.base_url))


@router.post("/cli/poll", summary="Collect the sealed agent key once the code is approved")
async def poll_cli_session(
    body: CliSessionPollRequest,
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import cli_setup

    return await cli_setup.poll(session, body.device_code)


@router.get("/cli/sessions/{user_code}", summary="What a CLI sign-in code asks for")
async def describe_cli_session(
    user_code: str,
    principal: Principal = Depends(get_human_account),
) -> dict:
    from app.database import async_session_factory
    from app.services import cli_setup

    async with async_session_factory() as unscoped:
        view = await cli_setup.describe(unscoped, user_code)
    view["workspace"] = {"id": str(principal.workspace.id), "name": principal.workspace.name}
    view["can_approve"] = principal.human_role in ("OWNER", "ADMIN")
    return view


@router.post("/cli/sessions/{user_code}/approve",
             summary="Approve a CLI sign-in: mint an agent key sealed to that terminal")
async def approve_cli_session(
    user_code: str,
    body: CliSessionApproveRequest,
    principal: Principal = Depends(get_human_account),
    session: AsyncSession = Depends(get_db),
) -> dict:
    from app.database import async_session_factory
    from app.services import cli_setup

    async with async_session_factory() as unscoped:
        return await cli_setup.approve(session, unscoped, principal, user_code, body)


@router.post("/cli/sessions/{user_code}/deny", summary="Refuse a CLI sign-in")
async def deny_cli_session(
    user_code: str,
    principal: Principal = Depends(get_human_account),
) -> dict:
    from app.database import async_session_factory
    from app.services import cli_setup

    async with async_session_factory() as unscoped:
        return await cli_setup.deny(unscoped, principal, user_code)
