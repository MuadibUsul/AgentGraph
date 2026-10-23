"""Human-facing service descriptor rendered at ``/``.

The root URL has two audiences. Agents receive the JSON descriptor unchanged;
browsers receive this page, which keeps the same information but adds a visible
human-console entry and a quiet live network behind it.
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT_PRIMITIVES: tuple[str, ...] = (
    "get_orientation",
    "ack_constitution",
    "issue_child_token",
    "revoke_token",
    "renew_token",
    "register_agent",
    "get_agent",
    "list_agents",
    "update_agent",
    "set_status",
    "list_tasks",
    "write_continuation",
    "create_subtasks",
    "get_task_tree",
    "clear_taint",
    "recall_document",
    "get_recall",
    "get_budget",
    "set_budget",
    "list_budgets",
    "append_entries",
    "read_entries",
    "list_alerts",
    "ack_alert",
    "get_metrics",
    "supersede_document",
    "set_watermark",
    "get_watermark",
    "get_digest",
    "request_peer_review",
    "submit_peer_review",
    "list_resources",
    "acquire_resource",
    "release_resource",
    "get_lineage_attestation",
    "list_inbox",
    "ack_inbox",
    "get_tenant_tree",
    "create_child_tenant",
    "move_tenant",
    "attach_tenant",
    "detach_tenant",
    "get_effective_policy",
    "set_policy",
    "list_shares",
    "share",
    "revoke_share",
    "open_admin_access",
    "get_usage",
    "get_index_status",
    "heartbeat",
    "release_lease",
    "list_leases",
    "export_workspace",
    "get_export",
    "list_exports",
    "poll_events",
    "subscribe",
    "list_subscriptions",
    "unsubscribe",
    "workspace_sync",
    "create_task",
    "review_task",
    "request_human_review",
    "agent_claim_and_read",
    "agent_patch_shared_state",
    "agent_handoff_task",
    "agent_escalate_to_human",
    "upsert_shared_doc",
    "upsert_many",
    "patch_doc_section",
    "query_workspace",
    "list_documents",
    "dereference",
    "dereference_many",
    "archive_document",
    "restore_document",
    "rename_document",
    "register_namespace",
    "list_namespaces",
    "get_token",
    "record_gate_result",
    "get_audit_view",
    "report_change_claim",
    "verify_change_claim",
    "get_lineage",
    "list_token_tree",
    "explain_write",
    "register_parts",
    "propose_document_change",
    "vote_on_proposal",
    "list_document_proposals",
    "draft_external_action",
    "get_api_docs",
)

_CURATED_KEYS = (
    "name",
    "version",
    "rest",
    "mcp",
    "health",
    "agent_docs",
    "first_call",
    "primitives",
    "dashboard",
)

_TEMPLATE_PATH = Path(__file__).with_name("templates") / "root.html"


def _curated(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    curated = {key: descriptor[key] for key in _CURATED_KEYS if key in descriptor}
    primitives = curated.get("primitives")
    if isinstance(primitives, (list, tuple)):
        # The API contract carries the full tool list; the human page shows its size.
        curated["primitives"] = len(primitives)
    return curated


def _code_line(key: str, value: Any, *, last: bool = False) -> str:
    if key == "dashboard" and isinstance(value, str):
        return (
            '<div class="code-line inner">'
            '<span class="quoted">&quot;</span><span class="key">dashboard</span>'
            '<span class="quoted">&quot;</span><span class="punctuation">: </span>'
            '<a class="entry" href="/dashboard/" '
            'aria-label="Open the AgentGraph human console">'
            '<span class="quoted">&quot;</span>'
            f'<span class="value">{html.escape(value)}</span>'
            '<span class="quoted">&quot;</span>'
            '<span class="entry-arrow" aria-hidden="true">↗</span></a>'
            "</div>"
        )

    comma = "" if last else '<span class="comma">,</span>'
    key_html = (
        f'<span class="quoted">&quot;</span><span class="key">{html.escape(key)}</span>'
        '<span class="quoted">&quot;</span><span class="punctuation">: </span>'
    )
    if isinstance(value, bool):
        value_html = f'<span class="value">{"true" if value else "false"}</span>'
    elif isinstance(value, (int, float)):
        value_html = f'<span class="number">{value}</span>'
    elif value is None:
        value_html = '<span class="value">null</span>'
    else:
        value_html = (
            '<span class="quoted">&quot;</span>'
            f'<span class="value">{html.escape(str(value))}</span>'
            '<span class="quoted">&quot;</span>'
        )
    return f'<div class="code-line inner">{key_html}{value_html}{comma}</div>'


def _code_lines(curated: Mapping[str, Any]) -> str:
    keys = list(curated)
    lines = ['<div class="code-line"><span class="brace">{</span></div>']
    for index, key in enumerate(keys):
        lines.append(_code_line(key, curated[key], last=index == len(keys) - 1))
    lines.append(
        '<div class="code-line last"><span class="brace">}</span>'
        '<span class="end-caret" aria-hidden="true"></span></div>'
    )
    return "\n".join(lines)


@lru_cache(maxsize=1)
def _template() -> str:
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def render_root_page(descriptor: Mapping[str, Any]) -> str:
    curated = _curated(descriptor)
    payload = json.dumps(curated, ensure_ascii=False, indent=2) + "\n"
    return (
        _template()
        .replace("__CODE_LINES__", _code_lines(curated))
        .replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
    )
