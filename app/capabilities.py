"""What this deployment can do, for agents reading the root (retest finding 2).

``describe`` lists the feature flags with their current state and every
token-facing REST endpoint that has no MCP tool of the same name, so an
agent reading only the root and ``get_api_docs`` can enumerate the whole
surface and tell "not there" from "switched off". A call to a switched-off
feature answers ``feature_disabled`` (403) naming the flag.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.config import get_settings

settings = get_settings()

# Paths agents never use with their own token (people, operators, login).
_NOT_AGENT_FACING = ("/v1/admin", "/v1/human", "/v1/auth")

_cache: dict[str, Any] = {}


def feature_flags() -> dict[str, dict[str, Any]]:
    return {
        "child_token_issuance": {
            "enabled": settings.child_token_issuance, "setting": "CHILD_TOKEN_ISSUANCE",
            "affects": ["issue_child_token", "POST /v1/tokens"]},
        "budget_enforcement": {
            "enabled": settings.budget_mode == "enforce", "setting": "BUDGET_MODE",
            "value": settings.budget_mode,
            "affects": ["429 rate_limited / budget_exhausted / circuit_open",
                        "shadow mode records would_* only"]},
        "scheduled_exports": {
            "enabled": settings.scheduled_exports, "setting": "SCHEDULED_EXPORTS",
            "affects": ["weekly full + daily incremental exports per tenant"]},
        "retrieval_probe": {
            "enabled": settings.retrieval_probe_enabled, "setting": "RETRIEVAL_PROBE_ENABLED",
            "affects": ["daily self_recall@50 per workspace"]},
        "mcp": {"enabled": settings.mcp_enabled, "setting": "MCP_ENABLED", "affects": ["/mcp/"]},
        "api_reference": {
            "enabled": settings.docs_enabled, "setting": "DOCS_ENABLED",
            "affects": ["/api-reference", "/v1/openapi.json"]},
    }


def _all_routes(routes: list[Any]) -> list[Any]:
    """Flatten included routers (newer FastAPI keeps them as wrappers)."""
    flat: list[Any] = []
    for route in routes:
        nested = getattr(route, "original_router", None)
        flat.extend(_all_routes(nested.routes) if nested is not None else [route])
    return flat


def rest_only(app: FastAPI) -> list[dict[str, str]]:
    """Token-facing REST endpoints whose handler has no MCP tool of the same name."""
    from app.root_page import ROOT_PRIMITIVES

    tools = set(ROOT_PRIMITIVES)
    items: list[dict[str, str]] = []
    for route in _all_routes(app.routes):
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        if not route.path.startswith("/v1/") or route.path.startswith(_NOT_AGENT_FACING):
            continue
        if route.endpoint.__name__ in tools:
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            items.append({"method": method, "path": route.path, "summary": route.summary or ""})
    return sorted(items, key=lambda item: (item["path"], item["method"]))


def describe(app: FastAPI) -> dict[str, Any]:
    """The compact block for the root: flags plus a pointer to the full list."""
    if "value" not in _cache:
        _cache["value"] = {
            "feature_flags": feature_flags(),
            "rest_without_same_named_tool": len(rest_only(app)),
            "details": "get_api_docs(section='capabilities') lists every flag and each REST "
                       "endpoint that has no MCP tool of the same name (some have an MCP "
                       "equivalent under another name); /v1/openapi.json is the full contract",
        }
    return _cache["value"]


def full(app: FastAPI) -> dict[str, Any]:
    """For get_api_docs: flags and every endpoint with method, path and summary."""
    return {"feature_flags": feature_flags(), "rest_without_same_named_tool": rest_only(app),
            "mcp_tools": "tools/list (MCP) or the root's primitives"}
