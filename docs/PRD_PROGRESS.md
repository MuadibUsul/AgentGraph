# PRD v0.3 implementation progress

Tracks the requirements of the 2026-10-21 PRD (58 requirements). "Done" means
implemented with acceptance tests in `tests/`; deviations are listed.

## Phase 0 (complete)

| ID | Status | Notes |
|---|---|---|
| AG-ST-18 failure-semantics protocol | Done | Error classes/actions on every error (`docs/ERROR_CODES.md`, CI-checked); idempotency 24 h (REST header / MCP arg); `AgentGraph-Version` negotiation; `event_id`/`seq` on events. |
| AG-ID-07 tenant dimension | Done | Tenant tree fields + DB constraints, server-derived `tenant_id` on every table, RLS on tenant scope, memberships, suspend, two-party deletion with 30-day grace + tombstone. **Deviation:** each existing workspace becomes its own root tenant instead of one `ten_default` (self-service signup means they are different customers). Final export on deletion lands with AG-ST-02. |
| AG-ST-17 address spec | Done | Canonical ids (`doc_`, `task_`, `ws_`, `tok_`, `prn_`, `slot_`) exposed as `canonical_id`; redirects; tombstones; namespace registry; `$ref` resolution; parts. Grammar and register-before-use are enforced under API version `2026-11`; the baseline warns (existing namespaces such as `teamA` break the grammar). |
| AG-ID-03 lineage | Done | Stamps on every write via `app.lineage_stamp` + triggers; immutable grant snapshots; `explain_write`, `get_token`, `get_lineage`, `list_token_tree`, `written_by_subtree`. |
| AG-CO-01 review_task | Done | Shared enum/validator, IN_PROGRESS rework, review rounds/history. |
| AG-ST-03 large content | Done | 1 MiB bytes, byte-exact storage, ranges, matched_chunks, register_parts. |
| AG-ST-04 query defaults | Done | Own drafts visible by default (already true in 3.2.1), `include_own_unverified`, `is_own`, `excluded`. |
| AG-ST-05 conflict details | Done | `current_version`, hash, `updated_by(_type)`, optional `current_content`. |
| AG-OB-01 orientation | Done (degraded) | Collaborators inferred until AG-ID-06; `next_calls` verified to run without errors. |
| AG-OB-02 constitution + namespace map | Done | `system/*` platform namespace, readme stubs, soft/strict conventions, backfill. |
| AG-OB-03 agent docs | Done | Generated guide, token-readable OpenAPI, MCP resources. |
| AG-ST-15 cost control | Done (enforce available) | Shadow by default (`BUDGET_MODE=shadow`); `enforce` adds 429 `rate_limited` (writes 60/min burst 20, queries 120/min), server-enforced backoff (early retry refused without taking a token, wait doubles to 60 s, 3 in a row → 1 min `circuit_open` `ignored_backoff`), ≤4 in-flight requests and 1 stream per token, `circuit_open` for `hot_key_loop` and `write_spike` (10× own 7-day p99), and quota exhaustion that stores the write but defers indexing (`embedding_deferred_quota`) until the daily reset. Admin overrides per workspace or tenant (`/v1/admin/budget-limits/...`; tenant rows cap the subtree's summed embedding). **Gaps:** the concurrency count is per process; the dashboard has no circuit panel yet (the owner API `POST /v1/human/budget/circuits/{key}/clear` exists). |

## Phase 1 (in progress)

| ID | Status | Notes |
|---|---|---|
| AG-ST-13 async index | Done | Under `2026-11` writes return `stored=true`, `index_state=pending`; a background worker embeds only the latest queued version (coalescing, `superseded` count), emits `DOC_INDEXED`, retries with exponential backoff and ends as `failed` + `DOC_INDEX_FAILED`. `GET /v1/index/status` / `get_index_status`; queries carry `index_lag`. Baseline clients keep synchronous indexing. |
| AG-ST-06 write semantics | Done | Identical writes are a `noop` (no version bump); connection-pool exhaustion returns 503 `overloaded` with `Retry-After` instead of a 500. |

Not started yet: R1.1 (ST-15 enforce, CO-03, ST-01, ST-02),
R1.2 (EV-01, EV-02), R1.3 (ST-10, TC-06), R1.4 (ID-01/02/04/05/06/10, TC-07,
OB-01 full), Phase 2 and Phase 3.
