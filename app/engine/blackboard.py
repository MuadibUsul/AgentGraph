"""Deterministic blackboard mechanics: leases, JSON Patch, section patching,
handoff briefs, and circuit-breaker helpers."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jsonpatch
from jsonpatch import JsonPatchConflict, JsonPatchException

from app.config import get_settings
from app.errors import ApiError, NotFound

settings = get_settings()

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def lease_expiry(seconds: int | None = None) -> datetime:
    ttl = seconds or settings.default_task_lease_seconds
    return utcnow() + timedelta(seconds=ttl)


def lease_is_active(
    owner: str | None,
    expires_at: datetime | None,
    now: datetime | None = None,
) -> bool:
    if not owner or expires_at is None:
        return False
    reference = now or utcnow()
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > reference


def lease_can_be_taken(
    owner: str | None,
    expires_at: datetime | None,
    agent: str,
    now: datetime | None = None,
) -> bool:
    if not owner or owner == agent:
        return True
    return not lease_is_active(owner, expires_at, now)


def validate_handoff_brief(brief: dict[str, Any] | None) -> dict[str, Any]:
    """Validate and normalize the L1 handoff contract."""
    if not isinstance(brief, dict):
        raise ApiError(422, "invalid_handoff_brief", "handoff brief must be an object")
    required = {
        "summary",
        "key_metrics",
        "unverified_hypotheses",
        "risk_flag",
    }
    missing = sorted(required - set(brief))
    if missing:
        raise ApiError(
            422,
            "invalid_handoff_brief",
            f"handoff brief is missing required fields: {', '.join(missing)}",
            details={"missing": missing},
        )
    summary = brief["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise ApiError(422, "invalid_handoff_brief", "summary must be a non-empty string")
    metrics = brief["key_metrics"]
    if not isinstance(metrics, dict):
        raise ApiError(422, "invalid_handoff_brief", "key_metrics must be an object")
    hypotheses = brief["unverified_hypotheses"]
    if not isinstance(hypotheses, list) or not all(isinstance(item, str) for item in hypotheses):
        raise ApiError(
            422,
            "invalid_handoff_brief",
            "unverified_hypotheses must be a list of strings",
        )
    if not isinstance(brief["risk_flag"], bool):
        raise ApiError(422, "invalid_handoff_brief", "risk_flag must be a boolean")
    normalized = {
        "summary": summary.strip(),
        "key_metrics": metrics,
        "unverified_hypotheses": [item.strip() for item in hypotheses if item.strip()],
        "risk_flag": brief["risk_flag"],
    }
    for key, value in brief.items():
        if key not in normalized:
            normalized[key] = value
    encoded = json.dumps(normalized, ensure_ascii=False, default=str)
    if len(encoded.encode("utf-8")) > settings.max_brief_bytes:
        raise ApiError(
            422,
            "invalid_handoff_brief",
            f"handoff brief exceeds {settings.max_brief_bytes} bytes",
        )
    return normalized


def apply_json_patch(
    document: dict[str, Any],
    operations: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(operations, list) or not operations:
        raise ApiError(422, "invalid_json_patch", "ops must be a non-empty list")
    if len(operations) > 100:
        raise ApiError(422, "invalid_json_patch", "at most 100 patch operations are allowed")
    try:
        patch = jsonpatch.JsonPatch(operations)
        result = patch.apply(document, in_place=False)
    except (JsonPatchConflict, JsonPatchException, TypeError, ValueError) as exc:
        raise ApiError(
            422, "invalid_json_patch", f"JSON Patch failed: {exc}"
        ) from exc
    if not isinstance(result, dict):
        raise ApiError(422, "invalid_json_patch", "state document must remain an object")
    return result, operations


def patch_markdown_section(
    content: str,
    section_heading: str,
    new_markdown: str,
) -> str:
    """Replace the body under a Markdown heading until the next heading of the
    same or a higher level. The heading itself is preserved."""
    wanted = section_heading.strip().lstrip("#").strip()
    if not wanted:
        raise ApiError(422, "invalid_section", "section_heading is required")
    lines = content.splitlines()
    start: int | None = None
    level: int | None = None
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match and match.group(2).strip().casefold() == wanted.casefold():
            start = index
            level = len(match.group(1))
            break
    if start is None or level is None:
        raise NotFound("document_section", f"section '{section_heading}' not found")

    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = _HEADING.match(lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break

    replacement = new_markdown.strip("\n")
    body = [""]
    if replacement:
        body.extend(replacement.splitlines())
    body.append("")
    updated = lines[: start + 1] + body + lines[end:]
    return "\n".join(updated).rstrip() + "\n"


def parse_doc_uri(uri: str) -> str:
    cleaned = uri.strip()
    from app.addresses import is_canonical

    if is_canonical(cleaned, "doc"):
        return cleaned
    for prefix in ("doc://", "doc:///"):
        if cleaned.startswith(prefix):
            doc_key = cleaned[len(prefix):].strip("/")
            if doc_key:
                return doc_key
    raise ApiError(
        400,
        "unsupported_artifact_uri",
        "only doc://<doc_key> artifact URIs can be dereferenced by the server",
    )


def task_public_payload(task: Any, *, include_artifact: bool = False) -> dict[str, Any]:
    payload = {
        "id": str(task.id),
        "task_key": task.task_key,
        "title": task.title,
        "description": task.description,
        "status": task.status.value,
        "priority": task.priority,
        "assigned_role": task.assigned_role,
        "namespace": task.namespace,
        "claimed_by": task.claimed_by,
        "lease_owner": task.lease_owner,
        "lease_expires_at": task.lease_expires_at.isoformat()
        if task.lease_expires_at
        else None,
        "handoff_brief": task.handoff_brief,
        "artifact_uri": task.artifact_uri,
        "human_feedback": task.human_feedback,
        "ping_pong_count": task.ping_pong_count,
        "max_retries": task.max_retries,
        "version": task.version,
        "created_by": task.created_by,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }
    if include_artifact:
        payload["input_context"] = task.input_context
        payload["output_artifact"] = task.output_artifact
        payload["reject_history"] = task.reject_history
    return payload


def batch_uuid(value: uuid.UUID | None) -> uuid.UUID:
    return value or uuid.uuid4()
