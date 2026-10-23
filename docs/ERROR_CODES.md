# Veynod error codes

Generated from `app/error_registry.py` by `scripts/gen_error_codes.py`;
do not edit by hand. Every error body is
`{"error": {"code", "message", "class", "action", "retryable",
"retry_after_ms", "details"}}`.

## Classes

| class | action | retryable | rule |
|---|---|---|---|
| `retry_after` | `wait_retry_after_then_retry_same_request` | true | Wait for Retry-After, then resend unchanged with the same idempotency key. |
| `retry_backoff` | `backoff_then_retry_same_request` | true | Retry with full-jitter exponential backoff, at most 5 times, with the same idempotency key. |
| `refresh_and_retry` | `refresh_state_then_retry_once` | true | Merge or resync using the current state in details (current_version etc.), then retry once on that new state. |
| `fix_request` | `fix_request_then_resend` | false | Never resend unchanged; correct the request using details first. |
| `reauth` | `stop_and_reauthenticate` | false | Stop; ask the parent token holder or a human for a valid credential. |
| `abandon` | `abandon_and_release_local_state` | false | Give up this action, release local state (e.g. leases) and re-read global state if needed. |
| `escalate_human` | `stop_and_escalate_to_human` | false | Stop automatic retries; call agent_escalate_to_human or wait for a human. |

## Retry algorithm

`sleep_ms = max(retry_after_ms, random(0, min(cap_ms, base_ms * 2**attempt)))` with base 500 ms, cap 30000 ms, at most 5 attempts. Resend with the same idempotency_key on every retry. Events are delivered at least once; deduplicate by event_id.

## Codes

| code | HTTP | class | action | rule |
|---|---|---|---|---|
| `backup_in_progress` | 409 | `retry_after` | `wait_retry_after_then_retry_same_request` | Another backup is running; retry later. |
| `contention` | 409 | `retry_after` | `wait_retry_after_then_retry_same_request` | Wait for Retry-After, then retry. |
| `export_in_progress` | 409 | `retry_after` | `wait_retry_after_then_retry_same_request` | Poll details.export_id instead of starting another export. |
| `idempotency_in_progress` | 409 | `retry_after` | `wait_retry_after_then_retry_same_request` | Wait for Retry-After and query the result with the same key. |
| `embedding_budget_exceeded` | 429 | `retry_after` | `wait_retry_after_then_retry_same_request` | Embedding byte budget for this minute is spent; wait. |
| `rate_limited` | 429 | `retry_after` | `wait_retry_after_then_retry_same_request` | Wait for Retry-After. |
| `sse_capacity` | 429 | `retry_after` | `wait_retry_after_then_retry_same_request` | Too many event streams; reconnect later. |
| `overloaded` | 503 | `retry_after` | `wait_retry_after_then_retry_same_request` | Wait for Retry-After; never resend earlier. |
| `internal_error` | 500 | `retry_backoff` | `backoff_then_retry_same_request` | Back off and retry at most 5 times, then escalate_human. |
| `embedding_failed` | 502 | `retry_backoff` | `backoff_then_retry_same_request` | Embedding provider failed; back off. |
| `cli_code_unavailable` | 503 | `retry_backoff` | `backoff_then_retry_same_request` | Back off and retry. |
| `database_unavailable` | 503 | `retry_backoff` | `backoff_then_retry_same_request` | Back off and retry. |
| `google_unavailable` | 503 | `retry_backoff` | `backoff_then_retry_same_request` | Back off and retry sign-in. |
| `tenant_context_unavailable` | 503 | `retry_backoff` | `backoff_then_retry_same_request` | Back off and retry. |
| `proposal_conflict` | 409 | `refresh_and_retry` | `refresh_state_then_retry_once` | Re-read the document and rebase the proposal. |
| `rebase_conflict` | 409 | `refresh_and_retry` | `refresh_state_then_retry_once` | Re-read from base_version and redo non-overlapping edits. |
| `section_conflict` | 409 | `refresh_and_retry` | `refresh_state_then_retry_once` | Re-read the section, then rewrite. |
| `undo_conflict` | 409 | `refresh_and_retry` | `refresh_state_then_retry_once` | Review details.conflicts; retry with force=true if intended. |
| `version_conflict` | 409 | `refresh_and_retry` | `refresh_state_then_retry_once` | Merge against details.current_version, then rewrite. |
| `cursor_expired` | 410 | `refresh_and_retry` | `refresh_state_then_retry_once` | Drop the cursor and resync with workspace_sync. |
| `cursor_invalid` | 400 | `fix_request` | `fix_request_then_resend` | Fix the cursor per details. |
| `http_error` | 400 | `fix_request` | `fix_request_then_resend` | Fix the request method, path or body. |
| `invalid_cursor` | 400 | `fix_request` | `fix_request_then_resend` | Pass back next_cursor unchanged. |
| `tenant_id_not_allowed` | 400 | `fix_request` | `remove_tenant_id` | Remove tenant_id from the request. |
| `tool_error` | 400 | `fix_request` | `fix_request_then_resend` | Fix the tool arguments. |
| `unsupported_api_version` | 400 | `fix_request` | `use_supported_api_version` | Use a version from details.supported. |
| `unsupported_artifact_uri` | 400 | `fix_request` | `fix_request_then_resend` | Use doc://<doc_key>. |
| `google_email_unverified` | 401 | `fix_request` | `fix_request_then_resend` | Verify the Google email first. |
| `field_not_writable` | 403 | `fix_request` | `fix_request_then_resend` | Remove the field from the request. |
| `outbound_host_forbidden` | 403 | `fix_request` | `fix_request_then_resend` | Use an allowed outbound host. |
| `outbound_private_address` | 403 | `fix_request` | `fix_request_then_resend` | Use a public target address. |
| `system_namespace_protected` | 403 | `fix_request` | `propose_document_change` | Use propose_document_change instead. |
| `unknown_tool` | 404 | `fix_request` | `fix_request_then_resend` | Call tools/list and use a listed tool. |
| `account_exists` | 409 | `fix_request` | `fix_request_then_resend` | Sign in instead of registering. |
| `account_link_required` | 409 | `fix_request` | `fix_request_then_resend` | Sign in with the password, then link Google. |
| `admin_access_required` | 409 | `fix_request` | `fix_request_then_resend` | Call open_admin_access first (the node is notified). |
| `agent_id_taken` | 409 | `fix_request` | `fix_request_then_resend` | Choose another agent_id. |
| `agent_token_exists` | 409 | `fix_request` | `fix_request_then_resend` | Use another agent signature. |
| `alias_taken` | 409 | `fix_request` | `fix_request_then_resend` | Choose another alias. |
| `alias_tombstoned` | 409 | `fix_request` | `fix_request_then_resend` | Choose another alias; tombstones never reuse. |
| `artifact_archived` | 409 | `fix_request` | `fix_request_then_resend` | Restore the document or use another. |
| `auto_release_cap_reached` | 409 | `fix_request` | `fix_request_then_resend` | Hand off to REVIEWING; an audit releases it today. |
| `claim_required` | 409 | `fix_request` | `fix_request_then_resend` | Claim the task before this transition. |
| `dependency_cycle` | 409 | `fix_request` | `fix_request_then_resend` | Remove the cyclic depends_on link. |
| `google_email_mismatch` | 409 | `fix_request` | `fix_request_then_resend` | Use the Google account for this email. |
| `google_identity_conflict` | 409 | `fix_request` | `fix_request_then_resend` | Unlink the other identity first. |
| `human_user_exists` | 409 | `fix_request` | `fix_request_then_resend` | Use another email. |
| `idempotency_key_reused` | 409 | `fix_request` | `use_new_idempotency_key` | Generate a new idempotency key; confirm this is a different request. |
| `increment_out_of_order` | 409 | `fix_request` | `fix_request_then_resend` | Import details.prev_export_id first, then this increment. |
| `last_owner_required` | 409 | `fix_request` | `fix_request_then_resend` | Assign another owner first. |
| `locate_required` | 409 | `fix_request` | `fix_request_then_resend` | Run locate_personal_data first. |
| `log_document_append_only` | 409 | `fix_request` | `append_entries` | Use append_entries instead. |
| `not_a_log_document` | 409 | `fix_request` | `fix_request_then_resend` | Write with upsert_shared_doc instead. |
| `policy_exists` | 409 | `fix_request` | `fix_request_then_resend` | Update the existing policy instead. |
| `proposal_required` | 409 | `fix_request` | `propose_document_change` | Submit propose_document_change instead. |
| `quality_circuit_open` | 409 | `fix_request` | `fix_request_then_resend` | The domain's circuit is open: hand off to REVIEWING. |
| `quality_gates_unmet` | 409 | `fix_request` | `fix_request_then_resend` | Report the gates in details.missing as PASS, then retry. |
| `task_exists` | 409 | `fix_request` | `fix_request_then_resend` | Use another task_key. |
| `tenant_has_children` | 409 | `fix_request` | `fix_request_then_resend` | Fix the request per details. |
| `workspace_confirmation_mismatch` | 409 | `fix_request` | `fix_request_then_resend` | Confirm with the exact name. |
| `batch_too_large` | 413 | `fix_request` | `fix_request_then_resend` | Split the batch per details. |
| `content_too_large` | 413 | `fix_request` | `split_into_parts` | Split into parts (details.action). |
| `payload_too_large` | 413 | `fix_request` | `fix_request_then_resend` | Send a smaller archive (details.limit_bytes). |
| `acceptance_items_incomplete` | 422 | `fix_request` | `fix_request_then_resend` | Give a PASS result for every acceptance item. |
| `ambiguous_artifact` | 422 | `fix_request` | `fix_request_then_resend` | Provide artifact_text or artifact_uri, not both. |
| `audit_view_required` | 422 | `fix_request` | `fix_request_then_resend` | Call get_audit_view, then resend. |
| `budget_exceeds_parent` | 422 | `fix_request` | `fix_request_then_resend` | Lower the budget per details. |
| `checksum_mismatch` | 422 | `fix_request` | `fix_request_then_resend` | Download the export again, then import. |
| `context_doc_not_found` | 422 | `fix_request` | `fix_request_then_resend` | Fix the context pointers per details. |
| `continuation_required` | 422 | `fix_request` | `fix_request_then_resend` | Attach a continuation pack. |
| `duplicate_key_in_batch` | 422 | `fix_request` | `fix_request_then_resend` | Deduplicate keys per details. |
| `export_format_unsupported` | 422 | `fix_request` | `fix_request_then_resend` | Use a supported export format. |
| `insecure_target_url` | 422 | `fix_request` | `fix_request_then_resend` | Use an https URL. |
| `invalid_address` | 422 | `fix_request` | `fix_request_then_resend` | Fix the address per details. |
| `invalid_dependency` | 422 | `fix_request` | `fix_request_then_resend` | Fix the dependency per details. |
| `invalid_filter` | 422 | `fix_request` | `fix_request_then_resend` | Fix the filter per details. |
| `invalid_filter_operator` | 422 | `fix_request` | `fix_request_then_resend` | Use a supported filter operator. |
| `invalid_filter_value` | 422 | `fix_request` | `fix_request_then_resend` | Fix the filter value. |
| `invalid_handoff_brief` | 422 | `fix_request` | `fix_request_then_resend` | Fix the L1 brief per details. |
| `invalid_json_patch` | 422 | `fix_request` | `fix_request_then_resend` | Fix the JSON Patch operations. |
| `invalid_last_event_id` | 422 | `fix_request` | `fix_request_then_resend` | Send a non-negative integer. |
| `invalid_public_key` | 422 | `fix_request` | `fix_request_then_resend` | Send a base64 X25519 public key (32 bytes). |
| `invalid_section` | 422 | `fix_request` | `fix_request_then_resend` | Use an existing section heading. |
| `invalid_sort` | 422 | `fix_request` | `fix_request_then_resend` | Use a supported sort. |
| `invalid_stream_filter` | 422 | `fix_request` | `fix_request_then_resend` | Use a supported table filter. |
| `invalid_target_url` | 422 | `fix_request` | `fix_request_then_resend` | Fix the target URL. |
| `invalid_task_status` | 422 | `fix_request` | `fix_request_then_resend` | Use a valid task status. |
| `invalid_verification_status` | 422 | `fix_request` | `fix_request_then_resend` | Use a valid status. |
| `limit_too_large` | 422 | `fix_request` | `fix_request_then_resend` | Lower limit to the documented maximum. |
| `namespace_not_registered` | 422 | `fix_request` | `register_namespace` | Call register_namespace, then resend. |
| `notice_template_incomplete` | 422 | `fix_request` | `fix_request_then_resend` | Fill details.missing fields. |
| `offset_not_supported` | 422 | `fix_request` | `fix_request_then_resend` | Use cursor pagination. |
| `policy_loosening_not_allowed` | 422 | `fix_request` | `fix_request_then_resend` | Use a value no looser than the ancestor's. |
| `policy_schema_violation` | 422 | `fix_request` | `fix_request_then_resend` | Conform to the path JSON Schema. |
| `policy_size_limit` | 422 | `fix_request` | `fix_request_then_resend` | Shrink the content below the policy limit. |
| `quota_exceeds_parent` | 422 | `fix_request` | `fix_request_then_resend` | Stay within the parent's limit. |
| `review_feedback_required` | 422 | `fix_request` | `fix_request_then_resend` | Say why in feedback, then resend. |
| `review_note_required` | 422 | `fix_request` | `fix_request_then_resend` | Say why in note, then resend. |
| `rollup_summary_required` | 422 | `fix_request` | `fix_request_then_resend` | Attach a rollup summary. |
| `scope_unknown` | 422 | `fix_request` | `fix_request_then_resend` | Use permissions listed in details.allowed. |
| `subject_identifier_required` | 422 | `fix_request` | `fix_request_then_resend` | Fix the request per details. |
| `task_tree_too_deep` | 422 | `fix_request` | `fix_request_then_resend` | Split under a shallower task. |
| `tenant_cycle` | 422 | `fix_request` | `fix_request_then_resend` | Fix the request per details. |
| `tenant_depth_exceeded` | 422 | `fix_request` | `fix_request_then_resend` | Fix the request per details. |
| `tenant_type_invalid_parent` | 422 | `fix_request` | `fix_request_then_resend` | Choose a parent from details.allowed. |
| `too_many_headers` | 422 | `fix_request` | `fix_request_then_resend` | Send at most 64 headers. |
| `ttl_exceeds_parent` | 422 | `fix_request` | `fix_request_then_resend` | Lower the TTL per details. |
| `unknown_principal` | 422 | `fix_request` | `fix_request_then_resend` | Fix the principal reference per details. |
| `url_not_allowed` | 422 | `fix_request` | `fix_request_then_resend` | Use an allowed URL per details. |
| `validation_error` | 422 | `fix_request` | `fix_request_then_resend` | Fix the fields listed in details. |
| `google_audience_mismatch` | 401 | `reauth` | `stop_and_reauthenticate` | Sign in with this deployment's client. |
| `google_identity_mismatch` | 401 | `reauth` | `stop_and_reauthenticate` | Sign in with the linked Google identity. |
| `google_token_invalid` | 401 | `reauth` | `stop_and_reauthenticate` | Obtain a fresh Google credential. |
| `human_session_required` | 401 | `reauth` | `stop_and_reauthenticate` | Sign in with a human account. |
| `invalid_api_key` | 401 | `reauth` | `stop_and_reauthenticate` | Configure a valid API key. |
| `invalid_credentials` | 401 | `reauth` | `stop_and_reauthenticate` | Check the email and password. |
| `token_expired` | 401 | `reauth` | `stop_and_reauthenticate` | Renew with the parent token. |
| `token_invalid` | 401 | `reauth` | `stop_and_reauthenticate` | Stop; ask the issuer for a new token. |
| `token_revoked` | 401 | `reauth` | `stop_and_reauthenticate` | Stop all actions; do not retry. |
| `unauthorized` | 401 | `reauth` | `stop_and_reauthenticate` | Send a valid bearer key. |
| `renewal_requires_parent` | 403 | `reauth` | `stop_and_reauthenticate` | Ask the parent token holder to renew. |
| `NAMESPACE_FORBIDDEN` | 403 | `abandon` | `abandon_and_release_local_state` | Stay within the token's namespaces. |
| `acceptance_locked` | 403 | `abandon` | `abandon_and_release_local_state` | Acceptance is locked after the first claim; ask the task's creator or a person. |
| `acceptance_self_report` | 403 | `abandon` | `abandon_and_release_local_state` | The executor cannot report its own acceptance; another agent or a person must. |
| `depth_limit_exceeded` | 403 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `forbidden` | 403 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `issue_not_permitted` | 403 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `not_assignee` | 403 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `not_lease_holder` | 403 | `abandon` | `abandon_and_release_local_state` | Give up; do not preempt. |
| `review_sealed` | 403 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `role_mismatch` | 403 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `role_not_assignable` | 403 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `root_token_data_access_denied` | 403 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `scope_escalation` | 403 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `self_vote_forbidden` | 403 | `abandon` | `abandon_and_release_local_state` | Agents cannot vote on their own proposals. |
| `signup_disabled` | 403 | `abandon` | `abandon_and_release_local_state` | Ask an owner for an invitation. |
| `invalid_device_code` | 404 | `abandon` | `abandon_and_release_local_state` | Start a new setup with `npx veynod init`. |
| `lease_not_found` | 404 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `not_found` | 404 | `abandon` | `abandon_and_release_local_state` | Give up; cross-tenant access also returns 404. |
| `already_registered` | 409 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `circuit_not_open` | 409 | `abandon` | `abandon_and_release_local_state` | The domain's circuit is already closed. |
| `claim_already_refuted` | 409 | `abandon` | `abandon_and_release_local_state` | The claim was refuted; report a new commit. |
| `cli_session_decided` | 409 | `abandon` | `abandon_and_release_local_state` | Run `npx veynod init` again for a new code. |
| `document_archived` | 409 | `abandon` | `abandon_and_release_local_state` | Give up, or ask for restore_document. |
| `escape_closed` | 409 | `abandon` | `abandon_and_release_local_state` | The escape is already FIXED or WAIVED. |
| `external_action_error` | 409 | `abandon` | `abandon_and_release_local_state` | Re-read the action state. |
| `id_collision` | 409 | `abandon` | `abandon_and_release_local_state` | Abort the import or import into a new tenant. |
| `invalid_state_transition` | 409 | `abandon` | `abandon_and_release_local_state` | Re-read the task; give up this action. |
| `invalid_transition` | 409 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `lease_conflict` | 409 | `abandon` | `abandon_and_release_local_state` | Another agent holds the lease; do not preempt. |
| `lease_lost` | 409 | `abandon` | `abandon_and_release_local_state` | Drop local state and claim again. |
| `lineage_unavailable` | 409 | `abandon` | `abandon_and_release_local_state` | This subject has no signed lineage; give up. |
| `link_exists` | 409 | `abandon` | `abandon_and_release_local_state` | The link already exists. |
| `nothing_to_probe` | 409 | `abandon` | `abandon_and_release_local_state` | Write and index documents first. |
| `proposal_not_pending` | 409 | `abandon` | `abandon_and_release_local_state` | The proposal was already decided. |
| `queue_ticket_expired` | 409 | `abandon` | `abandon_and_release_local_state` | Call acquire_resource again to re-queue. |
| `request_closed` | 409 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `retention_not_met` | 409 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `sample_unavailable` | 409 | `abandon` | `abandon_and_release_local_state` | Someone else took the sample or it was already judged. |
| `stale_fencing_token` | 409 | `abandon` | `abandon_and_release_local_state` | Drop local state and claim again. |
| `task_not_reviewable` | 409 | `abandon` | `abandon_and_release_local_state` | The task is not awaiting review. |
| `token_already_expired` | 409 | `abandon` | `abandon_and_release_local_state` | Ask the issuer to issue again. |
| `cli_session_consumed` | 410 | `abandon` | `abandon_and_release_local_state` | The key was collected once; start a new setup. |
| `cli_session_expired` | 410 | `abandon` | `abandon_and_release_local_state` | Run `npx veynod init` again for a new code. |
| `gone` | 410 | `abandon` | `abandon_and_release_local_state` | Give up; details carry the tombstone. |
| `reviewer_conflict` | 422 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `share_cross_root_not_allowed` | 422 | `abandon` | `abandon_and_release_local_state` | Use cross-tree collaboration instead. |
| `budget_exceeded` | 402 | `escalate_human` | `stop_and_escalate_to_human` | Ask a human to raise the budget. |
| `quota_exceeded` | 402 | `escalate_human` | `stop_and_escalate_to_human` | Ask a human to raise the quota. |
| `HUMAN_SOVEREIGNTY_LOCKED` | 403 | `escalate_human` | `stop_and_escalate_to_human` | A human locked this resource; ask a human to change it. |
| `feature_disabled` | 403 | `escalate_human` | `stop_and_escalate_to_human` | Switched off here; ask an operator (details.setting). |
| `tenant_deleting` | 403 | `escalate_human` | `stop_and_escalate_to_human` | Stop; only an owner can cancel deletion or fetch the export. |
| `tenant_suspended` | 403 | `escalate_human` | `stop_and_escalate_to_human` | Stop writing and notify a human; reads and export still work. |
| `backup_chain_broken` | 409 | `escalate_human` | `stop_and_escalate_to_human` | Restore from an older full backup or take a new one. |
| `backups_not_configured` | 409 | `escalate_human` | `stop_and_escalate_to_human` | An operator must configure backup storage. |
| `confirmation_required` | 409 | `escalate_human` | `stop_and_escalate_to_human` | Wait for details.pending_confirmations. |
| `escalated_to_human` | 409 | `escalate_human` | `stop_and_escalate_to_human` | Wait for the human decision. |
| `incident_not_confirmed` | 409 | `escalate_human` | `stop_and_escalate_to_human` | Confirm the incident first. |
| `staging_not_configured` | 409 | `escalate_human` | `stop_and_escalate_to_human` | An operator must configure a separate staging database. |
| `taint_blocked` | 409 | `escalate_human` | `stop_and_escalate_to_human` | A human must verify tainted content. |
| `tree_quota_exceeded` | 409 | `escalate_human` | `stop_and_escalate_to_human` | Revoke unused child tokens or ask a human for more quota. |
| `budget_exhausted` | 429 | `escalate_human` | `stop_and_escalate_to_human` | Wait for resets_at or ask the issuer for more budget. |
| `circuit_open` | 429 | `escalate_human` | `stop_and_escalate_to_human` | Stop the loop; call agent_escalate_to_human. |
| `mcp_not_configured` | 500 | `escalate_human` | `stop_and_escalate_to_human` | An operator must set MCP_API_KEY. |
| `admin_disabled` | 503 | `escalate_human` | `stop_and_escalate_to_human` | ADMIN_TOKEN is not configured. |
| `google_login_not_configured` | 503 | `escalate_human` | `stop_and_escalate_to_human` | An operator must configure Google sign-in. |

## Warnings

Soft constraints reported in `warnings[]`; the request still succeeds.

| code | meaning |
|---|---|
| `promotion_below_threshold` | The domain was promoted below the promotion threshold. |
| `gate_source_downgraded` | Only a trusted runner reports RUNNER results; recorded as AGENT_CLAIM. |
| `secrets_not_imported` | Imported tokens must be rotated and users must reset passwords. |
| `keys_rotated` | A restored workspace has new keys; rotate them and re-issue agent tokens. |
| `alias_redirected` | Read through a renamed alias; use the current alias or id. |
| `legacy_alias` | Address predates the grammar; accepted, but 2026-11 rejects new ones. |
| `dangling_ref` | A $ref or part does not resolve; stored as written. |
| `namespace_auto_registered` | Namespace registered implicitly; 2026-11 requires it first. |
| `namespace_unchanged` | The new key's prefix is not a registered namespace; the document stayed where it was. Register the namespace and rename again to move it. |
| `namespace_undocumented` | The namespace readme is still a generated stub. |
| `frontmatter_missing` | Fields the namespace readme requires are missing. |
| `doc_key_pattern_mismatch` | doc_key does not match the namespace pattern. |
| `budget_warning` | A token's daily budget is at 80%; budget.warning went to its issuer. |
| `recall_still_depends` | This version still derives from recalled versions; the recall node stays open. |
| `ttl_capped_by_policy` | The token's lifetime was capped by an organisation policy. |
| `continuation_missing` | No continuation pack was written since the claim; call write_continuation before the next handoff. |
| `output_missing` | Nothing was written to the task's output_to since the claim. |
| `target_offline` | A mentioned agent is offline or retired; its owner is notified too. |
| `unknown_mention` | A mentioned principal is not in the directory. |
| `not_registered` | The collaborator directory is not live; identity is inferred. |
| `constitution_draft` | The constitution is still the generated template. |
| `token_expiring` | The calling token expires within a day. |
| `quota_80_percent` | The daily embedding quota is at least 80% used. |
| `embedding_deferred_quota` | Stored, but indexing waits for the daily embedding quota reset. |
