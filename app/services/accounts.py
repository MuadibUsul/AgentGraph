"""Human accounts, password sessions, and console identity."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import anyio
import httpx
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    generate_api_key,
    generate_human_key,
    hash_api_key,
    principal_for_human,
)
from app.config import get_settings
from app.errors import ApiError, Forbidden, NotFound
from app.models import HumanSession, HumanUser, Workspace
from app.schemas import (
    GoogleLoginRequest,
    HumanLoginRequest,
    HumanMeResponse,
    HumanRegisterRequest,
    HumanSessionOut,
    HumanUserCreateRequest,
    HumanUserOut,
    HumanUserUpdateRequest,
)
from app.services.common import workspace_info

settings = get_settings()
_PASSWORD_LIMITER = anyio.CapacityLimiter(4)
_DUMMY_PASSWORD_HASH = ""


def _b64(value: bytes) -> str:
    return value.hex()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    n, r, p = settings.password_hash_n, settings.password_hash_r, settings.password_hash_p
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=64
    )
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n_raw, r_raw, p_raw, salt_raw, digest_raw = encoded.split("$", 5)
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_raw),
            n=int(n_raw),
            r=int(r_raw),
            p=int(p_raw),
            dklen=64,
        )
        return hmac.compare_digest(digest.hex(), digest_raw)
    except (ValueError, TypeError):
        return False


_DUMMY_PASSWORD_HASH = hash_password("agentgraph-dummy-password")


async def _hash_password_async(password: str) -> str:
    return await anyio.to_thread.run_sync(
        hash_password, password, limiter=_PASSWORD_LIMITER
    )


async def _verify_password_async(password: str, encoded: str) -> bool:
    return await anyio.to_thread.run_sync(
        verify_password, password, encoded, limiter=_PASSWORD_LIMITER
    )


def user_out(user: HumanUser) -> HumanUserOut:
    return HumanUserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        is_active=user.is_active,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


async def _rate_limit_login(
    session: AsyncSession,
    key: str,
    workspace_id: uuid.UUID | None,
    limit: int | None = None,
) -> None:
    if not settings.rate_limit_enabled:
        return
    bucket = f"login:{key}"[:160]
    count = await session.scalar(
        text(
            """
INSERT INTO rate_limit_windows (workspace_id, bucket, window_start, count)
VALUES (
    :workspace_id,
    :bucket,
    to_timestamp(floor(extract(epoch from now()) / 60) * 60),
    1
)
ON CONFLICT (bucket, window_start)
DO UPDATE SET count = rate_limit_windows.count + 1
RETURNING count
"""
        ),
        {
            "workspace_id": str(workspace_id) if workspace_id else None,
            "bucket": bucket,
        },
    )
    effective_limit = limit or settings.human_login_max_attempts_per_minute
    await session.commit()
    if int(count or 0) > effective_limit:
        from app.errors import RateLimited

        raise RateLimited(60)


async def create_human_user(
    session: AsyncSession,
    workspace: Workspace,
    request: HumanUserCreateRequest,
) -> tuple[HumanUserOut, str]:
    email = request.email.strip().lower()
    existing = await session.scalar(
        select(HumanUser.id).where(HumanUser.email == email)
    )
    if existing is not None:
        raise ApiError(409, "human_user_exists", "this email already has an account")
    temporary_password = request.password or secrets.token_urlsafe(14)
    user = HumanUser(
        workspace_id=workspace.id,
        email=email,
        display_name=request.display_name,
        role=request.role,
        password_hash=await _hash_password_async(temporary_password),
        is_active=True,
    )
    session.add(user)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise ApiError(409, "human_user_exists", "this email already has an account") from None
    return user_out(user), temporary_password


async def list_human_users(
    session: AsyncSession, workspace_id: uuid.UUID
) -> list[HumanUserOut]:
    rows = (
        await session.execute(
            select(HumanUser)
            .where(HumanUser.workspace_id == workspace_id)
            .order_by(HumanUser.created_at.desc())
        )
    ).scalars().all()
    return [user_out(row) for row in rows]


async def update_human_user(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    request: HumanUserUpdateRequest,
) -> HumanUserOut:
    user = await session.get(HumanUser, user_id)
    if user is None or user.workspace_id != workspace_id:
        raise NotFound("human_user")
    if request.display_name is not None:
        user.display_name = request.display_name
    if request.role is not None:
        user.role = request.role
    if request.is_active is not None:
        user.is_active = request.is_active
        if not request.is_active:
            await session.execute(
                update(HumanSession)
                .where(
                    HumanSession.user_id == user.id,
                    HumanSession.revoked_at.is_(None),
                )
                .values(revoked_at=datetime.now(timezone.utc))
            )
    if request.password is not None:
        user.password_hash = await _hash_password_async(request.password)
        await session.execute(
            update(HumanSession)
            .where(
                HumanSession.user_id == user.id,
                HumanSession.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(timezone.utc))
        )
    await session.commit()
    return user_out(user)


async def _issue_session(
    session: AsyncSession,
    user: HumanUser,
    workspace: Workspace,
    *,
    ip_address: str | None,
    user_agent: str | None,
) -> HumanSessionOut:
    raw_token = generate_human_key()
    expires_at = datetime.now(timezone.utc) + timedelta(
        hours=settings.human_session_hours
    )
    session.add(
        HumanSession(
            workspace_id=workspace.id,
            user_id=user.id,
            token_hash=hash_api_key(raw_token),
            expires_at=expires_at,
            user_agent=(user_agent or "")[:2_000] or None,
            ip_address=(ip_address or "")[:64] or None,
        )
    )
    user.last_login_at = datetime.now(timezone.utc)
    await session.commit()
    return HumanSessionOut(
        token=raw_token,
        expires_at=expires_at,
        user=user_out(user),
        workspace=workspace_info(workspace),
    )


async def _provision_account(
    session: AsyncSession,
    *,
    email: str,
    display_name: str,
    workspace_name: str | None,
    password_hash: str,
    google_sub: str | None = None,
    email_verified: bool = False,
) -> tuple[HumanUser, Workspace]:
    existing = await session.scalar(
        select(HumanUser.id).where(HumanUser.email == email)
    )
    if existing is not None:
        raise ApiError(409, "human_user_exists", "this email already has an account")
    clean_name = (workspace_name or f"{display_name}'s workspace").strip()[:128]
    workspace = Workspace(
        name=clean_name,
        api_key_hash=hash_api_key(generate_api_key()),
        tier=settings.signup_default_tier,
    )
    session.add(workspace)
    await session.flush()
    user = HumanUser(
        workspace_id=workspace.id,
        email=email,
        display_name=display_name[:128],
        google_sub=google_sub,
        email_verified=email_verified,
        role="OWNER",
        password_hash=password_hash,
        is_active=True,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise ApiError(409, "human_user_exists", "this email already has an account") from None
    return user, workspace


async def register(
    session: AsyncSession,
    request: HumanRegisterRequest,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> HumanSessionOut:
    if not settings.human_signup_enabled:
        raise ApiError(403, "signup_disabled", "self-service signup is disabled")
    email = request.email.strip().lower()
    client = ip_address or "unknown"
    await _rate_limit_login(
        session,
        f"register-ip:{client}",
        None,
        settings.auth_ip_max_attempts_per_minute,
    )
    await _rate_limit_login(
        session,
        "register-global",
        None,
        settings.signup_global_max_per_minute,
    )
    await _rate_limit_login(
        session,
        f"register:{client}:{email}",
        None,
        settings.human_login_max_attempts_per_minute,
    )
    existing = await session.scalar(
        select(HumanUser.id).where(HumanUser.email == email)
    )
    if existing is not None:
        raise ApiError(
            409,
            "account_exists",
            "unable to create an account with that email; sign in or reset access",
        )
    user, workspace = await _provision_account(
        session,
        email=email,
        display_name=request.display_name.strip(),
        workspace_name=request.workspace_name,
        password_hash=await _hash_password_async(request.password),
    )
    return await _issue_session(
        session, user, workspace, ip_address=ip_address, user_agent=user_agent
    )


async def _verify_google_credential(credential: str) -> dict:
    if not settings.google_client_id:
        raise ApiError(
            503,
            "google_login_not_configured",
            "Google login is not configured on this deployment",
        )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"id_token": credential},
            )
    except Exception as exc:  # noqa: BLE001 - external identity boundary
        raise ApiError(
            503, "google_unavailable", "Google login is temporarily unavailable"
        ) from exc
    if response.status_code != 200:
        raise ApiError(401, "google_token_invalid", "Google credential is invalid")
    data = response.json()
    if data.get("aud") != settings.google_client_id:
        raise ApiError(
            401, "google_audience_mismatch", "Google credential audience mismatch"
        )
    verified = data.get("email_verified")
    if verified is not True and str(verified).lower() not in ("true", "1"):
        raise ApiError(
            401, "google_email_unverified", "Google email is not verified"
        )
    if not data.get("email") or not data.get("sub"):
        raise ApiError(401, "google_token_invalid", "Google credential is incomplete")
    return data


async def google_login(
    session: AsyncSession,
    request: GoogleLoginRequest,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> HumanSessionOut:
    client = ip_address or "unknown"
    await _rate_limit_login(
        session,
        f"google-ip:{client}",
        None,
        settings.auth_ip_max_attempts_per_minute,
    )
    data = await _verify_google_credential(request.credential)
    email = str(data["email"]).strip().lower()
    google_sub = str(data["sub"])
    await _rate_limit_login(
        session,
        f"google:{client}:{email}",
        None,
        settings.human_login_max_attempts_per_minute,
    )
    user = (
        await session.execute(select(HumanUser).where(HumanUser.email == email))
    ).scalar_one_or_none()
    if user is not None:
        if not user.is_active:
            raise Forbidden("this human account is disabled")
        if user.google_sub is None:
            raise ApiError(
                409,
                "account_link_required",
                "this email already has a password account; sign in with the "
                "password and link Google from account settings",
            )
        if not hmac.compare_digest(user.google_sub, google_sub):
            raise ApiError(
                401,
                "google_identity_mismatch",
                "this Google identity does not match the linked account",
            )
        workspace = await session.get(Workspace, user.workspace_id)
        if workspace is None:
            raise NotFound("workspace")
        return await _issue_session(
            session, user, workspace, ip_address=ip_address, user_agent=user_agent
        )
    if not settings.human_signup_enabled:
        raise ApiError(403, "signup_disabled", "self-service signup is disabled")
    display_name = (
        request.display_name or data.get("name") or email.split("@", 1)[0]
    ).strip()[:128]
    user, workspace = await _provision_account(
        session,
        email=email,
        display_name=display_name,
        workspace_name=request.workspace_name,
        password_hash=await _hash_password_async(secrets.token_urlsafe(32)),
        google_sub=google_sub,
        email_verified=True,
    )
    return await _issue_session(
        session, user, workspace, ip_address=ip_address, user_agent=user_agent
    )


async def change_password(
    session: AsyncSession,
    user_id: uuid.UUID,
    current_password: str,
    new_password: str,
    *,
    current_session_id: uuid.UUID | None = None,
) -> None:
    user = await session.get(HumanUser, user_id)
    if user is None or not user.is_active:
        raise NotFound("human_user")
    if not await _verify_password_async(current_password, user.password_hash):
        raise ApiError(401, "invalid_credentials", "current password is incorrect")
    user.password_hash = await _hash_password_async(new_password)
    query = update(HumanSession).where(HumanSession.user_id == user.id)
    if current_session_id is not None:
        query = query.where(HumanSession.id != current_session_id)
    await session.execute(query.values(revoked_at=datetime.now(timezone.utc)))
    await session.commit()


async def link_google(
    session: AsyncSession,
    user_id: uuid.UUID,
    credential: str,
) -> HumanUserOut:
    data = await _verify_google_credential(credential)
    user = await session.get(HumanUser, user_id)
    if user is None or not user.is_active:
        raise NotFound("human_user")
    email = str(data["email"]).strip().lower()
    if email != user.email:
        raise ApiError(
            409,
            "google_email_mismatch",
            "the Google email does not match this account",
        )
    google_sub = str(data["sub"])
    if user.google_sub is not None and not hmac.compare_digest(
        user.google_sub, google_sub
    ):
        raise ApiError(
            409, "google_identity_conflict", "another Google identity is linked"
        )
    duplicate = await session.scalar(
        select(HumanUser.id).where(
            HumanUser.google_sub == google_sub,
            HumanUser.id != user.id,
        )
    )
    if duplicate is not None:
        raise ApiError(
            409, "google_identity_conflict", "this Google identity is already linked"
        )
    user.google_sub = google_sub
    user.email_verified = True
    await session.commit()
    return user_out(user)


async def login(
    session: AsyncSession,
    request: HumanLoginRequest,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> HumanSessionOut:
    email = request.email.strip().lower()
    user = (
        await session.execute(
            select(HumanUser).where(HumanUser.email == email)
        )
    ).scalar_one_or_none()
    client = ip_address or "unknown"
    await _rate_limit_login(
        session,
        f"login-ip:{client}",
        None,
        settings.auth_ip_max_attempts_per_minute,
    )
    await _rate_limit_login(
        session,
        f"{client}:{email}",
        user.workspace_id if user else None,
        settings.human_login_max_attempts_per_minute,
    )
    encoded = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    valid = await _verify_password_async(request.password, encoded)
    if user is None or not valid:
        raise ApiError(401, "invalid_credentials", "email or password is incorrect")
    if not user.is_active:
        await session.commit()
        raise Forbidden("this human account is disabled")
    workspace = await session.get(Workspace, user.workspace_id)
    if workspace is None:
        raise NotFound("workspace")
    return await _issue_session(
        session, user, workspace, ip_address=ip_address, user_agent=user_agent
    )


async def logout(session: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
    result = await session.execute(
        update(HumanSession)
        .where(
            HumanSession.id == session_id,
            HumanSession.user_id == user_id,
            HumanSession.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(timezone.utc))
    )
    if not result.rowcount:
        raise NotFound("human_session")
    await session.commit()


async def me(
    session: AsyncSession, user_id: uuid.UUID, workspace: Workspace
) -> HumanMeResponse:
    user = await session.get(HumanUser, user_id)
    if user is None:
        raise NotFound("human_user")
    return HumanMeResponse(
        user=user_out(user),
        workspace=workspace_info(workspace),
        role=principal_for_human(user, workspace).role.value,
    )
