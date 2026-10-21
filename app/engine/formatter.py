"""Human/agent views for documents, handoff briefs, and token-efficient output."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings

settings = get_settings()

_WHITESPACE = re.compile(r"\s+")


def compact_json(value: Any, max_chars: int | None = None) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), default=str
            )
        except (TypeError, ValueError):
            text = str(value)
    text = _WHITESPACE.sub(" ", text).strip()
    if max_chars is not None and len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + "..."
    return text


def preview(text: str | None, max_chars: int | None = None) -> str:
    if not text:
        return ""
    collapsed = _WHITESPACE.sub(" ", text).strip()
    limit = max_chars or settings.preview_chars
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 3].rstrip() + "..."
    return collapsed


def time_ago(created_at: datetime | None, now: datetime | None = None) -> str:
    if created_at is None:
        return "unknown"
    reference = now or datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    seconds = max((reference - created_at).total_seconds(), 0.0)
    if seconds < 60:
        return "just now" if seconds < 5 else f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86_400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86_400)}d ago"


def short_id(value: Any, length: int = 8) -> str:
    return f"{str(value)[:length]}.."


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, int(round(len(text) / settings.token_chars_per_token)))


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if not value or re.search(r"[:#\[\]{},&\n]", value):
            return json.dumps(value, ensure_ascii=False)
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def render_document(
    document: Any,
    *,
    include_content: bool = True,
    content_chars: int | None = None,
) -> str:
    """Render the agent view: YAML frontmatter + Markdown body."""
    reserved = {
        "doc_key",
        "title",
        "version",
        "namespace",
        "updated_by",
        "locked_by_human",
        "human_note",
    }
    lines = [
        "---",
        f"doc_key: {_yaml_scalar(document.doc_key)}",
        f"title: {_yaml_scalar(document.title)}",
        f"version: {document.version}",
        f"namespace: {_yaml_scalar(document.namespace)}",
        f"updated_by: {_yaml_scalar(document.updated_by)}",
        f"verification_status: {_yaml_scalar(document.verification_status)}",
        f"trust_level: {_yaml_scalar(document.trust_level)}",
        f"tainted: {_yaml_scalar(bool(getattr(document, 'tainted', False)))}",
        f"locked_by_human: {_yaml_scalar(bool(document.locked_by_human))}",
    ]
    if document.human_note:
        lines.append(f"human_note: {_yaml_scalar(document.human_note)}")
    for key, value in (document.frontmatter or {}).items():
        if key in reserved:
            continue
        lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    if include_content:
        body = document.content
        if content_chars is not None and len(body) > content_chars:
            body = body[: content_chars - 3].rstrip() + "..."
    else:
        body = preview(document.content)
    if getattr(document, "tainted", False):
        body = (
            "[[TAINTED_PROVENANCE]]\n"
            + body.rstrip()
            + "\n[[/TAINTED_PROVENANCE]]"
        )
    if getattr(document, "trust_level", "UNTRUSTED") == "UNTRUSTED":
        # Prompt-injection guard: make untrusted provenance explicit to the LLM.
        body = "[[UNTRUSTED_SOURCE]]\n" + body.rstrip() + "\n[[/UNTRUSTED_SOURCE]]"
    lines.append(body.rstrip())
    return "\n".join(lines)


def render_handoff_brief(
    task_key: str,
    brief: dict[str, Any] | None,
    *,
    artifact_uri: str | None = None,
) -> str:
    if not brief:
        return f"[{task_key}] no L1 handoff brief"
    lines = [
        f"[{task_key}] L1 handoff brief",
        f"- summary: {preview(str(brief.get('summary', '')), 240)}",
        f"- key_metrics: {compact_json(brief.get('key_metrics', {}), 240)}",
        f"- unverified_hypotheses: {compact_json(brief.get('unverified_hypotheses', []), 240)}",
        f"- risk_flag: {brief.get('risk_flag')}",
    ]
    if artifact_uri:
        lines.append(f"- artifact_uri: {artifact_uri}")
    return "\n".join(lines)


def render_task_card(task: Any) -> str:
    lease = time_ago(task.lease_expires_at) if task.lease_expires_at else "none"
    lines = [
        f"[{task.task_key}] {task.title}",
        f"status={task.status.value} role={task.assigned_role or '*'} "
        f"priority={task.priority} version={task.version}",
        f"claimed_by={task.claimed_by or 'none'} lease={lease} "
        f"ping_pong={task.ping_pong_count}/{task.max_retries}",
    ]
    if task.handoff_brief:
        lines.append(
            render_handoff_brief(
                task.task_key, task.handoff_brief, artifact_uri=task.artifact_uri
            )
        )
    return "\n".join(lines)


def render_slot(slot: Any) -> str:
    lock = "HUMAN" if slot.locked_by_human else (slot.locked_by or "none")
    return (
        f"[{slot.slot_key}] v{slot.version} updated_by={slot.updated_by} "
        f"lock={lock} state={compact_json(slot.current_state, 500)}"
    )


def render_timeline_item(item: Any, now: datetime | None = None) -> str:
    payload = compact_json(item.payload or {}, settings.timeline_payload_chars)
    suffix = f": {payload}" if payload and payload != "{}" else ""
    return (
        f"- [{time_ago(item.created_at, now)} by {item.agent_signature}] "
        f"{item.event_type}{suffix}"
    )
