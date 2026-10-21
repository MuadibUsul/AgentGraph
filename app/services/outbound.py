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
from urllib.parse import urlsplit, urlunsplit

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


_BLOCKED_V6 = (ipaddress.ip_network("2002::/16"), ipaddress.ip_network("64:ff9b::/96"))


def _is_private(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    if any(ip in network for network in _BLOCKED_V6):
        return True
    return not ip.is_global


async def _resolve_pinned(
    url: str,
) -> tuple[str, str, str]:
    """Return (pinned_url, Host header, SNI hostname).

    Resolution happens once and the connection is made to that exact address,
    so DNS rebinding between validation and connect cannot reach a new target.
    """
    parsed = urlsplit(url)
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
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            parsed.hostname,
            port,
            0,
            socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise ApiError(
            422, "invalid_target_url", f"cannot resolve host: {exc}"
        ) from exc
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in addresses:
            addresses.append(address)
    if not settings.outbound_allow_private_networks:
        addresses = [address for address in addresses if not _is_private(address)]
    if not addresses:
        raise ApiError(
            403,
            "outbound_private_address",
            "outbound target resolves only to private or reserved addresses",
        )
    address = addresses[0]
    host_literal = f"[{address}]" if ":" in address else address
    default_port = 443 if parsed.scheme == "https" else 80
    netloc = host_literal if port == default_port else f"{host_literal}:{port}"
    pinned = urlunsplit(
        (parsed.scheme, netloc, parsed.path or "/", parsed.query, "")
    )
    host_header = parsed.hostname
    if parsed.port and parsed.port != default_port:
        host_header = f"{host_header}:{parsed.port}"
    return pinned, host_header, parsed.hostname


async def validate_target(url: str) -> str:
    await _resolve_pinned(url)
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
        pinned_url, host_header, sni_hostname = await _resolve_pinned(url)
        request_headers["Host"] = host_header
        for key in list(request_headers):
            if key.lower() in {
                "connection",
                "content-length",
                "transfer-encoding",
                "upgrade",
                "proxy-authorization",
            }:
                request_headers.pop(key)
        if len(request_headers) > 64:
            raise ApiError(422, "too_many_headers", "outbound headers are limited to 64")
        total = timeout or settings.outbound_timeout_seconds

        async def _request() -> tuple[int, str]:
            async with httpx.AsyncClient(
                timeout=total,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    method,
                    pinned_url,
                    content=body,
                    headers=request_headers,
                    extensions={"sni_hostname": sni_hostname},
                ) as response:
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        remaining = settings.outbound_max_response_bytes - size
                        if remaining <= 0:
                            break
                        chunks.append(chunk[:remaining])
                        size += min(len(chunk), remaining)
                    return response.status_code, b"".join(chunks).decode(
                        "utf-8", errors="replace"
                    )

        status_code, response_body = await asyncio.wait_for(_request(), total)
        duration_ms = int((time.perf_counter() - started) * 1000)
        return OutboundResult(
            status_code=status_code,
            ok=200 <= status_code < 300,
            error=None if status_code < 300 else f"HTTP {status_code}",
            body=response_body,
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
