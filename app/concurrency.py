"""In-flight request limit per credential (PRD AG-ST-15 point 2b).

At most ``max_in_flight_per_token`` requests per token run at once; long-lived
streams (SSE) have their own pool of ``max_streams_per_token``. Counting is per
process: behind several replicas the effective limit is the per-replica limit
times the replica count. In shadow mode overflows are only logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections import Counter

from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings
from app.errors import error_body

logger = logging.getLogger("agentgraph.concurrency")
settings = get_settings()

STREAM_PATHS = ("/v1/stream",)


def _credential(scope: Scope) -> str | None:
    headers = dict(scope.get("headers") or [])
    raw = headers.get(b"authorization", b"").decode("latin-1")
    scheme, _, token = raw.partition(" ")
    token = token.strip() if scheme.lower() == "bearer" else ""
    if not token:
        token = headers.get(b"x-api-key", b"").decode("latin-1").strip()
    if not token:
        return None
    if settings.admin_token and hmac.compare_digest(token, settings.admin_token):
        return None  # operators are not metered
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ConcurrencyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.in_flight: Counter[tuple[str, str]] = Counter()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not settings.rate_limit_enabled:
            await self.app(scope, receive, send)
            return
        credential = _credential(scope)
        if credential is None:
            await self.app(scope, receive, send)
            return
        stream = scope.get("path", "").startswith(STREAM_PATHS)
        pool = "streams" if stream else "requests"
        limit = settings.max_streams_per_token if stream else settings.max_in_flight_per_token
        key = (credential, pool)
        if self.in_flight[key] >= limit:
            if settings.budget_mode == "enforce":
                await self._refuse(send, pool, limit)
                return
            logger.info("would_rate_limit concurrency pool=%s limit=%s", pool, limit)
        self.in_flight[key] += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.in_flight[key] -= 1
            if self.in_flight[key] <= 0:
                del self.in_flight[key]

    @staticmethod
    async def _refuse(send: Send, pool: str, limit: int) -> None:
        headers = {"Retry-After": "1"}
        body = error_body(
            "rate_limited",
            f"at most {limit} concurrent {pool} per token",
            429,
            details={"dimension": "concurrency", "pool": pool, "limit": limit,
                     "retry_after_ms": 1000},
            headers=headers,
        )
        payload = json.dumps(body).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                    (b"retry-after", b"1"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
