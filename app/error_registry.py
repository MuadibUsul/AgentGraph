"""Error-code registry: every client-facing code maps to a class and an action.

This table is the single source for PRD appendix A (``docs/ERROR_CODES.md`` is
generated from it by ``scripts/gen_error_codes.py``). A test scans the code base
and fails when a code is raised without being registered here, so an agent can
decide to retry, fix, re-authenticate, abandon or escalate from the error body
alone, without reading documentation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The seven classes of the failure-semantics protocol (PRD §6.1 point 3).
RETRY_AFTER = "retry_after"
RETRY_BACKOFF = "retry_backoff"
REFRESH_AND_RETRY = "refresh_and_retry"
FIX_REQUEST = "fix_request"
REAUTH = "reauth"
ABANDON = "abandon"
ESCALATE_HUMAN = "escalate_human"

ERROR_CLASSES: dict[str, dict[str, Any]] = {
    RETRY_AFTER: {
        "retryable": True,
        "action": "wait_retry_after_then_retry_same_request",
        "rule": "Wait for Retry-After, then resend unchanged with the same idempotency key.",
    },
    RETRY_BACKOFF: {
        "retryable": True,
        "action": "backoff_then_retry_same_request",
        "rule": "Retry with full-jitter exponential backoff, at most 5 times, "
        "with the same idempotency key.",
    },
    REFRESH_AND_RETRY: {
        "retryable": True,
        "action": "refresh_state_then_retry_once",
        "rule": "Merge or resync using the current state in details "
        "(current_version etc.), then retry once on that new state.",
    },
    FIX_REQUEST: {
        "retryable": False,
        "action": "fix_request_then_resend",
        "rule": "Never resend unchanged; correct the request using details first.",
    },
    REAUTH: {
        "retryable": False,
        "action": "stop_and_reauthenticate",
        "rule": "Stop; ask the parent token holder or a human for a valid credential.",
    },
    ABANDON: {
        "retryable": False,
        "action": "abandon_and_release_local_state",
        "rule": "Give up this action, release local state (e.g. leases) and re-read "
        "global state if needed.",
    },
    ESCALATE_HUMAN: {
        "retryable": False,
        "action": "stop_and_escalate_to_human",
        "rule": "Stop automatic retries; call agent_escalate_to_human or wait for a human.",
    },
}

# Canonical retry algorithm (PRD §6.1 point 4); published through get_api_docs.
RETRY_POLICY: dict[str, Any] = {
    "algorithm": "full_jitter_exponential",
    "formula": "sleep_ms = max(retry_after_ms, random(0, min(cap_ms, base_ms * 2**attempt)))",
    "base_ms": 500,
    "cap_ms": 30_000,
    "max_attempts": 5,
    "idempotency": "Resend with the same idempotency_key on every retry.",
    "delivery": "Events are delivered at least once; deduplicate by event_id.",
}


@dataclass(frozen=True)
class ErrorSpec:
    status: int
    error_class: str
    rule: str
    action: str | None = None

    @property
    def machine_action(self) -> str:
        return self.action or ERROR_CLASSES[self.error_class]["action"]

    @property
    def retryable(self) -> bool:
        return bool(ERROR_CLASSES[self.error_class]["retryable"])


def _spec(status: int, error_class: str, rule: str, action: str | None = None) -> ErrorSpec:
    return ErrorSpec(status, error_class, rule, action)


REGISTRY: dict[str, ErrorSpec] = {
    # --- retry_after
    "rate_limited": _spec(429, RETRY_AFTER, "Wait for Retry-After."),
    "overloaded": _spec(503, RETRY_AFTER, "Wait for Retry-After; never resend earlier."),
    "contention": _spec(409, RETRY_AFTER, "Wait for Retry-After, then retry."),
    "export_in_progress": _spec(
        409, RETRY_AFTER, "Poll details.export_id instead of starting another export."
    ),
    "idempotency_in_progress": _spec(
        409, RETRY_AFTER, "Wait for Retry-After and query the result with the same key."
    ),
    "embedding_budget_exceeded": _spec(
        429, RETRY_AFTER, "Embedding byte budget for this minute is spent; wait."
    ),
    "sse_capacity": _spec(429, RETRY_AFTER, "Too many event streams; reconnect later."),
    # --- retry_backoff
    "internal_error": _spec(
        500, RETRY_BACKOFF, "Back off and retry at most 5 times, then escalate_human."
    ),
    "database_unavailable": _spec(503, RETRY_BACKOFF, "Back off and retry."),
    "embedding_failed": _spec(502, RETRY_BACKOFF, "Embedding provider failed; back off."),
    "tenant_context_unavailable": _spec(503, RETRY_BACKOFF, "Back off and retry."),
    "google_unavailable": _spec(503, RETRY_BACKOFF, "Back off and retry sign-in."),
    # --- refresh_and_retry
    "version_conflict": _spec(
        409, REFRESH_AND_RETRY, "Merge against details.current_version, then rewrite."
    ),
    "section_conflict": _spec(409, REFRESH_AND_RETRY, "Re-read the section, then rewrite."),
    "cursor_expired": _spec(
        410, REFRESH_AND_RETRY, "Drop the cursor and resync with workspace_sync."
    ),
    "proposal_conflict": _spec(
        409, REFRESH_AND_RETRY, "Re-read the document and rebase the proposal."
    ),
    "rebase_conflict": _spec(
        409, REFRESH_AND_RETRY, "Re-read from base_version and redo non-overlapping edits."
    ),
    "undo_conflict": _spec(
        409, REFRESH_AND_RETRY, "Review details.conflicts; retry with force=true if intended."
    ),
    # --- fix_request (PRD appendix A)
    "cursor_invalid": _spec(400, FIX_REQUEST, "Fix the cursor per details."),
    "tenant_id_not_allowed": _spec(
        400, FIX_REQUEST, "Remove tenant_id from the request.", "remove_tenant_id"
    ),
    "unsupported_api_version": _spec(
        400, FIX_REQUEST, "Use a version from details.supported.", "use_supported_api_version"
    ),
    "field_not_writable": _spec(403, FIX_REQUEST, "Remove the field from the request."),
    "system_namespace_protected": _spec(
        403, FIX_REQUEST, "Use propose_document_change instead.", "propose_document_change"
    ),
    "admin_access_required": _spec(
        409, FIX_REQUEST, "Call open_admin_access first (the node is notified)."
    ),
    "agent_id_taken": _spec(409, FIX_REQUEST, "Choose another agent_id."),
    "alias_taken": _spec(409, FIX_REQUEST, "Choose another alias."),
    "alias_tombstoned": _spec(409, FIX_REQUEST, "Choose another alias; tombstones never reuse."),
    "idempotency_key_reused": _spec(
        409,
        FIX_REQUEST,
        "Generate a new idempotency key; confirm this is a different request.",
        "use_new_idempotency_key",
    ),
    "last_owner_required": _spec(409, FIX_REQUEST, "Assign another owner first."),
    "locate_required": _spec(409, FIX_REQUEST, "Run locate_personal_data first."),
    "log_document_append_only": _spec(
        409, FIX_REQUEST, "Use append_entries instead.", "append_entries"
    ),
    "tenant_has_children": _spec(409, FIX_REQUEST, "Fix the request per details."),
    "batch_too_large": _spec(413, FIX_REQUEST, "Split the batch per details."),
    "payload_too_large": _spec(413, FIX_REQUEST, "Send a smaller archive (details.limit_bytes)."),
    "content_too_large": _spec(
        413, FIX_REQUEST, "Split into parts (details.action).", "split_into_parts"
    ),
    "budget_exceeds_parent": _spec(422, FIX_REQUEST, "Lower the budget per details."),
    "checksum_mismatch": _spec(422, FIX_REQUEST, "Download the export again, then import."),
    "context_doc_not_found": _spec(422, FIX_REQUEST, "Fix the context pointers per details."),
    "continuation_required": _spec(422, FIX_REQUEST, "Attach a continuation pack."),
    "duplicate_key_in_batch": _spec(422, FIX_REQUEST, "Deduplicate keys per details."),
    "export_format_unsupported": _spec(422, FIX_REQUEST, "Use a supported export format."),
    "invalid_address": _spec(422, FIX_REQUEST, "Fix the address per details."),
    "invalid_filter": _spec(422, FIX_REQUEST, "Fix the filter per details."),
    "namespace_not_registered": _spec(
        422, FIX_REQUEST, "Call register_namespace, then resend.", "register_namespace"
    ),
    "notice_template_incomplete": _spec(422, FIX_REQUEST, "Fill details.missing fields."),
    "policy_loosening_not_allowed": _spec(
        422, FIX_REQUEST, "Use a value no looser than the ancestor's."
    ),
    "quota_exceeds_parent": _spec(422, FIX_REQUEST, "Stay within the parent's limit."),
    "rollup_summary_required": _spec(422, FIX_REQUEST, "Attach a rollup summary."),
    "subject_identifier_required": _spec(422, FIX_REQUEST, "Fix the request per details."),
    "tenant_cycle": _spec(422, FIX_REQUEST, "Fix the request per details."),
    "tenant_depth_exceeded": _spec(422, FIX_REQUEST, "Fix the request per details."),
    "tenant_type_invalid_parent": _spec(
        422, FIX_REQUEST, "Choose a parent from details.allowed."
    ),
    "ttl_exceeds_parent": _spec(422, FIX_REQUEST, "Lower the TTL per details."),
    "unknown_principal": _spec(422, FIX_REQUEST, "Fix the principal reference per details."),
    "url_not_allowed": _spec(422, FIX_REQUEST, "Use an allowed URL per details."),
    "validation_error": _spec(422, FIX_REQUEST, "Fix the fields listed in details."),
    # --- fix_request (existing 3.2.x codes)
    "invalid_cursor": _spec(400, FIX_REQUEST, "Pass back next_cursor unchanged."),
    "unsupported_artifact_uri": _spec(400, FIX_REQUEST, "Use doc://<doc_key>."),
    "http_error": _spec(400, FIX_REQUEST, "Fix the request method, path or body."),
    "account_exists": _spec(409, FIX_REQUEST, "Sign in instead of registering."),
    "account_link_required": _spec(
        409, FIX_REQUEST, "Sign in with the password, then link Google."
    ),
    "agent_token_exists": _spec(409, FIX_REQUEST, "Use another agent signature."),
    "ambiguous_artifact": _spec(
        422, FIX_REQUEST, "Provide artifact_text or artifact_uri, not both."
    ),
    "artifact_archived": _spec(409, FIX_REQUEST, "Restore the document or use another."),
    "claim_required": _spec(409, FIX_REQUEST, "Claim the task before this transition."),
    "dependency_cycle": _spec(409, FIX_REQUEST, "Remove the cyclic depends_on link."),
    "google_email_mismatch": _spec(409, FIX_REQUEST, "Use the Google account for this email."),
    "google_email_unverified": _spec(401, FIX_REQUEST, "Verify the Google email first."),
    "google_identity_conflict": _spec(409, FIX_REQUEST, "Unlink the other identity first."),
    "human_user_exists": _spec(409, FIX_REQUEST, "Use another email."),
    "insecure_target_url": _spec(422, FIX_REQUEST, "Use an https URL."),
    "invalid_dependency": _spec(422, FIX_REQUEST, "Fix the dependency per details."),
    "invalid_filter_operator": _spec(422, FIX_REQUEST, "Use a supported filter operator."),
    "invalid_filter_value": _spec(422, FIX_REQUEST, "Fix the filter value."),
    "invalid_handoff_brief": _spec(422, FIX_REQUEST, "Fix the L1 brief per details."),
    "invalid_json_patch": _spec(422, FIX_REQUEST, "Fix the JSON Patch operations."),
    "invalid_last_event_id": _spec(422, FIX_REQUEST, "Send a non-negative integer."),
    "invalid_section": _spec(422, FIX_REQUEST, "Use an existing section heading."),
    "invalid_sort": _spec(422, FIX_REQUEST, "Use a supported sort."),
    "invalid_stream_filter": _spec(422, FIX_REQUEST, "Use a supported table filter."),
    "invalid_target_url": _spec(422, FIX_REQUEST, "Fix the target URL."),
    "invalid_task_status": _spec(422, FIX_REQUEST, "Use a valid task status."),
    "invalid_verification_status": _spec(422, FIX_REQUEST, "Use a valid status."),
    "limit_too_large": _spec(422, FIX_REQUEST, "Lower limit to the documented maximum."),
    "offset_not_supported": _spec(422, FIX_REQUEST, "Use cursor pagination."),
    "outbound_host_forbidden": _spec(403, FIX_REQUEST, "Use an allowed outbound host."),
    "outbound_private_address": _spec(403, FIX_REQUEST, "Use a public target address."),
    "policy_exists": _spec(409, FIX_REQUEST, "Update the existing policy instead."),
    "policy_schema_violation": _spec(422, FIX_REQUEST, "Conform to the path JSON Schema."),
    "policy_size_limit": _spec(422, FIX_REQUEST, "Shrink the content below the policy limit."),
    "proposal_required": _spec(
        409, FIX_REQUEST, "Submit propose_document_change instead.", "propose_document_change"
    ),
    "task_exists": _spec(409, FIX_REQUEST, "Use another task_key."),
    "too_many_headers": _spec(422, FIX_REQUEST, "Send at most 64 headers."),
    "tool_error": _spec(400, FIX_REQUEST, "Fix the tool arguments."),
    "unknown_tool": _spec(404, FIX_REQUEST, "Call tools/list and use a listed tool."),
    "workspace_confirmation_mismatch": _spec(409, FIX_REQUEST, "Confirm with the exact name."),
    # --- reauth
    "token_expired": _spec(401, REAUTH, "Renew with the parent token."),
    "token_invalid": _spec(401, REAUTH, "Stop; ask the issuer for a new token."),
    "token_revoked": _spec(401, REAUTH, "Stop all actions; do not retry."),
    "renewal_requires_parent": _spec(403, REAUTH, "Ask the parent token holder to renew."),
    "unauthorized": _spec(401, REAUTH, "Send a valid bearer key."),
    "invalid_api_key": _spec(401, REAUTH, "Configure a valid API key."),
    "invalid_credentials": _spec(401, REAUTH, "Check the email and password."),
    "human_session_required": _spec(401, REAUTH, "Sign in with a human account."),
    "google_audience_mismatch": _spec(401, REAUTH, "Sign in with this deployment's client."),
    "google_identity_mismatch": _spec(401, REAUTH, "Sign in with the linked Google identity."),
    "google_token_invalid": _spec(401, REAUTH, "Obtain a fresh Google credential."),
    # --- abandon
    "depth_limit_exceeded": _spec(403, ABANDON, "Give up this action."),
    "forbidden": _spec(403, ABANDON, "Give up this action."),
    "issue_not_permitted": _spec(403, ABANDON, "Give up this action."),
    "not_assignee": _spec(403, ABANDON, "Give up this action."),
    "not_lease_holder": _spec(403, ABANDON, "Give up; do not preempt."),
    "review_sealed": _spec(403, ABANDON, "Give up this action."),
    "role_mismatch": _spec(403, ABANDON, "Give up this action."),
    "role_not_assignable": _spec(403, ABANDON, "Give up this action."),
    "root_token_data_access_denied": _spec(403, ABANDON, "Give up this action."),
    "scope_escalation": _spec(403, ABANDON, "Give up this action."),
    "NAMESPACE_FORBIDDEN": _spec(403, ABANDON, "Stay within the token's namespaces."),
    "self_vote_forbidden": _spec(403, ABANDON, "Agents cannot vote on their own proposals."),
    "signup_disabled": _spec(403, ABANDON, "Ask an owner for an invitation."),
    "lease_not_found": _spec(404, ABANDON, "Give up this action."),
    "not_found": _spec(404, ABANDON, "Give up; cross-tenant access also returns 404."),
    "already_registered": _spec(409, ABANDON, "Give up this action."),
    "document_archived": _spec(409, ABANDON, "Give up, or ask for restore_document."),
    "id_collision": _spec(409, ABANDON, "Abort the import or import into a new tenant."),
    "increment_out_of_order": _spec(
        409, FIX_REQUEST, "Import details.prev_export_id first, then this increment."
    ),
    "invalid_transition": _spec(409, ABANDON, "Give up this action."),
    "invalid_state_transition": _spec(409, ABANDON, "Re-read the task; give up this action."),
    "lease_lost": _spec(409, ABANDON, "Drop local state and claim again."),
    "lease_conflict": _spec(409, ABANDON, "Another agent holds the lease; do not preempt."),
    "request_closed": _spec(409, ABANDON, "Give up this action."),
    "retention_not_met": _spec(409, ABANDON, "Give up this action."),
    "stale_fencing_token": _spec(409, ABANDON, "Drop local state and claim again."),
    "token_already_expired": _spec(409, ABANDON, "Ask the issuer to issue again."),
    "link_exists": _spec(409, ABANDON, "The link already exists."),
    "proposal_not_pending": _spec(409, ABANDON, "The proposal was already decided."),
    "task_not_reviewable": _spec(409, ABANDON, "The task is not awaiting review."),
    "external_action_error": _spec(409, ABANDON, "Re-read the action state."),
    "gone": _spec(410, ABANDON, "Give up; details carry the tombstone."),
    "reviewer_conflict": _spec(422, ABANDON, "Give up this action."),
    "share_cross_root_not_allowed": _spec(
        422, ABANDON, "Use cross-tree collaboration instead."
    ),
    # --- escalate_human
    "tenant_deleting": _spec(
        403, ESCALATE_HUMAN, "Stop; only an owner can cancel deletion or fetch the export."
    ),
    "tenant_suspended": _spec(
        403, ESCALATE_HUMAN, "Stop writing and notify a human; reads and export still work."
    ),
    "confirmation_required": _spec(
        409, ESCALATE_HUMAN, "Wait for details.pending_confirmations."
    ),
    "incident_not_confirmed": _spec(409, ESCALATE_HUMAN, "Confirm the incident first."),
    "tree_quota_exceeded": _spec(
        409, ESCALATE_HUMAN, "Revoke unused child tokens or ask a human for more quota."
    ),
    "budget_exhausted": _spec(
        429, ESCALATE_HUMAN, "Wait for resets_at or ask the issuer for more budget."
    ),
    "circuit_open": _spec(429, ESCALATE_HUMAN, "Stop the loop; call agent_escalate_to_human."),
    "HUMAN_SOVEREIGNTY_LOCKED": _spec(
        403, ESCALATE_HUMAN, "A human locked this resource; ask a human to change it."
    ),
    "escalated_to_human": _spec(409, ESCALATE_HUMAN, "Wait for the human decision."),
    "taint_blocked": _spec(409, ESCALATE_HUMAN, "A human must verify tainted content."),
    "quota_exceeded": _spec(402, ESCALATE_HUMAN, "Ask a human to raise the quota."),
    "budget_exceeded": _spec(402, ESCALATE_HUMAN, "Ask a human to raise the budget."),
    "admin_disabled": _spec(503, ESCALATE_HUMAN, "ADMIN_TOKEN is not configured."),
    "mcp_not_configured": _spec(500, ESCALATE_HUMAN, "An operator must set MCP_API_KEY."),
    "google_login_not_configured": _spec(
        503, ESCALATE_HUMAN, "An operator must configure Google sign-in."
    ),
}


# Soft-constraint warnings (PRD §6 warnings[]): never errors, never rejections.
WARNING_CODES: dict[str, str] = {
    "secrets_not_imported": "Imported tokens must be rotated and users must reset passwords.",
    "alias_redirected": "Read through a renamed alias; use the current alias or id.",
    "legacy_alias": "Address predates the grammar; accepted, but 2026-11 rejects new ones.",
    "dangling_ref": "A $ref or part does not resolve; stored as written.",
    "namespace_auto_registered": "Namespace registered implicitly; 2026-11 requires it first.",
    "namespace_undocumented": "The namespace readme is still a generated stub.",
    "frontmatter_missing": "Fields the namespace readme requires are missing.",
    "doc_key_pattern_mismatch": "doc_key does not match the namespace pattern.",
    "not_registered": "The collaborator directory is not live; identity is inferred.",
    "constitution_draft": "The constitution is still the generated template.",
    "token_expiring": "The calling token expires within a day.",
    "quota_80_percent": "The daily embedding quota is at least 80% used.",
    "embedding_deferred_quota": "Stored, but indexing waits for the daily embedding quota reset.",
}


def _fallback(status: int) -> ErrorSpec:
    if status in (429, 503):
        return _spec(status, RETRY_AFTER, "Wait for Retry-After.")
    if status >= 500:
        return _spec(status, RETRY_BACKOFF, "Back off and retry.")
    if status == 401:
        return _spec(status, REAUTH, "Re-authenticate.")
    if status in (403, 404):
        return _spec(status, ABANDON, "Give up this action.")
    return _spec(status, FIX_REQUEST, "Fix the request.")


def spec_for(code: str, status: int) -> ErrorSpec:
    return REGISTRY.get(code) or _fallback(status)


def retry_after_ms(headers: dict[str, str] | None) -> int | None:
    if not headers:
        return None
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                return max(int(float(value) * 1000), 0)
            except (TypeError, ValueError):
                return None
    return None


def classify(
    code: str,
    status: int,
    *,
    headers: dict[str, str] | None = None,
    details: Any | None = None,
) -> dict[str, Any]:
    """Return the protocol fields to merge into an error object."""
    spec = spec_for(code, status)
    delay = retry_after_ms(headers)
    if delay is None and isinstance(details, dict):
        raw = details.get("retry_after_ms")
        delay = int(raw) if isinstance(raw, (int, float)) else None
    return {
        "class": spec.error_class,
        "action": spec.machine_action,
        "retryable": spec.retryable,
        "retry_after_ms": delay,
    }


def registry_table() -> list[dict[str, Any]]:
    return [
        {
            "code": code,
            "http": spec.status,
            "class": spec.error_class,
            "action": spec.machine_action,
            "retryable": spec.retryable,
            "rule": spec.rule,
        }
        for code, spec in sorted(
            REGISTRY.items(), key=lambda item: (list(ERROR_CLASSES).index(item[1].error_class),
                                                item[1].status, item[0])
        )
    ]


def protocol_docs(section: str) -> dict[str, Any]:
    """Agent-readable protocol reference shared by REST and MCP docs."""
    from app import versioning

    versions = {
        "header": versioning.VERSION_HEADER,
        "default": versioning.BASELINE_VERSION,
        "latest": versioning.LATEST_VERSION,
        "supported": list(versioning.SUPPORTED_VERSIONS),
        "rule": "Undeclared requests run on the baseline. Within a version only "
        "additive changes ship; ignore unknown fields.",
    }
    idempotency = {
        "rest": "Send header Idempotency-Key: <key> (<=128 chars) on POST/PUT/PATCH/DELETE.",
        "mcp": "Pass idempotency_key to write tools.",
        "window_hours": 24,
        "replay": "Same key + same body replays the first response with replayed=true.",
        "conflicts": ["idempotency_key_reused", "idempotency_in_progress"],
    }
    if section == "retry":
        return {"retry": RETRY_POLICY, "classes": ERROR_CLASSES, "idempotency": idempotency}
    if section == "errors":
        return {"classes": ERROR_CLASSES, "codes": registry_table()}
    if section == "versions":
        return {"versions": versions}
    return {
        "error_shape": {
            "error": {
                "code": "snake_case",
                "message": "string",
                "class": list(ERROR_CLASSES),
                "action": "machine action",
                "retryable": "bool",
                "retry_after_ms": "int|null",
                "details": "optional",
            }
        },
        "classes": {name: meta["rule"] for name, meta in ERROR_CLASSES.items()},
        "retry": RETRY_POLICY,
        "idempotency": idempotency,
        "versions": versions,
    }
