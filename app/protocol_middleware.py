"""Pure-ASGI failure-semantics protocol layer (PRD AG-ST-18).

* Negotiates ``AgentGraph-Version`` for REST and MCP-over-HTTP requests and
  echoes the effective version on every response.
* Applies idempotency to REST writes that send an ``Idempotency-Key`` header:
  the first response is stored and replayed (``replayed: true``) for the same
  key and body; MCP tools take ``idempotency_key`` as an argument instead.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import versioning, warnings_context
from app.config import get_settings
from app.errors import ApiError, error_body
from app.services import idempotency

logger = logging.getLogger("agentgraph.protocol")

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Auth endpoints mint credentials; never persist their responses. Admin calls
# act across tenants and are not scoped to one tenant's idempotency records.
_IDEMPOTENCY_EXCLUDED_PREFIXES = ("/v1/auth/", "/v1/admin/")
# Admin routes name tenants explicitly in the path (PRD AG-ID-07 point 3).
_TENANT_CHECK_EXCLUDED_PREFIXES = ("/v1/admin/",)
_TENANT_HEADERS = ("tenant-id", "x-tenant-id", "agentgraph-tenant")
_MAX_INSPECTED_BODY = 8 * 1024 * 1024


def _tenant_checked(path: str) -> bool:
    return (path.startswith("/v1/") or path.startswith("/mcp")) and not path.startswith(
        _TENANT_CHECK_EXCLUDED_PREFIXES
    )


def _checks_body(path: str) -> bool:
    return _tenant_checked(path)


def _tenant_id_in_request(scope: Scope, headers: Headers) -> str | None:
    if not _tenant_checked(scope.get("path", "")):
        return None
    for name in _TENANT_HEADERS:
        if name in headers:
            return f"header:{name}"
    query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    if "tenant_id" in query:
        return "query:tenant_id"
    return None


def _tenant_id_in_body(path: str, headers: Headers, body: bytes) -> str | None:
    """Reject a top-level tenant_id; nested user data (frontmatter) may use the name."""
    if not body or len(body) > _MAX_INSPECTED_BODY:
        return None
    if "json" not in (headers.get("content-type") or "json"):
        return None
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if path.startswith("/mcp"):
        messages = payload if isinstance(payload, list) else [payload]
        for message in messages:
            params = message.get("params") if isinstance(message, dict) else None
            arguments = params.get("arguments") if isinstance(params, dict) else None
            if isinstance(arguments, dict) and "tenant_id" in arguments:
                return "arguments:tenant_id"
        return None
    if isinstance(payload, dict) and "tenant_id" in payload:
        return "body:tenant_id"
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        for item in payload["items"]:
            if isinstance(item, dict) and "tenant_id" in item:
                return "body:items[].tenant_id"
    return None


async def _read_body(receive: Receive) -> bytes | None:
    chunks: list[bytes] = []
    more = True
    while more:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        chunks.append(message.get("body", b""))
        more = bool(message.get("more_body"))
    return b"".join(chunks)


def _replaying(body: bytes, receive: Receive) -> Receive:
    delivered = False

    async def replay() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay
_REPLAY_HEADER = "Idempotent-Replayed"


def _credential(headers: Headers) -> str | None:
    authorization = headers.get("authorization")
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() in ("bearer", "basic") and token.strip():
            return token.strip()
    for name in ("x-api-key", "x-admin-token"):
        value = headers.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _error_response(exc: ApiError, version: str) -> JSONResponse:
    response = JSONResponse(
        status_code=exc.status_code,
        content=error_body(
            exc.code, exc.message, exc.status_code, details=exc.details, headers=exc.headers
        ),
        headers=exc.headers,
    )
    response.headers[versioning.VERSION_HEADER] = version
    return response


BUILD_HEADER = "AgentGraph-Build"
_BUILD = get_settings().app_version


class ProtocolMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        declared = versioning.normalize(headers.get(versioning.VERSION_HEADER))
        if declared is not None and not versioning.is_supported(declared):
            response = _error_response(
                ApiError(
                    400,
                    "unsupported_api_version",
                    f"API version '{declared}' is not supported",
                    details={
                        "requested": declared,
                        "supported": list(versioning.SUPPORTED_VERSIONS),
                        "default": versioning.BASELINE_VERSION,
                    },
                ),
                versioning.BASELINE_VERSION,
            )
            await response(scope, receive, send)
            return
        version = declared or versioning.BASELINE_VERSION
        token = versioning.set_version(version)

        collected = warnings_context.begin_collection()
        merge = not scope.get("path", "").startswith("/mcp")
        held_start: Message | None = None
        held_body: list[bytes] = []

        async def send_with_version(message: Message) -> None:
            nonlocal held_start
            if message["type"] == "http.response.start":
                outgoing = MutableHeaders(scope=message)
                outgoing[versioning.VERSION_HEADER] = version
                # The build, so a client sees a deploy even when the API version holds.
                outgoing[BUILD_HEADER] = _BUILD
                content_type = Headers(raw=message.get("headers", [])).get("content-type", "")
                if merge and collected and content_type.startswith("application/json"):
                    held_start = message
                    return
                await send(message)
                return
            if held_start is not None and message["type"] == "http.response.body":
                held_body.append(message.get("body", b""))
                if message.get("more_body"):
                    return
                body = b"".join(held_body)
                try:
                    body = json.dumps(
                        warnings_context.merge_warnings(json.loads(body or b"null"), collected),
                        ensure_ascii=False,
                        default=str,
                    ).encode("utf-8")
                except ValueError:
                    pass
                start = held_start
                held_start = None
                headers = MutableHeaders(scope=start)
                headers["content-length"] = str(len(body))
                await send(start)
                await send({"type": "http.response.body", "body": body, "more_body": False})
                return
            await send(message)

        try:
            path = scope.get("path", "")
            method = scope.get("method", "GET")
            violation = _tenant_id_in_request(scope, headers)
            body: bytes | None = None
            if violation is None and method in _WRITE_METHODS and _checks_body(path):
                body = await _read_body(receive)
                if body is None:
                    return
                violation = _tenant_id_in_body(path, headers, body)
            if violation is not None:
                await _error_response(
                    ApiError(
                        400,
                        "tenant_id_not_allowed",
                        "tenant_id is derived from your credential and must not be sent",
                        details={"location": violation},
                    ),
                    version,
                )(scope, receive, send_with_version)
                return
            downstream = receive if body is None else _replaying(body, receive)
            key = headers.get("idempotency-key")
            if (
                key is not None
                and method in _WRITE_METHODS
                and path.startswith("/v1/")
                and not path.startswith(_IDEMPOTENCY_EXCLUDED_PREFIXES)
            ):
                if body is None:
                    body = await _read_body(receive)
                    if body is None:
                        return
                await self._idempotent(
                    scope, downstream, send_with_version, headers, key, version, body
                )
            else:
                await self.app(scope, downstream, send_with_version)
        finally:
            versioning.reset_version(token)

    async def _idempotent(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        headers: Headers,
        raw_key: str,
        version: str,
        body: bytes,
    ) -> None:
        credential = _credential(headers)
        if credential is None:
            # Unauthenticated: let the route reject it; nothing to scope by.
            await self.app(scope, receive, send)
            return
        try:
            key = idempotency.validate_key(raw_key)
        except ApiError as exc:
            await _error_response(exc, version)(scope, receive, send)
            return

        principal = idempotency.credential_hash(credential)
        tool = f"{scope['method']} {scope.get('path', '')}"[:512]
        body_hash = idempotency.request_hash(
            tool, scope.get("query_string", b""), version, body
        )
        try:
            replay = await idempotency.begin(principal, tool, key, body_hash)
        except idempotency.UnknownCredential:
            await self.app(scope, receive, send)
            return
        except ApiError as exc:
            await _error_response(exc, version)(scope, receive, send)
            return
        if replay is not None:
            content = replay.response
            if isinstance(content, dict):
                content = {**content, "replayed": True}
            response = JSONResponse(status_code=replay.http_status, content=content)
            response.headers[_REPLAY_HEADER] = "true"
            await response(scope, receive, send)
            return

        status_code = 500
        start_message: Message | None = None
        response_chunks: list[bytes] = []

        async def capture(message: Message) -> None:
            # Buffer the whole response: the record must be stored before the
            # client sees it, or an immediate retry races into in_progress.
            nonlocal status_code, start_message
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                start_message = message
            elif message["type"] == "http.response.body":
                response_chunks.append(message.get("body", b""))

        try:
            await self.app(scope, receive, capture)
        except BaseException:
            await idempotency.abort(principal, tool, key)
            raise
        body_bytes = b"".join(response_chunks)
        try:
            stored = json.loads(body_bytes or b"null")
        except ValueError:
            await idempotency.abort(principal, tool, key)
        else:
            # Store what the client sees, warnings included, so replays match.
            stored = warnings_context.merge_warnings(stored, warnings_context.current())
            body_bytes = json.dumps(stored, ensure_ascii=False, default=str).encode("utf-8")
            if start_message is not None:
                MutableHeaders(scope=start_message)["content-length"] = str(len(body_bytes))
            try:
                await idempotency.complete(principal, tool, key, status_code, stored)
            except Exception:  # noqa: BLE001 - never fail a finished write on bookkeeping
                logger.exception("could not store idempotency record")
                await idempotency.abort(principal, tool, key)
        if start_message is not None:
            await send(start_message)
        await send({"type": "http.response.body", "body": body_bytes, "more_body": False})
