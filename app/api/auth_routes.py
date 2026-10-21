"""Human account login, identity, and logout."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.auth import Principal, get_human_account
from app.config import get_settings
from app.database import get_db
from app.schemas import (
    AuthConfigResponse,
    GoogleLoginRequest,
    HumanLoginRequest,
    HumanMeResponse,
    HumanRegisterRequest,
    HumanSessionOut,
)

router = APIRouter(prefix="/v1/auth", tags=["Human Auth"])
settings = get_settings()


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    return (
        forwarded.split(",")[0].strip()
        if forwarded
        else (request.client.host if request.client else None)
    )


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
