"""Path-level write policy and JSON Schema gates."""

from __future__ import annotations

import fnmatch
import uuid
from typing import Any

import jsonschema
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.errors import ApiError, Forbidden, NotFound, ProposalRequired
from app.models import PathPolicy
from app.schemas import PolicyCreateRequest, PolicyOut, PolicyUpdateRequest


def _policy_out(policy: PathPolicy) -> PolicyOut:
    return PolicyOut(
        id=policy.id,
        name=policy.name,
        resource_type=policy.resource_type,
        pattern=policy.pattern,
        priority=policy.priority,
        require_proposal=policy.require_proposal,
        allowed_roles=list(policy.allowed_roles or []),
        json_schema=policy.json_schema or {},
        max_content_bytes=policy.max_content_bytes,
        require_verified_sources=policy.require_verified_sources,
        active=policy.active,
        created_at=policy.created_at,
        updated_at=policy.updated_at,
    )


async def list_policies(
    session: AsyncSession, workspace_id: uuid.UUID
) -> list[PolicyOut]:
    rows = (
        await session.execute(
            select(PathPolicy)
            .where(PathPolicy.workspace_id == workspace_id)
            .order_by(PathPolicy.resource_type, PathPolicy.priority.desc())
        )
    ).scalars().all()
    return [_policy_out(row) for row in rows]


async def resolve_policy(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    resource_type: str,
    key: str,
) -> PathPolicy | None:
    rows = (
        await session.execute(
            select(PathPolicy).where(
                PathPolicy.workspace_id == workspace_id,
                PathPolicy.resource_type == resource_type,
                PathPolicy.active.is_(True),
            )
        )
    ).scalars().all()
    matches = [row for row in rows if fnmatch.fnmatchcase(key, row.pattern)]
    if not matches:
        return None
    matches.sort(key=lambda row: (row.priority, len(row.pattern)), reverse=True)
    return matches[0]


def _schema_errors(instance: Any, schema: dict[str, Any]) -> list[str]:
    validator = jsonschema.Draft202012Validator(schema)
    errors: list[str] = []
    for error in sorted(validator.iter_errors(instance), key=lambda item: list(item.path)):
        path = ".".join(str(part) for part in error.path) or "$"
        errors.append(f"{path}: {error.message}")
        if len(errors) >= 20:
            break
    return errors


async def enforce_policy(
    session: AsyncSession,
    principal: Principal,
    *,
    resource_type: str,
    key: str,
    action: str,
    payload: Any,
    content_bytes: int | None = None,
) -> PathPolicy | None:
    """Enforce the most specific active policy for a resource path.

    ``action`` is create/write/propose. Direct agent writes to a path with
    ``require_proposal`` are rejected so the change goes through review.
    Humans may still use the direct human endpoints, but schema checks apply.
    """
    policy = await resolve_policy(
        session, principal.workspace.id, resource_type, key
    )
    if policy is None:
        return None
    role = principal.role.value
    if not principal.is_human and policy.allowed_roles:
        if role not in policy.allowed_roles:
            raise Forbidden(
                f"path policy '{policy.name}' does not allow role {role}"
            )
    if (
        policy.require_proposal
        and not principal.is_human
        and action in ("create", "write")
    ):
        raise ProposalRequired(key, policy.name)
    if (
        content_bytes is not None
        and policy.max_content_bytes is not None
        and content_bytes > policy.max_content_bytes
    ):
        raise ApiError(
            422,
            "policy_size_limit",
            f"path policy '{policy.name}' limits content to "
            f"{policy.max_content_bytes} bytes",
            details={"policy": policy.name, "limit": policy.max_content_bytes},
        )
    if policy.json_schema:
        errors = _schema_errors(payload, policy.json_schema)
        if errors:
            raise ApiError(
                422,
                "policy_schema_violation",
                f"path policy '{policy.name}' rejected the payload",
                details={"policy": policy.name, "errors": errors},
            )
    return policy


async def create_policy(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    request: PolicyCreateRequest,
) -> PolicyOut:
    policy = PathPolicy(
        workspace_id=workspace_id,
        name=request.name,
        resource_type=request.resource_type,
        pattern=request.pattern,
        priority=request.priority,
        require_proposal=request.require_proposal,
        allowed_roles=list(request.allowed_roles),
        json_schema=request.json_schema,
        max_content_bytes=request.max_content_bytes,
        require_verified_sources=request.require_verified_sources,
        active=request.active,
    )
    session.add(policy)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise ApiError(
            409,
            "policy_exists",
            "a policy for this resource type and pattern already exists",
        ) from None
    return _policy_out(policy)


async def update_policy(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    policy_id: uuid.UUID,
    request: PolicyUpdateRequest,
) -> PolicyOut:
    policy = await session.get(PathPolicy, policy_id)
    if policy is None or policy.workspace_id != workspace_id:
        raise NotFound("path_policy")
    payload = request.model_dump(exclude_unset=True)
    if "allowed_roles" in payload:
        payload["allowed_roles"] = list(payload["allowed_roles"])
    for key, value in payload.items():
        setattr(policy, key, value)
    await session.commit()
    return _policy_out(policy)


async def delete_policy(
    session: AsyncSession, workspace_id: uuid.UUID, policy_id: uuid.UUID
) -> None:
    policy = await session.get(PathPolicy, policy_id)
    if policy is None or policy.workspace_id != workspace_id:
        raise NotFound("path_policy")
    await session.delete(policy)
    await session.commit()
