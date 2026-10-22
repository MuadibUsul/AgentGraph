"""Soft-constraint warnings collected during a request (PRD §6 ``warnings[]``).

Services call ``add_warning``; the protocol middleware merges the collected
warnings into JSON object responses and the MCP layer into tool results. Soft
constraints never reject a write.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any


class Collected(list):
    """Warnings of one request, plus an optional agent onboarding hint.

    A mutable container set at the edge of the request, so values added in a
    nested task (an endpoint behind middleware) are still seen when merging.
    """

    def __init__(self) -> None:
        super().__init__()
        self.onboarding: dict[str, Any] = {}

    def __bool__(self) -> bool:
        return bool(len(self)) or bool(self.onboarding)


_warnings: ContextVar[Collected | None] = ContextVar("agentgraph_warnings", default=None)


def begin_collection() -> Collected:
    collected = Collected()
    _warnings.set(collected)
    return collected


def set_onboarding(hint: dict[str, Any]) -> None:
    """Attach the agent's next onboarding step to this response (AG onboarding)."""
    collected = _warnings.get()
    if collected is None:
        return
    collected.onboarding.clear()
    collected.onboarding.update(hint)


def current() -> list[dict[str, Any]]:
    return _warnings.get() or []


def add_warning(code: str, message: str, **details: Any) -> None:
    collected = _warnings.get()
    if collected is None:
        return
    entry: dict[str, Any] = {"code": code, "message": message}
    entry.update({key: value for key, value in details.items() if value is not None})
    if entry not in collected:
        collected.append(entry)


def merge_warnings(payload: Any, collected: list[dict[str, Any]]) -> Any:
    if not collected or not isinstance(payload, dict):
        return payload
    result = dict(payload)
    if len(collected):
        existing = payload.get("warnings")
        merged = list(existing) if isinstance(existing, list) else []
        merged.extend(item for item in collected if item not in merged)
        result["warnings"] = merged
    hint = getattr(collected, "onboarding", None)
    if hint and "onboarding" not in payload:
        result["onboarding"] = dict(hint)
    return result
