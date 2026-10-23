"""Operator product events (v3.34): moments the workspace data cannot show.

Activation, retention and signups are derived from existing tables (see
``growth_metrics``). This log only holds what leaves no other trace: a CLI
setup started or finished, a human decision in the console, a click on
"Upgrade". Rows carry ids and short labels, never content, keys or e-mails.

Recording never breaks the action it describes: it runs in its own unscoped
session (the table is operator-only under RLS) and swallows its own errors.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import text

logger = logging.getLogger("agentgraph.product_events")

EVENT_NAMES = frozenset(
    {
        "cli_init_started",    # a CLI asked for a user code
        "cli_init_approved",   # an owner approved it and a key was minted
        "cli_init_completed",  # the CLI collected the sealed key
        "human_decision",      # approve / reject / verify / undo / apply
        "upgrade_intent",      # a click on Upgrade (props.source, props.plan)
        "upgrade_viewed",      # the upgrade dialog was opened
    }
)
_MAX_PROP_LEN = 64


def _clean_props(props: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in list(props.items())[:8]:
        if value is None:
            continue
        if isinstance(value, (bool, int, float)):
            clean[str(key)[:32]] = value
        else:
            clean[str(key)[:32]] = str(value)[:_MAX_PROP_LEN]
    return clean


async def record(
    name: str,
    *,
    workspace_id: uuid.UUID | str | None = None,
    user_id: uuid.UUID | str | None = None,
    **props: Any,
) -> None:
    if name not in EVENT_NAMES:
        raise ValueError(f"unknown product event: {name}")
    from app.database import async_session_factory

    try:
        async with async_session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO product_events (name, workspace_id, user_id, props) "
                    "VALUES (:n, :w, :u, CAST(:p AS jsonb))"
                ),
                {
                    "n": name,
                    "w": str(workspace_id) if workspace_id else None,
                    "u": str(user_id) if user_id else None,
                    "p": json.dumps(_clean_props(props)),
                },
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - analytics never fails the caller
        logger.warning("product event %s not recorded: %s", name, type(exc).__name__)


async def human_decision(principal: Any, kind: str, outcome: str | None = None) -> None:
    """One human decision in the console (the "humans hold the line" metric)."""
    await record(
        "human_decision",
        workspace_id=principal.workspace.id,
        user_id=getattr(principal, "user_id", None),
        kind=kind,
        outcome=outcome,
    )
