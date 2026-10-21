"""Safe outbound HTTP helpers for webhooks and approved external actions."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.config import get_settings
from app.errors import ApiError
from app.services.common import utcnow

settings = get_settings()


def _host_allowed(host: str) -> bool:
    allowed = settings.outbound_allowed_hosts
    if not allowed:
        return True
    host = host.lower()
    for pattern in allowed:
        pattern = pattern.lower().strip()
        if not pattern:
            continue
        if pattern.startswith("*."):
            if host == pattern[2:] or host.endswith(pattern[1:]):
                return True
        elif host == pattern:
            return True
    return False


def _is_private(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def validate_target(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ApiError(
            422, "invalid_target_url", "outbound URLs must use http or https"
        )
    if not parsed.hostname:
        raise ApiError(422, "invalid_target_url", "outbound URL needs a hostname")
    if settings.is_production and parsed.scheme != "https":
        raise ApiError(
            422, "insecure_target_url", "production outbound URLs must use https"
        )
    if not _host_allowed(parsed.hostname):
        raise ApiError(
            403,
            "outbound_host_forbidden",
            "this host is not in OUTBOUND_ALLOWED_HOSTS",
        )
    if not settings.outbound_allow_private_networks:
        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo, parsed.hostname, parsed.port or 443, 0, socket.SOCK_STREAM
            )
        except OSError as exc:
            raise ApiError(422, "invalid_target_url", f"cannot resolve host: {exc}") from exc
        for info in infos:
            address = info[4][0]
            if _is_private(address):
                raise ApiError(
                    403,
                    "outbound_private_address",
                    "outbound target resolves to a private or reserved address",
                )
    return url


def signature(secret: str, timestamp: int, body: bytes) -> str:
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


@dataclass
class OutboundResult:
    status_code: int | None
    ok: bool
    error: str | None = None
    body: str = ""
    duration_ms: int = 0


async def send_json(
    url: str,
    payload: dict,
    *,
    headers: dict[str, str] | None = None,
    method: str = "POST",
    secret: str | None = None,
    event_name: str | None = None,
    idempotency_key: str | None = None,
    timeout: float | None = None,
) -> OutboundResult:
    started = time.perf_counter()
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    request_headers = {
        "Content-Type": "application/json",
        "User-Agent": "AgentGraph-Outbound/3.2",
        **(headers or {}),
    }
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    if secret:
        timestamp = int(time.time())
        request_headers["X-AgentGraph-Timestamp"] = str(timestamp)
        request_headers["X-AgentGraph-Signature"] = signature(secret, timestamp, body)
    if event_name:
        request_headers["X-AgentGraph-Event"] = event_name
    duration_ms = 0
    try:
        async with httpx.AsyncClient(
            timeout=timeout or settings.outbound_timeout_seconds,
            follow_redirects=False,
        ) as client:
            response = await client.request(
                method, url, content=body, headers=request_headers
            )
        duration_ms = int((time.perf_counter() - started) * 1000)
        return OutboundResult(
            status_code=response.status_code,
            ok=200 <= response.status_code < 300,
            error=None if response.status_code < 300 else f"HTTP {response.status_code}",
            body=response.text[:4_000],
            duration_ms=duration_ms,
        )
    except Exception as exc:  # noqa: BLE001 - outbound boundary
        duration_ms = int((time.perf_counter() - started) * 1000)
        return OutboundResult(
            status_code=None, ok=False, error=str(exc)[:2_000], duration_ms=duration_ms
        )


def new_secret() -> str:
    import secrets

    return f"whsec_{secrets.token_urlsafe(32)}"


def now_iso() -> str:
    return utcnow().isoformat()
