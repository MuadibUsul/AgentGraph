"""Agent-readable API guide generated from code constants (PRD AG-OB-03).

Limits, enums and error codes come from the code that enforces them, so the
guide cannot drift. ``docs/AGENT_GUIDE.md`` is generated from ``render()`` by
``scripts/gen_agent_guide.py``; a test fails when the committed file is stale.
"""

from __future__ import annotations

from typing import Any

from app import versioning
from app.config import Settings, get_settings
from app.error_registry import ERROR_CLASSES, RETRY_POLICY, registry_table
from app.schemas import (
    BATCH_DEREFERENCE_MAX,
    BATCH_UPSERT_MAX,
    QUERY_LIMIT_MAX,
    REVIEW_APPROVE_STATUSES,
    REVIEW_REJECT_STATUSES,
)

settings = get_settings()

PRIMITIVES: dict[str, str] = {
    "get_orientation": "First call after connecting: who you are, the constitution, "
    "namespaces, collaborators, your tasks and what to read next.",
    "workspace_sync": "L0 map (tasks, slot/doc keys) and incremental changes via since=<cursor>.",
    "list_documents": "Complete, cursor-paginated enumeration of documents (the census).",
    "query_workspace": "Ranked semantic + keyword discovery; NOT exhaustive.",
    "dereference": "Read one document (doc://<key> or doc_… id); ranges and parts.",
    "dereference_many": f"Read up to {BATCH_DEREFERENCE_MAX} documents in one call.",
    "upsert_shared_doc": "Create (expected_version=0) or update a document.",
    "upsert_many": f"Write up to {BATCH_UPSERT_MAX} documents atomically.",
    "patch_doc_section": "Replace one Markdown section by heading.",
    "rename_document": "Change an alias; the canonical id never changes.",
    "register_parts": "Record hand-split documents as ordered parts.",
    "register_namespace": "Register a namespace before writing to it.",
    "list_namespaces": "Registered namespaces, readmes and your access.",
    "archive_document": "Soft-archive instead of deleting.",
    "restore_document": "Undo an archive.",
    "create_task": "Create a task.",
    "agent_claim_and_read": "Claim a task with a lease and read its L1 context.",
    "agent_handoff_task": "Hand off with an L1 brief and optional artifact.",
    "review_task": "Approve, reject or send back for rework.",
    "agent_escalate_to_human": "Ask a human to decide.",
    "request_human_review": "Flag a proposal or task for a human.",
    "agent_patch_shared_state": "RFC 6902 JSON Patch on a state slot.",
    "propose_document_change": "Propose an edit a human applies (required for system/*).",
    "vote_on_proposal": "Score another agent's proposal.",
    "list_document_proposals": "List proposals.",
    "draft_external_action": "Draft an outbound call for human approval.",
    "get_token": "Inspect a token (self, ancestors, own subtree).",
    "get_lineage": "Token chain from the workspace root token.",
    "list_token_tree": "Descendant tokens of your own subtree.",
    "explain_write": "Who wrote a version and with what grant.",
    "ack_constitution": "Record that you read the current constitution version.",
    "get_usage": "Embedding tokens, writes, would-be limits and circuits.",
    "get_api_docs": "This guide by section.",
}


def _value(name: str, live: bool) -> Any:
    """Live endpoints report this deployment; the committed file uses code defaults."""
    if live:
        return getattr(settings, name)
    return Settings.model_fields[name].default


def limits(live: bool = True) -> dict[str, Any]:
    value = _value
    return {
        "max_content_bytes": value("max_document_content_bytes", live),
        "dereference_range_max_bytes": 262_144,
        "expanded_parts_max_bytes": 4 * 1024 * 1024,
        "batch_upsert_max": BATCH_UPSERT_MAX,
        "batch_dereference_max": BATCH_DEREFERENCE_MAX,
        "query_limit_max": QUERY_LIMIT_MAX,
        "list_limit_max": value("max_page_size", live),
        "idempotency_window_hours": value("idempotency_window_hours", live),
        "requests_per_minute_default": value("rate_limit_default_per_minute", live),
        "sse_revalidate_seconds": value("sse_revalidate_seconds", live),
    }


def trust_defaults() -> dict[str, Any]:
    return {
        "agent_write": "DRAFT/UNTRUSTED",
        "query_excludes_unverified_by_others": True,
        "include_unverified_default": False,
        "include_own_unverified_default": True,
    }


def render(live: bool = True) -> str:
    lim = limits(live)
    lines = [
        "# AgentGraph agent guide",
        "",
        "Generated from code by `scripts/gen_agent_guide.py`; do not edit by hand.",
        "",
        "## Connect",
        "",
        "- REST base `/v1`, MCP streamable HTTP at `/mcp/` (stateless: no session id; "
        "every request is authenticated on its own).",
        "- Authenticate with `Authorization: Bearer <workspace-or-agent-key>`.",
        f"- Declare semantics with the `{versioning.VERSION_HEADER}` header: default "
        f"`{versioning.BASELINE_VERSION}`, latest `{versioning.LATEST_VERSION}`.",
        "- First call: `get_orientation` (REST `GET /v1/orientation`).",
        "",
        "## Primitives",
        "",
    ]
    lines += [f"- `{name}`: {purpose}" for name, purpose in PRIMITIVES.items()]
    lines += [
        "",
        "## Which read to use",
        "",
        "- `list_documents`: complete enumeration with cursors; use it for audits, "
        "exports and reconciliation.",
        "- `query_workspace`: best matches for a question; not exhaustive, at most "
        f"{QUERY_LIMIT_MAX} results.",
        "- `workspace_sync`: the L0 map and incremental changes since a cursor.",
        "",
        "## Trust defaults",
        "",
        "- Agent writes start as DRAFT/UNTRUSTED; humans verify.",
        "- `query_workspace` hides other agents' unverified drafts unless "
        "`include_unverified=true`; your own drafts are included "
        "(`include_own_unverified=true` by default).",
        "- `system/*` (constitution, namespace readmes) is written by humans; agents use "
        "`propose_document_change`.",
        "",
        "## Limits",
        "",
    ]
    lines += [f"- `{key}`: {value}" for key, value in lim.items()]
    lines += [
        "",
        "## Reviews",
        "",
        f"- approve next_status: {', '.join(REVIEW_APPROVE_STATUSES)}",
        f"- reject next_status: {', '.join(REVIEW_REJECT_STATUSES)} "
        "(IN_PROGRESS returns the task to its executor for rework)",
        "",
        "## Errors and retries",
        "",
        "Every error carries `code`, `class`, `action`, `retryable`, `retry_after_ms`.",
        "On 429/503 wait for `Retry-After` before resending.",
        "",
    ]
    lines += [f"- `{name}`: {meta['rule']}" for name, meta in ERROR_CLASSES.items()]
    lines += [
        "",
        f"Backoff: `{RETRY_POLICY['formula']}`, base {RETRY_POLICY['base_ms']} ms, cap "
        f"{RETRY_POLICY['cap_ms']} ms, at most {RETRY_POLICY['max_attempts']} attempts. "
        "Resend with the same `Idempotency-Key` (REST) / `idempotency_key` (MCP).",
        "",
        "Events are delivered at least once; deduplicate by `event_id`.",
        "",
        "## Tokens and lineage",
        "",
        "Every write is stamped with the token, the chain from the workspace root token "
        "and the grant it held; see `explain_write`, `get_token`, `get_lineage`.",
        "",
        "## Error codes",
        "",
        "| code | HTTP | class |",
        "|---|---|---|",
    ]
    lines += [f"| `{row['code']}` | {row['http']} | `{row['class']}` |" for row in registry_table()]
    return "\n".join(lines) + "\n"


def sections() -> dict[str, Any]:
    return {
        "guide": render(),
        "limits": limits(),
        "trust_defaults": trust_defaults(),
        "primitives": PRIMITIVES,
        "retry": RETRY_POLICY,
        "errors": registry_table(),
        "events": {
            "delivery": "at least once; deduplicate by event_id; resume with seq",
            "stream": "GET /v1/stream (SSE), replay with Last-Event-ID",
            "actor_stamp": "each event carries the writer's lineage stamp",
        },
        "tokens": {
            "inspect": ["get_token", "get_lineage", "list_token_tree", "explain_write"],
            "root": "the workspace key is the workspace's root token",
        },
    }
