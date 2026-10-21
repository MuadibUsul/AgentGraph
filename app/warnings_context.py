"""Soft-constraint warnings collected during a request (PRD §6 ``warnings[]``).

Services call ``add_warning``; the protocol middleware merges the collected
warnings into JSON object responses and the MCP layer into tool results. Soft
constraints never reject a write.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

_warnings: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "agentgraph_warnings", default=None
)


def begin_collection() -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    _warnings.set(collected)
    return collected


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
    existing = payload.get("warnings")
    merged = list(existing) if isinstance(existing, list) else []
    merged.extend(item for item in collected if item not in merged)
    return {**payload, "warnings": merged}
