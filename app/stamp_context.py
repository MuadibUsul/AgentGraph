"""The current request's lineage stamp (PRD AG-ID-03).

Kept separate from the lineage service so the tenant session hooks can apply
it without import cycles. ``app.tenant`` publishes it to every transaction as
the setting ``app.lineage_stamp``; database triggers copy it onto writes.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any

_current_stamp: ContextVar[dict[str, Any] | None] = ContextVar(
    "agentgraph_lineage_stamp", default=None
)


def current_stamp() -> dict[str, Any] | None:
    return _current_stamp.get()


def set_current_stamp(stamp: dict[str, Any] | None) -> None:
    _current_stamp.set(stamp)


def stamp_json() -> str:
    stamp = _current_stamp.get()
    return json.dumps(stamp, sort_keys=True, default=str) if stamp else ""
