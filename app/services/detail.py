"""Detail levels: summary / standard / full (2026-10-23 call-cost proposal).

Agents pay for every byte they receive. MCP list, sync and orientation calls
default to ``summary``: the same top-level structure, with each item cut to
the fields an agent needs to decide what to read next, and the timeline cut
to its newest entries. ``standard`` keeps every item field and caps the
timeline; ``full`` is the response as it was. REST keeps ``full`` unless
``detail`` is passed (the console reads the full shape).
"""

from __future__ import annotations

from typing import Any, Literal

Detail = Literal["summary", "standard", "full"]
LEVELS = ("summary", "standard", "full")

TASK_FIELDS = ("task_key", "title", "status", "priority", "namespace", "version",
               "updated_at", "assigned_role", "claimed_by")
DOCUMENT_FIELDS = ("doc_key", "namespace", "title", "version", "updated_at",
                   "verification_status", "tainted")
SLOT_FIELDS = ("slot_key", "namespace", "version", "updated_at", "locked_by",
               "locked_by_human")
TIMELINE_CAP = {"summary": 20, "standard": 50}
NAMESPACE_FIELDS = ("namespace", "my_access", "documents", "unverified", "last_updated_at")
ORIENTATION_TASK_FIELDS = ("task_id", "task_key", "title", "status", "assignee",
                           "assigned_role", "namespace")


def _pick(item: Any, fields: tuple[str, ...]) -> Any:
    if not isinstance(item, dict):
        return item
    return {key: item[key] for key in fields if key in item}


def _timeline_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    return {"id": item.get("id"), "event_type": item.get("event_type"),
            "agent_signature": item.get("agent_signature"),
            "namespace": item.get("namespace"), "created_at": item.get("created_at"),
            "key": payload.get("task_key") or payload.get("doc_key")}


def trim_items(items: list[Any], fields: tuple[str, ...], detail: str) -> list[Any]:
    return [_pick(item, fields) for item in items] if detail == "summary" else items


def sync(payload: dict[str, Any], detail: str, *, cards: bool = True) -> dict[str, Any]:
    """workspace_sync / GET /v1/sync. ``cards=False`` leaves items as they are
    (compact=true already has its own card shape)."""
    if detail == "full":
        return payload
    timeline = payload.get("timeline") or []
    cap = TIMELINE_CAP[detail]
    total = payload.get("timeline_total", len(timeline))  # compact already cut it
    payload["timeline_truncated"] = total > cap or len(timeline) > cap
    timeline = timeline[:cap]
    if detail == "summary":
        timeline = [_timeline_item(item) for item in timeline]
    if detail == "summary" and cards:
        payload["tasks"] = trim_items(payload.get("tasks") or [], TASK_FIELDS, detail)
        payload["documents"] = trim_items(payload.get("documents") or [], DOCUMENT_FIELDS,
                                          detail)
        payload["slots"] = trim_items(payload.get("slots") or [], SLOT_FIELDS, detail)
    payload["timeline"] = timeline
    payload["detail"] = detail
    return payload


def listing(payload: dict[str, Any], fields: tuple[str, ...], detail: str) -> dict[str, Any]:
    """list_tasks / list_documents: same envelope, slimmer items."""
    if detail != "summary":
        return payload
    payload["items"] = trim_items(payload.get("items") or [], fields, detail)
    payload["detail"] = detail
    return payload


def orientation(payload: dict[str, Any], detail: str) -> dict[str, Any]:
    """get_orientation for a returning agent: pointers instead of prose."""
    if detail != "summary":
        return payload
    if isinstance(payload.get("namespace_map"), list):
        payload["namespace_map"] = [_pick(item, NAMESPACE_FIELDS)
                                    for item in payload["namespace_map"]]
    for key in ("claimable_tasks", "my_tasks"):
        block = payload.get(key)
        if isinstance(block, dict) and isinstance(block.get("top"), list):
            block["top"] = [_pick(item, ORIENTATION_TASK_FIELDS) for item in block["top"]]
        elif isinstance(block, list):
            payload[key] = [_pick(item, ORIENTATION_TASK_FIELDS) for item in block]
    if "conventions" in payload:
        payload["conventions"] = {"see": "get_orientation(sections=['conventions'], detail='full')"}
    if isinstance(payload.get("collaborators"), list):
        payload["collaborators"] = [
            _pick(item, ("agent_id", "user_id", "type", "role", "status"))
            for item in payload["collaborators"]]
    payload["detail"] = detail
    return payload
