"""Typed actor refs for responses (PRD AG-ID-06 point 3, API version 2026-11).

Stored ``updated_by`` / ``created_by`` labels never change. When a request
negotiated ``2026-11``, the directory loads a label -> ref map for the
workspace and responses show ``agent:<agent_id>``, ``human:<user_id>``,
``token:<token_id>`` or ``legacy:<label>``, keeping the stored label in a
``*_label`` field. Older versions see the labels unchanged.
"""

from __future__ import annotations

from contextvars import ContextVar

_actor_map: ContextVar[dict[str, str] | None] = ContextVar("agentgraph_actor_map", default=None)


def set_map(mapping: dict[str, str] | None) -> None:
    _actor_map.set(mapping)


def actor_ref(label: str | None) -> str | None:
    mapping = _actor_map.get()
    if mapping is None or label is None:
        return label
    if label.startswith(("agent:", "human:", "token:", "legacy:")) or label == "system":
        return label
    return mapping.get(label) or f"legacy:{label}"


def actor_label(label: str | None) -> str | None:
    return label if _actor_map.get() is not None else None
