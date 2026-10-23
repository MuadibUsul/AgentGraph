"""Operator growth KPIs (v3.34): is anyone getting value, and coming back?

Four numbers, each with its target from the six-week plan:

* activation: share of self-serve signups whose workspace saw an agent call
  ``get_orientation`` within 24 hours (target 40%);
* week-2 retention: share of activated workspaces whose agents were still
  calling the board 7-14 days after their first orientation (target 25%);
* human decisions: approvals, rejections, verifications and undos per active
  workspace in the last 7 days (target 3);
* upgrade intent: workspaces that clicked Upgrade in the window (target 5).

Signups are derived from ``human_users`` (an owner created with its
workspace), activation from ``agent_onboarding``, agent activity from
``token_access_log`` (agent principals only) and the rest from
``product_events``. Accounts listed in METRICS_INTERNAL_EMAILS are left out.
These queries run unscoped, so only the admin routes call them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

settings = get_settings()

TARGETS = {
    "activation_rate": 0.40,
    "week2_retention": 0.25,
    "decisions_per_active_workspace": 3,
    "upgrade_intent_workspaces": 5,
}


def _internal_patterns() -> tuple[list[str], list[str]]:
    emails: list[str] = []
    domains: list[str] = []
    for item in settings.metrics_internal_emails.split(","):
        item = item.strip().lower()
        if not item:
            continue
        if item.startswith("@"):
            domains.append(item)
        else:
            emails.append(item)
    return emails, domains


# One row per self-serve signup: the owner created together with its
# workspace, minus internal accounts. Shared by every query below.
_SIGNUPS_CTE = """
signups AS (
    SELECT w.id AS workspace_id, w.name AS workspace_name, w.tier,
           w.created_at AS signup_at, u.id AS user_id, u.email
    FROM workspaces w
    JOIN human_users u
      ON u.workspace_id = w.id AND u.role = 'OWNER'
     AND u.created_at BETWEEN w.created_at - INTERVAL '5 minutes'
                          AND w.created_at + INTERVAL '5 minutes'
    WHERE NOT (lower(u.email) = ANY(CAST(:internal_emails AS text[])))
      AND NOT EXISTS (
          SELECT 1 FROM unnest(CAST(:internal_domains AS text[])) d
          WHERE lower(u.email) LIKE '%' || d)
),
oriented AS (
    SELECT workspace_id, MIN(oriented_at) AS first_oriented_at
    FROM agent_onboarding
    WHERE oriented_at IS NOT NULL
    GROUP BY workspace_id
),
agent_activity AS (
    SELECT l.workspace_id, l.bucket, l.requests
    FROM token_access_log l
    WHERE l.requests > 0
      AND EXISTS (SELECT 1 FROM agent_tokens t
                  WHERE t.workspace_id = l.workspace_id
                    AND t.agent_signature = l.principal_id)
),
cohort AS (
    SELECT s.*, o.first_oriented_at,
           (o.first_oriented_at IS NOT NULL
            AND o.first_oriented_at <= s.signup_at + INTERVAL '24 hours') AS activated,
           EXISTS (
               SELECT 1 FROM agent_activity a
               WHERE a.workspace_id = s.workspace_id
                 AND a.bucket >= o.first_oriented_at + INTERVAL '7 days'
                 AND a.bucket <  o.first_oriented_at + INTERVAL '14 days'
           ) AS retained_w2
    FROM signups s
    LEFT JOIN oriented o ON o.workspace_id = s.workspace_id
)
"""


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


async def growth_overview(
    session: AsyncSession,
    *,
    days: int = 28,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    emails, domains = _internal_patterns()
    params: dict[str, Any] = {
        "internal_emails": emails,
        "internal_domains": domains,
        "now": now,
        "since": since,
        "week_ago": now - timedelta(days=7),
    }

    totals = (
        await session.execute(
            text(
                "WITH " + _SIGNUPS_CTE + """
SELECT
    count(*) FILTER (WHERE signup_at >= :since) AS signups,
    count(*) FILTER (WHERE signup_at >= :since
                       AND signup_at <= CAST(:now AS timestamptz) - INTERVAL '24 hours')
        AS activation_eligible,
    count(*) FILTER (WHERE signup_at >= :since
                       AND signup_at <= CAST(:now AS timestamptz) - INTERVAL '24 hours'
                       AND activated) AS activated,
    count(*) FILTER (WHERE activated
                       AND first_oriented_at >= CAST(:since AS timestamptz) - INTERVAL '14 days'
                       AND first_oriented_at <= CAST(:now AS timestamptz) - INTERVAL '14 days')
        AS retention_eligible,
    count(*) FILTER (WHERE activated AND retained_w2
                       AND first_oriented_at >= CAST(:since AS timestamptz) - INTERVAL '14 days'
                       AND first_oriented_at <= CAST(:now AS timestamptz) - INTERVAL '14 days')
        AS retained
FROM cohort
"""
            ),
            params,
        )
    ).mappings().one()

    decisions = (
        await session.execute(
            text(
                "WITH " + _SIGNUPS_CTE + """,
active AS (
    SELECT DISTINCT a.workspace_id FROM agent_activity a
    JOIN signups s ON s.workspace_id = a.workspace_id
    WHERE a.bucket >= :week_ago
),
per_ws AS (
    SELECT a.workspace_id,
           (SELECT count(*) FROM product_events e
            WHERE e.workspace_id = a.workspace_id AND e.name = 'human_decision'
              AND e.occurred_at >= :week_ago) AS decisions
    FROM active a
)
SELECT count(*) AS active_workspaces,
       COALESCE(sum(decisions), 0) AS decisions,
       COALESCE(percentile_cont(0.5) WITHIN GROUP (ORDER BY decisions), 0) AS median,
       count(*) FILTER (WHERE decisions >= 3) AS at_target
FROM per_ws
"""
            ),
            params,
        )
    ).mappings().one()

    intent = (
        await session.execute(
            text(
                "WITH " + _SIGNUPS_CTE + """
SELECT
    count(DISTINCT e.workspace_id) FILTER (WHERE e.name = 'upgrade_intent') AS intent,
    count(DISTINCT e.workspace_id) FILTER (WHERE e.name = 'upgrade_viewed') AS viewed,
    -- A CLI setup has no workspace until it is approved: count every start.
    (SELECT count(*) FROM product_events c
     WHERE c.name = 'cli_init_started' AND c.occurred_at >= :since) AS cli_started,
    (SELECT count(*) FROM product_events c
     WHERE c.name = 'cli_init_completed' AND c.occurred_at >= :since) AS cli_completed
FROM product_events e
JOIN signups s ON s.workspace_id = e.workspace_id
WHERE e.occurred_at >= :since
"""
            ),
            params,
        )
    ).mappings().one()

    funnel = (
        await session.execute(
            text(
                "WITH " + _SIGNUPS_CTE + """
SELECT
    count(*) AS signed_up,
    count(*) FILTER (WHERE EXISTS (
        SELECT 1 FROM agent_tokens t WHERE t.workspace_id = c.workspace_id)) AS token_created,
    count(*) FILTER (WHERE c.first_oriented_at IS NOT NULL) AS oriented,
    count(*) FILTER (WHERE EXISTS (
        SELECT 1 FROM product_events e WHERE e.workspace_id = c.workspace_id
          AND e.name = 'human_decision')) AS decided,
    count(*) FILTER (WHERE c.retained_w2) AS retained
FROM cohort c
WHERE c.signup_at >= :since
"""
            ),
            params,
        )
    ).mappings().one()

    cohorts = (
        await session.execute(
            text(
                "WITH " + _SIGNUPS_CTE + """
SELECT date_trunc('week', signup_at) AS week,
       count(*) AS signups,
       count(*) FILTER (WHERE activated) AS activated,
       count(*) FILTER (WHERE activated
                          AND first_oriented_at <= CAST(:now AS timestamptz) - INTERVAL '14 days')
           AS retention_eligible,
       count(*) FILTER (WHERE activated AND retained_w2
                          AND first_oriented_at <= CAST(:now AS timestamptz) - INTERVAL '14 days')
           AS retained
FROM cohort
WHERE signup_at >= CAST(:now AS timestamptz) - INTERVAL '8 weeks'
GROUP BY 1
ORDER BY 1 DESC
"""
            ),
            params,
        )
    ).mappings().all()

    recent = (
        await session.execute(
            text(
                "WITH " + _SIGNUPS_CTE + """
SELECT c.workspace_id, c.workspace_name, c.email, c.tier, c.signup_at,
       c.first_oriented_at, c.activated, c.retained_w2,
       (SELECT count(*) FROM agent_tokens t WHERE t.workspace_id = c.workspace_id) AS agents,
       (SELECT max(a.bucket) FROM agent_activity a
        WHERE a.workspace_id = c.workspace_id) AS last_agent_activity,
       (SELECT count(*) FROM product_events e WHERE e.workspace_id = c.workspace_id
          AND e.name = 'human_decision' AND e.occurred_at >= :week_ago) AS decisions_7d,
       EXISTS (SELECT 1 FROM product_events e WHERE e.workspace_id = c.workspace_id
          AND e.name = 'upgrade_intent') AS upgrade_intent
FROM cohort c
ORDER BY c.signup_at DESC
LIMIT 50
"""
            ),
            params,
        )
    ).mappings().all()

    active = int(decisions["active_workspaces"])
    return {
        "generated_at": now.isoformat(),
        "window_days": days,
        "targets": TARGETS,
        "kpis": {
            "activation_rate": {
                "value": _ratio(int(totals["activated"]), int(totals["activation_eligible"])),
                "numerator": int(totals["activated"]),
                "denominator": int(totals["activation_eligible"]),
                "target": TARGETS["activation_rate"],
            },
            "week2_retention": {
                "value": _ratio(int(totals["retained"]), int(totals["retention_eligible"])),
                "numerator": int(totals["retained"]),
                "denominator": int(totals["retention_eligible"]),
                "target": TARGETS["week2_retention"],
            },
            "decisions_per_active_workspace": {
                "value": round(int(decisions["decisions"]) / active, 2) if active else None,
                "median": float(decisions["median"]),
                "active_workspaces": active,
                "at_target": int(decisions["at_target"]),
                "target": TARGETS["decisions_per_active_workspace"],
            },
            "upgrade_intent_workspaces": {
                "value": int(intent["intent"]),
                "viewed": int(intent["viewed"]),
                "target": TARGETS["upgrade_intent_workspaces"],
            },
        },
        "signups": int(totals["signups"]),
        "funnel": [
            {"step": "signed_up", "workspaces": int(funnel["signed_up"])},
            {"step": "token_created", "workspaces": int(funnel["token_created"])},
            {"step": "agent_oriented", "workspaces": int(funnel["oriented"])},
            {"step": "human_decision", "workspaces": int(funnel["decided"])},
            {"step": "retained_week2", "workspaces": int(funnel["retained"])},
        ],
        "cli": {
            "started": int(intent["cli_started"]),
            "completed": int(intent["cli_completed"]),
        },
        "cohorts": [
            {
                "week": row["week"].date().isoformat(),
                "signups": int(row["signups"]),
                "activated": int(row["activated"]),
                "activation_rate": _ratio(int(row["activated"]), int(row["signups"])),
                "retention_eligible": int(row["retention_eligible"]),
                "retained": int(row["retained"]),
                "week2_retention": _ratio(int(row["retained"]), int(row["retention_eligible"])),
            }
            for row in cohorts
        ],
        "recent": [
            {
                "workspace_id": str(row["workspace_id"]),
                "workspace_name": row["workspace_name"],
                "email": row["email"],
                "tier": row["tier"],
                "signup_at": row["signup_at"].isoformat(),
                "first_oriented_at": (
                    row["first_oriented_at"].isoformat() if row["first_oriented_at"] else None
                ),
                "activated": bool(row["activated"]),
                "retained_week2": bool(row["retained_w2"]),
                "agents": int(row["agents"]),
                "last_agent_activity": (
                    row["last_agent_activity"].isoformat()
                    if row["last_agent_activity"] else None
                ),
                "decisions_7d": int(row["decisions_7d"]),
                "upgrade_intent": bool(row["upgrade_intent"]),
            }
            for row in recent
        ],
    }
