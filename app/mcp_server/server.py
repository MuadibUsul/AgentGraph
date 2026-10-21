"""Native MCP server for the v3.2.1 shared blackboard workspace."""

from __future__ import annotations

import json
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime
from typing import Annotated, Any, Literal

from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field, TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app import agent_guide, services, warnings_context
from app.auth import (
    PERMISSIONS,
    AuthKind,
    Principal,
    ensure_tenant_usable,
    resolve_principal,
)
from app.config import get_settings
from app.database import async_session_factory
from app.error_registry import classify, protocol_docs, retry_after_ms
from app.errors import ApiError
from app.models import Tenant
from app.schemas import (
    REVIEW_APPROVE_STATUSES,
    REVIEW_REJECT_STATUSES,
    DocumentArchiveRequest,
    DocumentBatchUpsertItem,
    DocumentBatchUpsertRequest,
    DocumentQueryRequest,
    DocumentRenameRequest,
    DocumentSectionPatchRequest,
    DocumentUpsertRequest,
    ExternalActionCreateRequest,
    HandoffBrief,
    ProposalCreateRequest,
    ProposalVoteRequest,
    RegisterPartsRequest,
    SlotPatchRequest,
    TaskClaimRequest,
    TaskCreateRequest,
    TaskEscalateRequest,
    TaskHandoffRequest,
    TaskReviewRequest,
)
from app.services import addresses as address_service
from app.services import budget as budget_service
from app.services import idempotency
from app.services import lineage as lineage_service
from app.services import orientation as orientation_service
from app.services.common import set_event_platform
from app.services.lineage import stamp_for
from app.stamp_context import set_current_stamp
from app.tenant import activate_tenant
from app.versioning import current_version

logger = logging.getLogger(__name__)
settings = get_settings()

_request_principal: ContextVar[Principal | None] = ContextVar(
    "agentgraph_mcp_principal", default=None
)
_request_credential: ContextVar[str | None] = ContextVar(
    "agentgraph_mcp_credential", default=None
)
_stdio_principal: Principal | None = None
_current_tool: ContextVar[str | None] = ContextVar("agentgraph_mcp_tool", default=None)

# Write tools that take a generic ``idempotency_key`` (PRD AG-ST-18 point 1).
# draft_external_action keeps its own action-level idempotency_key.
IDEMPOTENT_TOOLS = frozenset(
    {
        "create_task",
        "review_task",
        "request_human_review",
        "agent_claim_and_read",
        "agent_patch_shared_state",
        "agent_handoff_task",
        "agent_escalate_to_human",
        "upsert_shared_doc",
        "upsert_many",
        "patch_doc_section",
        "propose_document_change",
        "vote_on_proposal",
        "archive_document",
        "restore_document",
        "register_namespace",
        "rename_document",
        "register_parts",
        "ack_constitution",
    }
)
_CONTENT_BLOCK = TypeAdapter(types.ContentBlock)


def _dump_result(result: Any) -> dict[str, Any]:
    def blocks(items: Any) -> list[Any]:
        return [item.model_dump(mode="json", by_alias=True) for item in items]

    if isinstance(result, tuple) and len(result) == 2:
        return {"kind": "tuple", "content": blocks(result[0]), "structured": result[1]}
    if isinstance(result, dict):
        return {"kind": "dict", "structured": result}
    return {"kind": "content", "content": blocks(result)}


def _mark_replayed_text(block: Any) -> Any:
    if getattr(block, "type", None) == "text":
        try:
            payload = json.loads(block.text)
        except (TypeError, ValueError):
            return block
        if isinstance(payload, dict):
            payload["replayed"] = True
            return types.TextContent(
                type="text", text=json.dumps(payload, ensure_ascii=False, default=str)
            )
    return block


def _merge_text_block(block: Any, collected: list[dict[str, Any]]) -> Any:
    if getattr(block, "type", None) != "text":
        return block
    try:
        payload = json.loads(block.text)
    except (TypeError, ValueError):
        return block
    if not isinstance(payload, dict):
        return block
    return types.TextContent(
        type="text",
        text=json.dumps(
            warnings_context.merge_warnings(payload, collected), ensure_ascii=False, default=str
        ),
    )


def _with_warnings(result: Any, collected: list[dict[str, Any]]) -> Any:
    """Attach soft-constraint warnings (PRD §6 warnings[]) to a tool result."""
    if not collected or isinstance(result, types.CallToolResult):
        return result
    if isinstance(result, tuple) and len(result) == 2:
        content, structured = result
        return (
            [_merge_text_block(block, collected) for block in content],
            warnings_context.merge_warnings(structured, collected),
        )
    if isinstance(result, dict):
        return warnings_context.merge_warnings(result, collected)
    return [_merge_text_block(block, collected) for block in result]


def _load_result(stored: dict[str, Any]) -> Any:
    content = [
        _mark_replayed_text(_CONTENT_BLOCK.validate_python(item))
        for item in stored.get("content") or []
    ]
    structured = stored.get("structured")
    if isinstance(structured, dict):
        structured = {**structured, "replayed": True}
    if stored.get("kind") == "tuple":
        return content, structured
    if stored.get("kind") == "dict":
        return structured
    return content

def _error_envelope(exc: ToolError) -> dict[str, Any]:
    """Turn FastMCP/pydantic/tool errors into one JSON envelope.

    FastMCP wraps argument validation and tool exceptions in ``ToolError`` and
    the low-level server normally returns only text. We inspect the cause so
    both SDK-level argument errors and our own structured errors keep the same
    shape in text and ``structuredContent``.
    """
    cause = exc.__cause__
    raw_text = str(cause) if isinstance(cause, ToolError) else str(exc)
    if raw_text.startswith("Unknown tool:"):
        tool_name = raw_text.split(":", 1)[1].strip()
        return {
            "code": "unknown_tool",
            "http_status": 404,
            "message": f"unknown tool: {tool_name}",
        }
    if isinstance(cause, ValidationError):
        return {
            "code": "validation_error",
            "http_status": 422,
            "message": "invalid arguments",
            "details": [
                {
                    "loc": list(error.get("loc", ())),
                    "msg": error.get("msg", "invalid value"),
                    "type": error.get("type", "value_error"),
                }
                for error in cause.errors()
            ],
        }
    raw = str(cause) if isinstance(cause, ToolError) else str(exc)
    if raw.startswith("Error executing tool ") and ": " in raw:
        raw = raw.split(": ", 1)[1]
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict) and "code" in payload:
            payload.setdefault("http_status", 400)
            payload.setdefault("message", "tool error")
            return payload
    except (TypeError, ValueError):
        pass
    if isinstance(cause, ToolError):
        return {
            "code": "tool_error",
            "http_status": 400,
            "message": raw,
        }
    return {
        "code": "internal_error",
        "http_status": 500,
        "message": "internal tool error",
        "details": {"type": type(cause).__name__ if cause is not None else type(exc).__name__},
    }


class AgentGraphFastMCP(FastMCP):
    """FastMCP with a structured error result for every failed tool call."""

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> types.CallToolResult | Any:
        _current_tool.set(name)
        collected = warnings_context.begin_collection()
        result = await self._dispatch(name, arguments)
        return _with_warnings(result, collected)

    async def _dispatch(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            key = None
            if name in IDEMPOTENT_TOOLS and isinstance(arguments, dict):
                arguments = dict(arguments)
                key = arguments.pop("idempotency_key", None)
            if key is None:
                return await super().call_tool(name, arguments)
            return await self._call_idempotent(name, arguments, key)
        except ToolError as exc:
            envelope = _error_envelope(exc)
            for key, value in classify(
                envelope["code"],
                int(envelope.get("http_status") or 400),
                details=envelope.get("details"),
            ).items():
                envelope.setdefault(key, value)
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(envelope, ensure_ascii=False, default=str),
                    )
                ],
                structuredContent=envelope,
                isError=True,
            )

    async def _call_idempotent(
        self, name: str, arguments: dict[str, Any], raw_key: Any
    ) -> Any:
        credential = _request_credential.get() or settings.mcp_api_key
        if not isinstance(raw_key, str) or not credential:
            raise ToolError(
                json.dumps(
                    {
                        "code": "validation_error",
                        "http_status": 422,
                        "message": "idempotency_key must be a string",
                        "details": {"field": "idempotency_key"},
                    }
                )
            )
        try:
            key = idempotency.validate_key(raw_key)
            principal = idempotency.credential_hash(credential)
            tool = f"mcp:{name}"
            body_hash = idempotency.request_hash(tool, current_version(), arguments)
            replay = await idempotency.begin(principal, tool, key, body_hash)
        except idempotency.UnknownCredential:
            return await super().call_tool(name, arguments)
        except ApiError as exc:
            raise ToolError(str(_api_error(exc))) from exc
        if replay is not None:
            return _load_result(replay.response or {})
        try:
            result = await super().call_tool(name, arguments)
        except BaseException:
            await idempotency.abort(principal, tool, key)
            raise
        try:
            await idempotency.complete(principal, tool, key, 200, _dump_result(result))
        except Exception:  # noqa: BLE001 - bookkeeping must not fail a finished call
            logger.exception("could not store MCP idempotency record")
            await idempotency.abort(principal, tool, key)
        return result


mcp = AgentGraphFastMCP(
    name="AgentGraph",
    instructions=(
        "A human-in-the-loop shared blackboard for multi-agent teams. "
        "FIRST CALL get_orientation: it returns your identity, the constitution, "
        "namespaces, your tasks and next_calls. "
        "Use workspace_sync for the L0 map, create_task to create work, "
        "agent_claim_and_read to claim a task and read only L1 briefs, "
        "agent_patch_shared_state for JSON Patch updates to leases, "
        "agent_handoff_task to submit an L1 brief plus an optional L2 artifact, "
        "review_task to approve/reject, agent_escalate_to_human for conflicts, "
        "upsert_shared_doc / patch_doc_section for collaborative documents, "
        "query_workspace for structured/semantic discovery, "
        "propose_document_change + vote_on_proposal for reviewed/arena edits, "
        "and request_human_review when a human must apply a proposal or decide "
        "a task. Proposals are human-applied by design. Tainted documents are "
        "excluded by default and cannot be agent-verified. Always pass one stable "
        "session_batch_id for all calls in a work session so humans can undo "
        "the whole batch. Respect 403 HUMAN_SOVEREIGNTY_LOCKED and 409 "
        "version/lease conflicts; re-read before retrying."
    ),
    streamable_http_path="/",
    stateless_http=settings.mcp_stateless_http,
    json_response=settings.mcp_json_response,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=settings.mcp_dns_rebinding_protection,
        allowed_hosts=settings.mcp_allowed_hosts
        or [
            "127.0.0.1",
            "127.0.0.1:*",
            "localhost",
            "localhost:*",
            "[::1]",
            "[::1]:*",
        ],
        allowed_origins=settings.mcp_allowed_origins
        or [
            "http://127.0.0.1",
            "http://127.0.0.1:*",
            "http://localhost",
            "http://localhost:*",
            "http://[::1]",
            "http://[::1]:*",
        ],
    ),
)


class MCPAuthMiddleware:
    """Resolve the workspace/agent bearer key before MCP handles a request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_headers = Headers(scope=scope)
        authorization = request_headers.get("authorization")
        raw_key = ""
        if authorization:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() == "bearer":
                raw_key = token.strip()
        if not raw_key:
            raw_key = (request_headers.get("x-api-key") or "").strip()
        if not raw_key:
            await self._unauthorized(scope, receive, send)
            return
        async with async_session_factory() as session:
            principal = await resolve_principal(
                session,
                raw_key,
                agent_hint=request_headers.get("x-agent-signature"),
            )
        if principal is None:
            await self._unauthorized(scope, receive, send)
            return
        token = _request_principal.set(principal)
        credential_token = _request_credential.set(raw_key)
        try:
            await self.app(scope, receive, send)
        finally:
            _request_credential.reset(credential_token)
            _request_principal.reset(token)

    @staticmethod
    async def _unauthorized(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            {"error": {"code": "unauthorized", "message": "invalid API key"}},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


async def _principal_for_call(session: AsyncSession) -> Principal:
    principal = _request_principal.get()
    if principal is None:
        global _stdio_principal
        if _stdio_principal is not None:
            principal = _stdio_principal
        else:
            if not settings.mcp_api_key:
                raise _tool_error(
                    "mcp_not_configured",
                    "MCP_API_KEY is not configured. Use a workspace or agent API key.",
                    500,
                )
            principal = await resolve_principal(
                session, settings.mcp_api_key, agent_hint=settings.mcp_agent_name
            )
            if principal is None:
                raise _tool_error(
                    "invalid_api_key", "MCP_API_KEY is invalid or inactive", 401
                )
            _stdio_principal = principal
    # Re-read the tenant on every call so suspension/deletion apply at once,
    # including to the long-lived stdio principal.
    principal.tenant = await session.get(Tenant, principal.workspace.tenant_id)
    if principal.tenant is None or principal.tenant.status == "deleted":
        raise _tool_error("unauthorized", "invalid or inactive API key", 401)
    tool_name = _current_tool.get()
    try:
        ensure_tenant_usable(
            principal,
            write=tool_name in IDEMPOTENT_TOOLS or tool_name == "draft_external_action",
            path=f"mcp:{tool_name}",
        )
    except ApiError as exc:
        raise _api_error(exc) from exc
    set_current_stamp(await stamp_for(principal))
    await activate_tenant(session, principal.workspace.id, principal.tenant_scope)
    from app.services.common import set_audit_actor
    from app.services.usage import enforce_rate_limit

    set_audit_actor(principal)
    await enforce_rate_limit(session, principal)
    set_event_platform("mcp")
    return principal


def _actor(principal: Principal, provided: str | None) -> str:
    if principal.auth_kind is AuthKind.AGENT:
        return principal.agent_signature
    if provided and provided.strip():
        return provided.strip()[:128]
    return settings.mcp_agent_name


def _batch(value: uuid.UUID | None) -> uuid.UUID | None:
    return value


def _compact(payload: Any) -> dict[str, Any]:
    return json.loads(json.dumps(payload, ensure_ascii=False, default=str))


def _tool_error(
    code: str,
    message: str,
    http_status: int = 400,
    details: Any | None = None,
) -> ToolError:
    error: dict[str, Any] = {
        "code": code,
        "http_status": http_status,
        "message": message,
    }
    if details is not None:
        error["details"] = details
    return ToolError(json.dumps(error, ensure_ascii=False, default=str))


def _validation_error(exc: ValidationError) -> ToolError:
    details = [
        {
            "loc": list(error.get("loc", ())),
            "msg": error.get("msg", "invalid value"),
            "type": error.get("type", "value_error"),
        }
        for error in exc.errors()
    ]
    return _tool_error(
        "validation_error",
        "invalid arguments",
        422,
        details,
    )


def _api_error(exc: ApiError) -> ToolError:
    details = exc.details
    delay = retry_after_ms(exc.headers)
    if delay is not None:
        # MCP has no headers: carry Retry-After as retry_after_ms (PRD §6).
        details = {**(details if isinstance(details, dict) else {}), "retry_after_ms": delay}
        if exc.details is not None and not isinstance(exc.details, dict):
            details["detail"] = exc.details
    return _tool_error(exc.code, exc.message, exc.status_code, details)


# ------------------------------------------------------------------- tools


@mcp.tool(name="get_orientation")
async def get_orientation(
    sections: list[str] | None = None,
    max_bytes: Annotated[int, Field(ge=4_096, le=131_072)] = 32_768,
) -> dict[str, Any]:
    """Call this first after connecting.

    Returns who you are (role, token, scopes), the workspace constitution,
    the namespace map with your access, collaborators, your open tasks,
    claimable tasks, required reading (constitution first), an event cursor,
    the conventions and limits, and next_calls: ready-to-run calls that follow
    the rules. Over max_bytes, optional detail is truncated (marked truncated).
    """
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            result = await orientation_service.build(
                session, principal, sections=sections, max_bytes=max_bytes
            )
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result)


@mcp.tool(name="ack_constitution")
async def ack_constitution(version: int) -> dict[str, Any]:
    """Record that you read this constitution version (get_orientation.ack_required)."""
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            result = await orientation_service.ack_constitution(session, principal, version)
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result)


@mcp.tool(name="workspace_sync")
async def workspace_sync(
    focus_keys: list[str] | None = None,
    include_slots: bool = True,
    task_statuses: list[str] | None = None,
    namespace: str | None = None,
    compact: bool = False,
    since: str | None = None,
    include_unverified: bool = False,
    include_tainted: bool = False,
) -> dict[str, Any]:
    """Load the L0 blackboard map without raw document/state payloads.

    Returns workspace counts, task cards (IDs/status/role/lease), an ``outputs``
    list with completed task artifact URIs and handoff summaries, slot keys and
    versions with human-lock flags, the document tree and the current role's
    permissions. Use focus_keys to narrow to specific task/doc/slot keys before
    loading L1/L2. Set compact=true for a lower-token L0 view.

    Pass since=<next_cursor from a previous call> for incremental sync:
    tasks/documents/slots and timeline contain only changes after the cursor,
    and namespace_counts gives per-namespace totals. A null/omitted since does
    a full map. The response always includes next_cursor for the next call.
    """
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            board = await services.workspace_map(
                session,
                principal,
                since=since,
                include_unverified=include_unverified,
                include_tainted=include_tainted,
            )
        except ApiError as exc:
            raise _api_error(exc) from exc
    payload = board.model_dump(mode="json")
    payload["permissions"] = sorted(PERMISSIONS.get(principal.role, set()))
    focus = set(focus_keys or [])
    if focus:
        payload["tasks"] = [
            item
            for item in payload["tasks"]
            if item["task_key"] in focus or item["id"] in focus
        ]
        payload["slots"] = [
            item for item in payload["slots"] if item["slot_key"] in focus
        ]
        payload["documents"] = [
            item for item in payload["documents"] if item["doc_key"] in focus
        ]
    if task_statuses:
        wanted = {status.upper() for status in task_statuses}
        payload["tasks"] = [
            item for item in payload["tasks"] if item["status"] in wanted
        ]
    if namespace:
        for field in ("tasks", "slots", "documents"):
            payload[field] = [
                item for item in payload[field] if item.get("namespace") == namespace
            ]
    visible_tasks = {item["task_key"] for item in payload["tasks"]}
    payload["outputs"] = [
        item for item in payload.get("outputs", []) if item["task_key"] in visible_tasks
    ]
    if not include_slots:
        payload["slots"] = []
    if compact:
        payload["tasks"] = [
            {
                key: item.get(key)
                for key in (
                    "task_key", "title", "status", "priority", "assigned_role",
                    "namespace", "claimed_by", "lease_active", "version",
                    "handoff_summary", "artifact_uri", "reviewed_by",
                )
            }
            for item in payload["tasks"]
        ]
        payload["slots"] = [
            {
                key: item.get(key)
                for key in (
                    "slot_key", "namespace", "version", "lease_active",
                    "locked_by", "locked_by_human",
                )
            }
            for item in payload["slots"]
        ]
        payload["documents"] = [
            {
                key: item.get(key)
                for key in (
                    "doc_key", "title", "namespace", "version",
                    "verification_status", "trust_level", "tainted",
                )
            }
            for item in payload["documents"]
        ]
    return _compact(payload)


@mcp.tool(name="create_task")
async def create_task(
    title: str,
    task_key: str | None = None,
    description: str | None = None,
    assigned_role: Literal["ANALYST", "AUDITOR", "EXECUTOR"] | None = None,
    priority: int = 0,
    namespace: str = "default",
    input_context: dict[str, Any] | None = None,
    depends_on: list[str] | None = None,
    max_retries: int | None = None,
    status: Literal["TODO", "READY"] = "READY",
    agent: str | None = None,
    session_batch_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Create a task for an agent role (EXECUTOR/ANALYST/AUDITOR).

    SCOUT is intentionally not claimable; raw collection work belongs in a
    raw_inbox document. depends_on task keys create dependency links and keep
    the new task TODO until every dependency is DONE. This mirrors REST
    POST /v1/tasks.
    """
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            body = TaskCreateRequest(
                task_key=task_key,
                title=title,
                description=description,
                status=status,
                priority=priority,
                assigned_role=assigned_role,
                namespace=namespace,
                input_context=input_context or {},
                depends_on=depends_on or [],
                max_retries=max_retries or settings.default_max_retries,
                agent=_actor(principal, agent) if principal.is_commander else agent,
                session_batch_id=_batch(session_batch_id),
            )
            result = await services.create_task(session, principal, body)
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result.model_dump(mode="json"))


@mcp.tool(name="review_task")
async def review_task(
    task_key: str,
    decision: Literal["approve", "reject"],
    feedback: str | None = None,
    expected_version: int | None = None,
    next_status: Annotated[
        str | None,
        Field(description=TaskReviewRequest.model_fields["next_status"].description),
    ] = None,
    agent: str | None = None,
    session_batch_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Approve or reject a task in REVIEWING (AUDITOR/human) or a human-state task.

    AWAITING_HUMAN and ESCALATED_TO_HUMAN require an actual human account;
    workspace automation keys cannot resolve them. Rejections increment the
    ping-pong counter and may escalate to human.
    """
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            body = TaskReviewRequest(
                decision=decision,
                feedback=feedback,
                expected_version=expected_version,
                next_status=next_status,
                agent=_actor(principal, agent) if principal.is_commander else agent,
                session_batch_id=_batch(session_batch_id),
            )
            result = await services.review_task(session, principal, task_key, body)
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result.model_dump(mode="json"))


@mcp.tool(name="request_human_review")
async def request_human_review(
    kind: Literal["proposal", "task"],
    key: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Explicitly hand a proposal or task to a human operator.

    Agents cannot apply proposals; this tool records a HUMAN_REVIEW_REQUESTED
    timeline event and returns the exact human action endpoint. For tasks it is
    also useful before a human approval/rejection decision.
    """
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            if kind == "proposal":
                proposal = await services.get_proposal(
                    session, principal, uuid.UUID(key)
                )
                if proposal.status != "PENDING":
                    raise _tool_error(
                        "proposal_not_pending",
                        f"proposal is {proposal.status.lower()}, not pending",
                        409,
                        {"proposal_id": str(proposal.id), "status": proposal.status},
                    )
                document = await services.get_document(
                    session, principal, proposal.doc_key
                )
                await services.append_event(
                    session,
                    workspace_id=principal.workspace.id,
                    agent=principal.agent_signature,
                    event_type="HUMAN_REVIEW_REQUESTED",
                    payload={
                        "resource": "proposal",
                        "proposal_id": str(proposal.id),
                        "doc_key": proposal.doc_key,
                        "note": note,
                    },
                    namespace=document.namespace,
                )
                await session.commit()
                return _compact(
                    {
                        "resource": "proposal",
                        "proposal_id": str(proposal.id),
                        "doc_key": proposal.doc_key,
                        "status": proposal.status,
                        "requested": True,
                        "next_step": (
                            "A human must apply or reject this proposal with "
                            f"POST /v1/proposals/{proposal.id}/apply or /reject"
                        ),
                    }
                )
            task = await services.get_task(session, principal, key)
            if task.status.value in ("DONE", "FAILED"):
                raise _tool_error(
                    "task_not_reviewable",
                    f"task is already {task.status.value.lower()}",
                    409,
                    {"task_key": task.task_key, "status": task.status.value},
                )
            await services.append_event(
                session,
                workspace_id=principal.workspace.id,
                agent=principal.agent_signature,
                event_type="HUMAN_REVIEW_REQUESTED",
                payload={
                    "resource": "task",
                    "task_key": task.task_key,
                    "status": task.status.value,
                    "note": note,
                },
                namespace=task.namespace,
            )
            await session.commit()
        except ValueError as exc:
            raise _tool_error(
                "validation_error", "proposal key must be a UUID", 422
            ) from exc
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(
        {
            "resource": "task",
            "task_key": task.task_key,
            "status": task.status.value,
            "requested": True,
            "next_step": (
                "A human should review this task in the console or call "
                f"POST /v1/tasks/{task.task_key}/review"
            ),
        }
    )


@mcp.tool(name="agent_claim_and_read")
async def agent_claim_and_read(
    task_key: str,
    lease_seconds: Annotated[
        int,
        Field(
            description=(
                f"Lease TTL in seconds. Default {settings.default_task_lease_seconds}; "
                f"allowed 5–{settings.max_task_lease_seconds}."
            ),
            ge=5,
            le=settings.max_task_lease_seconds,
        ),
    ] = settings.default_task_lease_seconds,
    agent: str | None = None,
    session_batch_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Claim a READY/HANDOFF task with a TTL lease and load its L1 context.

    Returns the task, upstream L1 handoff briefs, referenced state slots and
    document summaries. L2 artifact bodies are intentionally not included;
    call dereference(artifact_uri) only when verification is required.
    """
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            body = TaskClaimRequest(
                lease_seconds=lease_seconds,
                agent=agent,
                session_batch_id=_batch(session_batch_id),
            )
            result = await services.claim_task(
                session, principal, task_key, body
            )
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result.model_dump(mode="json"))


@mcp.tool(name="agent_patch_shared_state")
async def agent_patch_shared_state(
    slot_key: str,
    ops: list[dict[str, Any]],
    expected_version: int,
    lease_epoch: int | None = None,
    lease_seconds: Annotated[
        int,
        Field(
            description=(
                f"Optional lease TTL in seconds. Default {settings.default_task_lease_seconds}; "
                f"allowed 5–{settings.max_task_lease_seconds}."
            ),
            ge=5,
            le=settings.max_task_lease_seconds,
        ),
    ] = settings.default_task_lease_seconds,
    return_current_on_conflict: bool = False,
    agent: str | None = None,
    session_batch_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Apply an RFC 6902 JSON Patch to a shared state slot.

    Only EXECUTOR/COMMANDER roles may write. Human-locked slots return 403
    HUMAN_SOVEREIGNTY_LOCKED. Use expected_version for optimistic concurrency.
    Set lease_seconds to hold a short exclusive lease while working.
    """
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            body = SlotPatchRequest(
                return_current_on_conflict=return_current_on_conflict,
                ops=ops,
                expected_version=expected_version,
                lease_epoch=lease_epoch,
                lease_seconds=lease_seconds,
                agent=_actor(principal, agent) if principal.is_commander else agent,
                session_batch_id=_batch(session_batch_id),
            )
            result = await services.patch_slot(session, principal, slot_key, body)
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result.model_dump(mode="json"))


@mcp.tool(name="agent_handoff_task")
async def agent_handoff_task(
    task_key: str,
    next_status: Literal[
        "REVIEWING", "HANDOFF", "DONE", "AWAITING_HUMAN", "FAILED"
    ],
    brief: HandoffBrief | None = None,
    artifact_text: Annotated[
        str | None,
        Field(description="L2 artifact body; mutually exclusive with artifact_uri"),
    ] = None,
    artifact_uri: Annotated[
        str | None,
        Field(description="doc:// URI; mutually exclusive with artifact_text"),
    ] = None,
    output_artifact: dict[str, Any] | None = None,
    expected_version: int | None = None,
    lease_epoch: int | None = None,
    create_next: dict[str, Any] | None = None,
    human_feedback: str | None = None,
    agent: str | None = None,
    session_batch_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Hand off work with a required L1 brief and optional L2 artifact.

    REVIEWING/HANDOFF require a structured brief with summary, key_metrics,
    unverified_hypotheses and risk_flag (boolean). Agents must use REVIEWING;
    DONE is human-only, and human_feedback is rejected for non-human callers.
    Providing artifact_text stores the L2 body as a document and returns
    artifact_uri (doc://...); artifact_text and artifact_uri are mutually
    exclusive. The lease is released. REVIEWING enters the ping-pong circuit
    breaker on rejection.
    """
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            body = TaskHandoffRequest(
                next_status=next_status,
                brief=brief,
                artifact_text=artifact_text,
                artifact_uri=artifact_uri,
                output_artifact=output_artifact or {},
                expected_version=expected_version,
                lease_epoch=lease_epoch,
                create_next=create_next,
                human_feedback=human_feedback,
                agent=_actor(principal, agent) if principal.is_commander else agent,
                session_batch_id=_batch(session_batch_id),
            )
            result = await services.handoff_task(session, principal, task_key, body)
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result.model_dump(mode="json"))


@mcp.tool(name="agent_escalate_to_human")
async def agent_escalate_to_human(
    task_key: str,
    reason: str,
    expected_version: int | None = None,
    agent: str | None = None,
    session_batch_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Stop and escalate a task to the human commander for a decision."""
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            body = TaskEscalateRequest(
                reason=reason,
                expected_version=expected_version,
                agent=_actor(principal, agent) if principal.is_commander else agent,
                session_batch_id=_batch(session_batch_id),
            )
            result = await services.escalate_task(
                session, principal, task_key, body
            )
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result.model_dump(mode="json"))


@mcp.tool(name="upsert_shared_doc")
async def upsert_shared_doc(
    doc_key: str,
    content: str,
    title: str | None = None,
    frontmatter: dict[str, Any] | None = None,
    expected_version: int = 0,
    namespace: str | None = None,
    include_content: bool = False,
    return_current_on_conflict: bool = False,
    agent: str | None = None,
    session_batch_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Create or update a shared Markdown document with JSON properties.

    expected_version=0 creates; otherwise pass the current version. Human-locked
    documents reject agent writes. SCOUT tokens may only create documents in
    their allowed namespaces. By default the response is compact (doc_key,
    version, content_hash, status); set include_content=true to return the body.
    """
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            body = DocumentUpsertRequest(
                return_current_on_conflict=return_current_on_conflict,
                title=title,
                content=content,
                frontmatter=frontmatter or {},
                expected_version=expected_version,
                namespace=namespace,
                agent=_actor(principal, agent) if principal.is_commander else agent,
                session_batch_id=_batch(session_batch_id),
            )
            result = await services.upsert_document(
                session, principal, doc_key, body
            )
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        except ApiError as exc:
            raise _api_error(exc) from exc
    payload = result.model_dump(mode="json")
    if not include_content:
        document = payload.get("document", {})
        payload["document"] = {
            key: document.get(key)
            for key in (
                "id", "doc_key", "version", "content_hash",
                "verification_status", "trust_level", "namespace", "updated_at",
            )
        }
    return _compact(payload)


@mcp.tool(name="patch_doc_section")
async def patch_doc_section(
    doc_key: str,
    section_heading: str,
    new_markdown: str,
    expected_version: int,
    include_content: bool = False,
    return_current_on_conflict: bool = False,
    agent: str | None = None,
    session_batch_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Replace only one Markdown section (by heading) instead of the whole doc.

    The response is compact unless include_content=true.
    """
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            body = DocumentSectionPatchRequest(
                return_current_on_conflict=return_current_on_conflict,
                section_heading=section_heading,
                new_markdown=new_markdown,
                expected_version=expected_version,
                agent=_actor(principal, agent) if principal.is_commander else agent,
                session_batch_id=_batch(session_batch_id),
            )
            result = await services.patch_document_section(
                session, principal, doc_key, body
            )
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        except ApiError as exc:
            raise _api_error(exc) from exc
    payload = result.model_dump(mode="json")
    if not include_content:
        document = payload.get("document", {})
        payload["document"] = {
            key: document.get(key)
            for key in (
                "id", "doc_key", "version", "content_hash",
                "verification_status", "trust_level", "namespace", "updated_at",
            )
        }
    return _compact(payload)


@mcp.tool(name="query_workspace")
async def query_workspace(
    query: str | None = None,
    filters: dict[str, Any] | None = None,
    namespace: str | None = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
    include_unverified: bool = False,
    include_own_unverified: bool = True,
    include_tainted: bool = False,
    include_archived: bool = False,
    mode: str = "hybrid",
) -> dict[str, Any]:
    """Structured + semantic document discovery (semantic search, NOT exhaustive).

    This ranks the best matches; it does not guarantee completeness. To
    enumerate every document in a namespace use list_documents (cursor
    pagination); for the L0 map and incremental changes use workspace_sync.
    limit defaults to 10, at most 50.

    include_own_unverified (default true) keeps your own DRAFTs in the results
    so a writer can retrieve what it just created; include_unverified (default
    false) adds other agents' unverified drafts. Each hit carries is_own,
    created_by, trust and matched_chunks; excluded counts what the default
    filters hid (unverified_by_others, archived, tainted).

    filters support scalar equality and {eq, gte, lte, gt, lt, exists}
    operators, e.g. {"status":"DRAFT", "due_at":{"gte":"2026-11-01"},
    "owner":{"exists":true}}. Archived documents are excluded unless
    include_archived=true. query adds hybrid ranking; returns summaries only.
    """
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            body = DocumentQueryRequest(
                query=query,
                filters=filters or {},
                namespace=namespace,
                limit=limit,
                include_unverified=include_unverified,
                include_own_unverified=include_own_unverified,
                include_tainted=include_tainted,
                include_archived=include_archived,
                mode=mode,
            )
            result = await services.query_documents(session, principal, body)
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result.model_dump(mode="json"))


@mcp.tool(name="dereference")
async def dereference(
    uri: str,
    include_unverified: bool = False,
    include_tainted: bool = False,
    expand_parts: bool = False,
    content_offset: Annotated[int, Field(ge=0)] = 0,
    content_limit: Annotated[int | None, Field(ge=1, le=262_144)] = None,
) -> dict[str, Any]:
    """Resolve an L2 artifact/doc URI (doc://<doc_key> or a doc_… id) to full content.

    Other agents' unverified or tainted documents require the explicit flags.
    expand_parts=true concatenates frontmatter.parts in index order (<= 4 MiB)
    into expanded_content; each part keeps its own id.
    """
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            result = await services.dereference(
                session,
                principal,
                uri,
                include_unverified=include_unverified,
                include_tainted=include_tainted,
                expand_parts=expand_parts,
                content_offset=content_offset,
                content_limit=content_limit,
            )
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result.model_dump(mode="json"))


@mcp.tool(name="register_namespace")
async def register_namespace(
    namespace: str,
    purpose: str | None = None,
    doc_type_default: Literal["collaborative", "log"] = "collaborative",
    write_policy: Literal["open", "proposal_required", "human_only"] = "open",
) -> dict[str, Any]:
    """Register a namespace before writing to it (register-before-use).

    Namespaces match ^[a-z][a-z0-9-]{1,31}$; system/handoff/reviews/tasks/ag and
    names starting with _ are reserved. Under API version 2026-11 writing to an
    unregistered namespace fails with namespace_not_registered; call this, then
    retry the write unchanged. Idempotent: an existing namespace is returned.
    """
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            result = await address_service.register_namespace(
                session,
                principal,
                namespace=namespace,
                purpose=purpose,
                doc_type_default=doc_type_default,
                write_policy=write_policy,
            )
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result)


@mcp.tool(name="list_namespaces")
async def list_namespaces() -> dict[str, Any]:
    """List registered namespaces with their purpose and default document type."""
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            result = await address_service.list_namespaces(session, principal)
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result)


@mcp.tool(name="rename_document")
async def rename_document(
    ref: str,
    new_alias: str,
    expected_version: int | None = None,
    session_batch_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Change a document's alias (doc_key). The canonical id never changes.

    ref accepts the doc_… id or the current alias. The old alias keeps
    resolving to the same document forever (alias_redirected warning) and is
    never assigned to another document.
    """
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            result = await services.rename_document(
                session,
                principal,
                ref,
                DocumentRenameRequest(
                    new_alias=new_alias,
                    expected_version=expected_version,
                    session_batch_id=session_batch_id,
                ),
            )
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result.model_dump(mode="json"))


@mcp.tool(name="register_parts")
async def register_parts(
    parent_ref: str,
    part_refs: list[str],
    session_batch_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Register existing documents as the ordered parts of parent_ref.

    For content that was split by hand (e.g. to fit an old size limit): writes
    parts / part_of / part_index frontmatter without moving any content, so
    dereference(parent, expand_parts=true) returns the joined body.
    """
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            result = await services.register_parts(
                session,
                principal,
                parent_ref,
                RegisterPartsRequest(part_refs=part_refs, session_batch_id=session_batch_id),
            )
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result)


@mcp.tool(name="get_usage")
async def get_usage(window: Literal["day", "week", "month"] = "day") -> dict[str, Any]:
    """Embedding tokens used vs quota, writes, would-be limits and open circuits.

    Agents see workspace totals and their own row; humans see every agent.
    mode="shadow" means limits are recorded (would_*) but not yet enforced.
    """
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            result = await budget_service.get_usage(session, principal, window)
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result)


@mcp.tool(name="get_token")
async def get_token(token_id: str = "self") -> dict[str, Any]:
    """Inspect a token without its secret: kind, depth, scopes, namespaces, expiry.

    token_id="self" (default) is the calling token. A token may inspect itself,
    its ancestors and its own subtree; humans and the workspace key see all.
    """
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            result = await lineage_service.get_token(session, principal, token_id)
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result)


@mcp.tool(name="get_lineage")
async def get_lineage(token_id: str = "self") -> dict[str, Any]:
    """Return the chain of tokens from the workspace root token down to token_id."""
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            result = await lineage_service.get_lineage(session, principal, token_id)
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result)


@mcp.tool(name="list_token_tree")
async def list_token_tree(
    token_id: str = "self",
    include_inactive: bool = False,
    cursor: str | None = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """List descendant tokens of token_id (no secrets). Only your own subtree."""
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            result = await lineage_service.list_token_tree(
                session,
                principal,
                token_id,
                include_inactive=include_inactive,
                limit=limit,
                cursor=cursor,
            )
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result)


@mcp.tool(name="explain_write")
async def explain_write(ref: str, version: int | None = None) -> dict[str, Any]:
    """Answer who wrote a document version, who issued their token, and what
    the token was allowed to do at write time (the grant snapshot)."""
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            result = await lineage_service.explain_write(session, principal, ref, version)
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result)


@mcp.tool(name="propose_document_change")
async def propose_document_change(
    doc_key: str,
    content: str,
    base_version: int,
    frontmatter: dict[str, Any] | None = None,
    title: str | None = None,
    mode: str = "single",
    note: str | None = None,
    session_batch_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Submit a reviewed proposal instead of writing a policy-gated document.

    The proposal is queued as PENDING; only a human can apply or reject it.
    Call request_human_review(kind="proposal", key=<proposal_id>) to record the
    handoff and get the exact human action endpoint.
    """
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            body = ProposalCreateRequest(
                content=content,
                title=title,
                frontmatter=frontmatter or {},
                base_version=base_version,
                mode=mode,
                note=note,
                session_batch_id=_batch(session_batch_id),
            )
            result = await services.create_proposal(
                session, principal, doc_key, body
            )
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result.model_dump(mode="json"))


@mcp.tool(name="vote_on_proposal")
async def vote_on_proposal(
    proposal_id: uuid.UUID,
    score: int,
    comment: str | None = None,
) -> dict[str, Any]:
    """Score a pending Proposal Arena entry from 0 to 5."""
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            result = await services.vote_proposal(
                session,
                principal,
                proposal_id,
                ProposalVoteRequest(score=score, comment=comment),
            )
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result.model_dump(mode="json"))


@mcp.tool(name="list_document_proposals")
async def list_document_proposals(
    doc_key: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List proposals and arena vote scores without leaking L2 document bodies.

    Proposals are human-applied. Use request_human_review for a pending item.
    """
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            result = await services.list_proposals(
                session,
                principal,
                doc_key=doc_key,
                status=status,
                limit=limit,
            )
        except ApiError as exc:
            raise _api_error(exc) from exc
    payload = result.model_dump(mode="json")
    for item in payload["items"]:
        item.pop("content", None)
        item.pop("frontmatter", None)
    return _compact(payload)


@mcp.tool(name="draft_external_action")
async def draft_external_action(
    action_type: str,
    target_url: str,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
    compensation_payload: dict[str, Any] | None = None,
    namespace: str = "default",
    session_batch_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Queue an external action for human approval; never executes directly."""
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            body = ExternalActionCreateRequest(
                action_type=action_type,
                target_url=target_url,
                method="POST",
                payload=payload or {},
                idempotency_key=idempotency_key,
                compensation_payload=compensation_payload,
                namespace=namespace,
                session_batch_id=_batch(session_batch_id),
                agent=_actor(principal, None),
            )
            result = await services.create_action(
                session,
                principal,
                body,
                actor=_actor(principal, None),
            )
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result.model_dump(mode="json"))


@mcp.tool(name="list_documents")
async def list_documents(
    namespace: str | None = None,
    cursor: str | None = None,
    limit: int = 100,
    include_archived: bool = False,
    include_unverified: bool = False,
    include_tainted: bool = False,
    filters: dict[str, Any] | None = None,
    verification_status: str | None = None,
    tainted: bool | None = None,
    updated_after: datetime | None = None,
    updated_before: datetime | None = None,
    sort: str = "updated_desc",
) -> dict[str, Any]:
    """Exhaustively enumerate documents in a namespace with an opaque cursor.

    Order is stable by (updated_at, id). Pass the returned next_cursor back as
    cursor; next_cursor=null means enumeration is complete. Archived documents
    are excluded unless include_archived=true. filters supports frontmatter
    equality and {eq, gte, lte, gt, lt, exists} operators.
    """
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            result = await services.list_documents(
                session,
                principal,
                namespace=namespace,
                filters=filters or {},
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
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result.model_dump(mode="json"))


@mcp.tool(name="upsert_many")
async def upsert_many(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Create or update up to 25 documents in one all-or-nothing transaction.

    Each item accepts doc_key, content, title, frontmatter, expected_version,
    namespace, agent and session_batch_id. If any item fails, the whole batch
    rolls back and the error details identify the failing index and doc_key.
    Up to 25 items and 5,000,000 bytes of content per batch; successful items
    carry ok=true.
    """
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            parsed = []
            for index, item in enumerate(items):
                payload = dict(item)
                if principal.is_commander:
                    payload["agent"] = _actor(principal, payload.get("agent"))
                try:
                    parsed.append(DocumentBatchUpsertItem(**payload))
                except ValidationError as exc:
                    details = [
                        {
                            "loc": [index, *error.get("loc", ())],
                            "msg": error.get("msg", "invalid value"),
                            "type": error.get("type", "value_error"),
                        }
                        for error in exc.errors()
                    ]
                    raise _tool_error(
                        "validation_error",
                        f"invalid batch item {index}",
                        422,
                        {"index": index, "doc_key": item.get("doc_key"), "errors": details},
                    ) from exc
            body = DocumentBatchUpsertRequest(items=parsed)
            result = await services.batch_upsert_documents(session, principal, body)
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        except ApiError as exc:
            raise _api_error(exc) from exc
    payload = result.model_dump(mode="json")
    for item in payload["items"]:
        document = item.get("document", {})
        item["document"] = {
            key: document.get(key)
            for key in (
                "id", "doc_key", "version", "content_hash",
                "verification_status", "trust_level", "namespace", "updated_at",
            )
        }
    return _compact(payload)


@mcp.tool(name="dereference_many")
async def dereference_many(
    uris: list[str],
    include_unverified: bool = False,
    include_tainted: bool = False,
) -> dict[str, Any]:
    """Resolve up to 25 L2 URIs; each result has ok plus document or error.

    One failed URI does not hide successful items.
    """
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            result = await services.batch_dereference(
                session,
                principal,
                uris,
                include_unverified=include_unverified,
                include_tainted=include_tainted,
            )
        except ApiError as exc:
            raise _api_error(exc) from exc
    return _compact(result.model_dump(mode="json"))


@mcp.tool(name="archive_document")
async def archive_document(
    doc_key: str,
    reason: str | None = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Soft-archive a document (creator or workspace commander only).

    Archived documents disappear from ordinary list/query/map results but can
    still be dereferenced for audit and listed with include_archived=true.
    """
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            result = await services.archive_document(
                session,
                principal,
                doc_key,
                DocumentArchiveRequest(
                    reason=reason, expected_version=expected_version
                ),
            )
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        except ApiError as exc:
            raise _api_error(exc) from exc
    payload = result.model_dump(mode="json")
    document = payload.get("document", {})
    payload["document"] = {
        key: document.get(key)
        for key in (
            "id", "doc_key", "version", "namespace", "archived_at",
            "archived_by", "archive_reason", "updated_at",
        )
    }
    return _compact(payload)


@mcp.tool(name="restore_document")
async def restore_document(
    doc_key: str,
    reason: str | None = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Restore a previously archived document (creator or commander only)."""
    async with async_session_factory() as session:
        principal = await _principal_for_call(session)
        try:
            result = await services.restore_document(
                session,
                principal,
                doc_key,
                DocumentArchiveRequest(
                    reason=reason, expected_version=expected_version
                ),
            )
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        except ApiError as exc:
            raise _api_error(exc) from exc
    payload = result.model_dump(mode="json")
    document = payload.get("document", {})
    payload["document"] = {
        key: document.get(key)
        for key in (
            "id", "doc_key", "version", "namespace", "archived_at",
            "archived_by", "archive_reason", "updated_at",
        )
    }
    return _compact(payload)


@mcp.tool(name="get_api_docs")
async def get_api_docs(
    section: Literal[
        "guide", "errors", "limits", "retry", "events", "tokens", "versions", "all", "overview"
    ] = "overview",
) -> dict[str, Any]:
    """Return the REST/MCP connection guide and core request schemas.

    Agent-readable; no admin token required. The live OpenAPI document is also
    available at GET /v1/agent-docs. section="retry" returns the canonical
    backoff algorithm, "errors" the full error-code registry (code -> class ->
    action), "versions" the supported API versions.
    """
    guide = agent_guide.sections()
    if section == "all":
        return _compact({**guide, "versions": protocol_docs("versions")["versions"]})
    if section in ("guide", "limits", "events", "tokens"):
        return _compact({section: guide[section]})
    if section != "overview":
        return _compact(protocol_docs(section))
    return _compact(
        {
            "protocol": protocol_docs("summary"),
            "name": "AgentGraph",
            "version": settings.app_version,
            "rest_base": "/v1",
            "mcp_endpoint": "/mcp/",
            "agent_docs_endpoint": "/v1/agent-docs",
            "authentication": (
                "Authorization: Bearer <workspace-or-agent-key> for both REST "
                "and MCP; admin Basic auth is not required."
            ),
            "workflow": [
                "whoami -> workspace_sync -> list_documents/query_workspace",
                "dereference or dereference_many for L2 content",
                "upsert_shared_doc / patch_doc_section / upsert_many for writes",
                "archive_document / restore_document instead of deletion",
                "workspace_sync(since=next_cursor) for incremental changes",
            ],
            "batch_limits": {
                "upsert_many": agent_guide.limits()["batch_upsert_max"],
                "dereference_many": agent_guide.limits()["batch_dereference_max"],
            },
            "limits": agent_guide.limits(),
            "schemas": {
                "DocumentUpsertRequest": DocumentBatchUpsertItem.model_json_schema(),
                "DocumentQueryRequest": DocumentQueryRequest.model_json_schema(),
                "DocumentArchiveRequest": DocumentArchiveRequest.model_json_schema(),
            },
        }
    )


def _advertise_idempotency_keys() -> None:
    for tool_name in IDEMPOTENT_TOOLS:
        tool = mcp._tool_manager.get_tool(tool_name)
        if tool is None:
            continue
        tool.parameters.setdefault("properties", {})["idempotency_key"] = {
            "type": "string",
            "maxLength": idempotency.MAX_KEY_LENGTH,
            "description": (
                "Optional. Retries with the same key and arguments within 24 h "
                "replay the first result (replayed=true) without a second write; "
                "a different body with the same key is idempotency_key_reused."
            ),
        }


_advertise_idempotency_keys()


def _describe_review_targets() -> None:
    tool = mcp._tool_manager.get_tool("review_task")
    if tool is not None:
        tool.description = (tool.description or "").rstrip() + (
            "\n\nnext_status (generated from the shared enum): approve -> "
            + "|".join(REVIEW_APPROVE_STATUSES)
            + "; reject -> "
            + "|".join(REVIEW_REJECT_STATUSES)
            + ". IN_PROGRESS returns the task to its executor while the executor's "
            "lease is still valid, otherwise to READY (effective_status). This "
            "mirrors REST POST /v1/tasks/{key}/review."
        )


_describe_review_targets()

@mcp.resource(
    "agentgraph://docs/agent-guide",
    name="agent-guide",
    description="Agent guide (same as GET /v1/agent-docs?format=markdown)",
    mime_type="text/markdown",
)
def agent_guide_resource() -> str:
    return agent_guide.render()


@mcp.resource(
    "agentgraph://docs/openapi",
    name="openapi",
    description="Read-only OpenAPI document without admin endpoints",
    mime_type="application/json",
)
def openapi_resource() -> str:
    from app.main import app as fastapi_app

    openapi = fastapi_app.openapi()
    paths = {
        path: operation
        for path, operation in openapi.get("paths", {}).items()
        if path.startswith("/v1") and not path.startswith("/v1/admin")
    }
    return json.dumps({**openapi, "paths": paths}, default=str)


@mcp.resource(
    "agentgraph://docs/errors",
    name="errors",
    description="Error-code registry: code -> class -> action",
    mime_type="application/json",
)
def errors_resource() -> str:
    return json.dumps(protocol_docs("errors"), default=str)


_http_app: ASGIApp | None = None


def create_http_app() -> ASGIApp:
    global _http_app
    if _http_app is None:
        _http_app = MCPAuthMiddleware(mcp.streamable_http_app())
    return _http_app


def run_stdio() -> None:
    mcp.run(transport="stdio")
