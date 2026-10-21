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

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import versioning
from app.errors import ApiError, error_body
from app.services import idempotency

logger = logging.getLogger("agentgraph.protocol")

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Auth endpoints mint credentials; never persist their responses.
_IDEMPOTENCY_EXCLUDED_PREFIXES = ("/v1/auth/",)
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

        async def send_with_version(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[versioning.VERSION_HEADER] = version
            await send(message)

        try:
            path = scope.get("path", "")
            key = headers.get("idempotency-key")
            if (
                key is not None
                and scope.get("method") in _WRITE_METHODS
                and path.startswith("/v1/")
                and not path.startswith(_IDEMPOTENCY_EXCLUDED_PREFIXES)
            ):
                await self._idempotent(scope, receive, send_with_version, headers, key, version)
            else:
                await self.app(scope, receive, send_with_version)
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

        chunks: list[bytes] = []
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunks.append(message.get("body", b""))
            more = bool(message.get("more_body"))
        body = b"".join(chunks)
        principal = idempotency.credential_hash(credential)
        tool = f"{scope['method']} {scope.get('path', '')}"[:512]
        body_hash = idempotency.request_hash(
            tool, scope.get("query_string", b""), version, body
        )
        try:
            replay = await idempotency.begin(principal, tool, key, body_hash)
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

        delivered = False

        async def replay_receive() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

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
            await self.app(scope, replay_receive, capture)
        except BaseException:
            await idempotency.abort(principal, tool, key)
            raise
        body_bytes = b"".join(response_chunks)
        try:
            stored = json.loads(body_bytes or b"null")
        except ValueError:
            await idempotency.abort(principal, tool, key)
        else:
            try:
                await idempotency.complete(principal, tool, key, status_code, stored)
            except Exception:  # noqa: BLE001 - never fail a finished write on bookkeeping
                logger.exception("could not store idempotency record")
                await idempotency.abort(principal, tool, key)
        if start_message is not None:
            await send(start_message)
        await send({"type": "http.response.body", "body": body_bytes, "more_body": False})
