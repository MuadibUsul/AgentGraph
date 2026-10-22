"""Getting-started checklist for people in the console.

Six steps take a new owner from sign-up to a reviewed piece of agent work. The
console observes most of them in workspace data (a minted token, an agent's
get_orientation call, first task or document, a human review); the rest are
ticked by the person. Manual ticks, skips and "closed" are stored per user
and workspace, so a finished or closed checklist never reappears on its own.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.errors import ApiError

# (step id, docs page, how it completes)
STEPS: list[tuple[str, str, str]] = [
    ("learn_workspace", "/docs/concepts/", "manual"),
    ("create_token", "/docs/quickstart/", "auto"),
    ("connect_mcp", "/docs/mcp/", "auto"),
    ("first_content", "/docs/workflows/", "auto"),
    ("human_review", "/docs/console/", "auto"),
    ("view_audit", "/docs/console/", "manual"),
]
STEP_IDS = [step for step, _, _ in STEPS]

_DETECT = {
    "create_token": "SELECT EXISTS (SELECT 1 FROM agent_tokens WHERE workspace_id = :ws)",
    # An agent token (not a person) ran get_orientation, over MCP or REST.
    "connect_mcp": (
        "SELECT EXISTS (SELECT 1 FROM access_audit a JOIN agent_tokens t "
        "ON t.workspace_id = a.workspace_id AND t.agent_signature = a.details->>'principal' "
        "WHERE a.workspace_id = :ws AND a.action = 'agent.oriented')"
    ),
    "first_content": (
        "SELECT EXISTS (SELECT 1 FROM collab_tasks WHERE workspace_id = :ws) OR EXISTS ("
        "SELECT 1 FROM shared_documents WHERE workspace_id = :ws AND namespace NOT IN "
        "('system', 'handoff') AND created_by NOT IN ('system', 'platform'))"
    ),
    "human_review": (
        "SELECT EXISTS (SELECT 1 FROM collab_tasks WHERE workspace_id = :ws "
        "AND reviewed_by IS NOT NULL) OR EXISTS (SELECT 1 FROM shared_documents "
        "WHERE workspace_id = :ws AND namespace <> 'system' "
        "AND verification_status IN ('VERIFIED', 'DEPRECATED')) OR EXISTS ("
        "SELECT 1 FROM document_proposals WHERE workspace_id = :ws AND reviewed_by IS NOT NULL)"
    ),
}


def _require_user(principal: Principal) -> None:
    if principal.user_id is None:
        raise ApiError(401, "human_session_required",
                       "the getting-started checklist belongs to a signed-in person",
                       headers={"WWW-Authenticate": "Bearer"})


async def _row(session: AsyncSession, principal: Principal) -> dict[str, Any]:
    row = (
        await session.execute(
            text("SELECT completed, skipped, dismissed_at FROM onboarding_progress "
                 "WHERE user_id = :user AND workspace_id = :ws"),
            {"user": principal.user_id, "ws": principal.workspace.id},
        )
    ).mappings().one_or_none()
    return dict(row) if row else {"completed": [], "skipped": [], "dismissed_at": None}


async def status(session: AsyncSession, principal: Principal) -> dict[str, Any]:
    _require_user(principal)
    stored = await _row(session, principal)
    steps = []
    for step, doc, mode in STEPS:
        observed = False
        if mode == "auto":
            observed = bool(
                await session.scalar(text(_DETECT[step]), {"ws": principal.workspace.id})
            )
        done = observed or step in stored["completed"]
        steps.append({
            "id": step,
            "done": done,
            "skipped": (not done) and step in stored["skipped"],
            "detected": observed,
            "mode": mode,
            "doc": doc,
        })
    finished = sum(1 for step in steps if step["done"] or step["skipped"])
    return {
        "steps": steps,
        "completed": sum(1 for step in steps if step["done"]),
        "total": len(steps),
        "dismissed": stored["dismissed_at"] is not None,
        "all_done": finished == len(steps),
        # Show the checklist until it is closed or every step is done or skipped.
        "visible": stored["dismissed_at"] is None and finished < len(steps),
    }


async def update(
    session: AsyncSession, principal: Principal, *, action: str, step: str | None
) -> dict[str, Any]:
    _require_user(principal)
    if action in ("complete", "skip", "unskip") and step not in STEP_IDS:
        raise ApiError(422, "validation_error", "unknown onboarding step",
                       details={"field": "step", "allowed": STEP_IDS})
    stored = await _row(session, principal)
    completed = list(stored["completed"])
    skipped = list(stored["skipped"])
    dismissed_at = stored["dismissed_at"]
    if action == "complete" and step not in completed:
        completed.append(step)
        skipped = [item for item in skipped if item != step]
    elif action == "skip" and step not in skipped:
        skipped.append(step)
    elif action == "unskip":
        skipped = [item for item in skipped if item != step]
    elif action == "dismiss":
        dismissed_at = datetime.now(timezone.utc)
    elif action == "reopen":
        dismissed_at = None
    elif action not in ("complete", "skip"):
        raise ApiError(422, "validation_error", "unknown onboarding action",
                       details={"field": "action",
                                "allowed": ["complete", "skip", "unskip", "dismiss", "reopen"]})
    await session.execute(
        text(
            "INSERT INTO onboarding_progress (user_id, workspace_id, completed, skipped, "
            "dismissed_at, updated_at) VALUES (:user, :ws, CAST(:completed AS jsonb), "
            "CAST(:skipped AS jsonb), :dismissed, NOW()) ON CONFLICT (user_id, workspace_id) "
            "DO UPDATE SET completed = EXCLUDED.completed, skipped = EXCLUDED.skipped, "
            "dismissed_at = EXCLUDED.dismissed_at, updated_at = NOW()"
        ),
        {"user": principal.user_id, "ws": principal.workspace.id,
         "completed": json.dumps(completed), "skipped": json.dumps(skipped),
         "dismissed": dismissed_at},
    )
    await session.commit()
    return await status(session, principal)
