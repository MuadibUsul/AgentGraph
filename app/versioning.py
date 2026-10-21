"""API version negotiation (PRD AG-ST-18 point 5, §6.1 point 5).

Versions are dates. A request that declares nothing runs on the baseline
``2026-10-21`` (the 3.2.1 semantics). REST declares with the
``AgentGraph-Version`` header; MCP over HTTP uses the same header on each
request because the server is stateless. Inside one version only additive
changes ship; behaviour changes are gated on ``at_least(<version>)``.
"""

from __future__ import annotations

from contextvars import ContextVar

BASELINE_VERSION = "2026-10-21"
LATEST_VERSION = "2026-11"
SUPPORTED_VERSIONS: tuple[str, ...] = (BASELINE_VERSION, LATEST_VERSION)
VERSION_HEADER = "AgentGraph-Version"

_current_version: ContextVar[str] = ContextVar(
    "agentgraph_api_version", default=BASELINE_VERSION
)


def normalize(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def is_supported(version: str) -> bool:
    return version in SUPPORTED_VERSIONS


def set_version(version: str):
    return _current_version.set(version)


def reset_version(token) -> None:
    _current_version.reset(token)


def current_version() -> str:
    return _current_version.get()


def at_least(version: str) -> bool:
    """True when the request's negotiated version includes ``version``."""
    return current_version() >= version


__all__ = [
    "BASELINE_VERSION",
    "LATEST_VERSION",
    "SUPPORTED_VERSIONS",
    "VERSION_HEADER",
    "at_least",
    "current_version",
    "is_supported",
    "normalize",
]
