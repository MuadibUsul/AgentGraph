# AgentGraph v3.0 — Canonical Requirements

**Product:** Human-in-the-Loop Shared Blackboard Workspace for multi-agent teams.
**Status:** consolidated from the v3.0 refactor proposal, the six-mechanism brief,
and the production DDL. Where the source documents disagree, this file is the
single source of truth.

---

## 1. Goal

Evolve AgentGraph from a memory/decay database into a collaborative blackboard
that agents and humans can safely share:

- agents coordinate asynchronously through tasks, leases, and shared state;
- context is handed off in progressive L0/L1/L2 layers instead of raw logs;
- runaway agent ping-pong is stopped by a database-backed circuit breaker;
- humans can freeze state, approve work, interrupt agents, and undo an agent's
  entire session batch;
- every agent has a scoped token instead of sharing the commander key.

Preserved from v2.0: workspaces + API-key tenancy, FastAPI, PostgreSQL 16 +
pgvector, MCP, Next.js operator console, timeline auditing.

Removed from v2.0: Ebbinghaus/salience scoring, `context_pull` ranking, the
crystal/causal primitives. Vector search remains available on demand inside
`query_workspace`.

## 1.1 v3.1 production hardening (implemented after the initial v3.0 build)

The following correctness items were added in v3.1.0 and are part of the current
implementation contract:

- Immutable `document_versions` with SHA-256 content hashes, parent version,
  author and session batch.
- `verification_status` (`DRAFT / PENDING_REVIEW / VERIFIED / DEPRECATED`),
  hash-bound `verified_version` / `verified_hash`, automatic invalidation on
  content change, and verified-only semantic search by default.
- `trust_level` with UNTRUSTED-source delimiters in the agent-facing document
  view.
- Transactional `workspace_events` outbox, monotonic SSE ids, `Last-Event-ID`
  replay, and one LISTEN connection per process.
- Monotonic `lease_epoch` fencing plus database-clock lease checks in
  claim/renew/handoff/reaper paths.
- Conflict-aware batch undo: dry-run in reverse, compare against after-images,
  409 `undo_conflict` on drift, explicit `force=true` override, and
  append-only event warnings.
- Dependency-cycle detection for `depends_on` links; advisory-locked schema
  migrations; embedding model recorded per document.

## 1.2 v3.2 production expansion (implemented)

v3.2.0/v3.2.1 close the production gaps that were previously listed as future work:

- **Proposals and Proposal Arena** — `document_proposals` / `proposal_votes`,
  human apply/reject, vote ranking, `PENDING / APPLIED / REJECTED / SUPERSEDED`.
- **Automatic non-overlapping rebase** — three-way document merge and section
  rebase; slots rebase only when the touched JSON Pointer values are unchanged
  since the audited base version. Overlaps return 409 with conflict details.
- **Path policy / JSON Schema** — glob-scoped document/slot/task policies,
  `require_proposal`, role allowlists, content-size caps and Draft 2020-12
  frontmatter/state validation.
- **Full taint propagation** — `taint_edges` from source documents to derived
  documents, slots and tasks; default query exclusion; agent verification
  blocked; human verification clears the root and recomputes downstream.
- **Human accounts / RLS** — scrypt password accounts, expiring sessions,
  owner/reviewer/viewer roles, `/v1/auth/*`, `/v1/human/*`, an
  `agentgraph_app` non-superuser role and FORCE ROW LEVEL SECURITY keyed by
  `app.workspace_id`.
- **Typed link FKs** — typed source/target columns and real foreign keys with
  cascade cleanup and a BEFORE INSERT/UPDATE trigger for automatic typing.
- **Chunked/keyword hybrid retrieval** — `document_chunks`, HNSW,
  generated tsvector, pg_trgm, RRF fusion, semantic/keyword/hybrid/auto modes,
  verified/taint filters and administrative reindex.
- **External-action outbox/compensation** — human approval, idempotency keys,
  attempts ledger, retries, signed delivery, compensation actions and undo
  compensation scheduling.
- **Persistent signed webhooks** — rule table, event cursor, HMAC signature,
  backoff retries, delivery history and test endpoint.
- **Cost budgets / rate limits** — monthly token and cost budgets, per-token
  limits, fixed-window request limits, usage ledger and 402/429 responses.
- **Benchmark and failure tests** — real retrieval benchmark, Hypothesis merge
  properties, SSE queue overflow, SSRF blocks, migration idempotence and the
  64-test integration suite.

Residual boundaries: OIDC/WebAuthn/MFA, language-aware Chinese tokenization,
cross-region disaster-recovery drills, automated axe/Playwright visual
regression, and a light theme. These are explicit product/operations decisions,
not hidden omissions.

## 2. Canonical data model

| Table | Role |
|---|---|
| `workspaces` | Tenant + commander API key (unchanged) |
| `agent_tokens` | Scoped agent keys: `SCOUT / ANALYST / AUDITOR / EXECUTOR`, namespaces, active flag |
| `shared_documents` | Notion-like artifacts: `doc_key`, title, Markdown content, `frontmatter` JSONB, namespace, version, human lock |
| `context_slots` | Live state grid: JSONB state, version, optional lease, human lock + note, schema hint |
| `collab_tasks` | Kanban tasks: status, priority, role, claim/lease, L1 brief, L2 artifact URI, ping-pong counter, human feedback |
| `workspace_links` | Typed links between tasks/docs/slots (renamed `causal_edges`) |
| `timeline_streams` | Append-only audit/event stream, namespaced |
| `state_audit_diffs` | Before/after images per `session_batch_id` for time-travel undo |
| `schema_migrations` | Applied schema versions |

Human-lock canonical column name: `locked_by_human` (mapped to `human_locked`
in older drafts). Error code: `HUMAN_SOVEREIGNTY_LOCKED`.

## 3. Canonical enums

**Task status:** `TODO`, `READY`, `IN_PROGRESS`, `REVIEWING`, `HANDOFF`,
`AWAITING_HUMAN`, `ESCALATED_TO_HUMAN`, `DONE`, `FAILED`.

**Agent role:** `SCOUT`, `ANALYST`, `AUDITOR`, `EXECUTOR`. A workspace commander
key is the implicit `COMMANDER` role.

**Workspace link type:** `triggers`, `contradicts`, `supports`, `precedes`,
`resolves`, `depends_on`, `references`, `blocks`, `produced_by`.

**Task state machine**

```
create ──► READY / TODO
TODO ──(deps satisfied)──► READY
READY/HANDOFF ──claim──► IN_PROGRESS
IN_PROGRESS ──handoff──► REVIEWING | HANDOFF | DONE | AWAITING_HUMAN | FAILED
REVIEWING ──approve──► DONE
REVIEWING ──reject──► IN_PROGRESS   (ping_pong_count += 1)
any active ──escalate──► ESCALATED_TO_HUMAN
lease expiry (IN_PROGRESS/REVIEWING) ──► READY (LEASE_EXPIRED event)
```

`ping_pong_count >= max_retries` forces `ESCALATED_TO_HUMAN` and blocks automatic
retry. A human must resolve the escalation.

## 4. Six mechanisms → implementation

### 4.1 Progressive disclosure (L0/L1/L2)
- **L0** `workspace_sync`: counts, task IDs/statuses, slot keys + versions +
  human-lock flags, document tree by namespace, schema enums. No raw content.
- **L1** `handoff_brief`: required structured JSON on handoff:
  `summary`, `key_metrics`, `unverified_hypotheses`, `risk_flag`.
- **L2** artifact text/URI: stored as a `shared_documents` row under
  `artifact/<task_key>/<id>`; `artifact_uri = doc://<doc_key>`.
- `dereference(uri)` is the only way to load L2. Only `doc://` is resolvable
  server-side (no SSRF).

### 4.2 Leases + surgical patching
- Tasks and slots support TTL leases (default 60 s).
- `claim`/`renew`/expiry reaper; expired leases return to `READY`.
- State changes use RFC 6902 JSON Patch; documents support section patching by
  Markdown heading. Full-document replacement remains available for creates.

### 4.3 Ping-pong circuit breaker
- Every review rejection increments `ping_pong_count` and appends to
  `reject_history`.
- At `max_retries` (default 3) the task is forced to
  `ESCALATED_TO_HUMAN`; further agent transition attempts are rejected.

### 4.4 Reactive event topology
- DB triggers on slots, tasks, documents, and timeline emit
  `pg_notify('workspace_events', payload)`.
- `GET /v1/stream` (agent auth) and
  `GET /v1/admin/workspaces/{id}/stream` (human console) expose SSE.
- Filters: `tables`, `status`, `assigned_role`, `namespace`.
- No Redis/Kafka. The SSE connection holds one asyncpg LISTEN connection.

### 4.5 Human sovereignty + time travel
- `locked_by_human` on slots and documents. Agents get
  `403 HUMAN_SOVEREIGNTY_LOCKED`; commander/admin edits are allowed and
  broadcast a `HUMAN_OVERRIDE` interrupt event.
- Human edits bump versions so stale agents receive 409.
- Every agent mutation writes a before/after image with `session_batch_id`.
- `POST /v1/admin/workspaces/{id}/batches/{batch_id}/undo` restores images in
  reverse order, bumps versions, records the undo as a new batch, and emits a
  `BATCH_UNDONE` event.

### 4.6 Scoped agent tokens / RBAC
- Workspace key = COMMANDER (all operations, human console).
- Agent tokens are hashed and scoped to a role + namespaces.
- Matrix:
  - SCOUT: read workspace/tasks; append timeline and create raw documents only
    in allowed namespaces; no slot read/write; no task claim.
  - ANALYST: read slots; read/write documents in namespaces; create/claim/handoff
    tasks; no slot writes.
  - AUDITOR: read everything; review tasks; read audit; no slot writes.
  - EXECUTOR: read/write slots; read/write documents; claim/handoff tasks.
  - COMMANDER: everything + human overrides.
- Namespace violations return 403 `NAMESPACE_FORBIDDEN`.

## 5. API surface

### Agent / tenant scoped
- `GET /v1/whoami`
- `GET /v1/workspace/map` (L0)
- `GET /v1/tasks`, `POST /v1/tasks`, `GET /v1/tasks/{task_key}`
- `POST /v1/tasks/{task_key}/claim`
- `POST /v1/tasks/{task_key}/renew`
- `POST /v1/tasks/{task_key}/release`
- `POST /v1/tasks/{task_key}/handoff`
- `POST /v1/tasks/{task_key}/review`
- `POST /v1/tasks/{task_key}/escalate`
- `GET /v1/slots`, `GET /v1/slots/{slot_key}`
- `POST /v1/slots/{slot_key}/patch` (JSON Patch)
- `PUT /v1/slots/{slot_key}` (full replace)
- `GET /v1/documents`, `GET /v1/documents/{doc_key}`
- `PUT /v1/documents/{doc_key}`
- `POST /v1/documents/{doc_key}/patch-section`
- `POST /v1/query` (structured + semantic document query)
- `POST /v1/dereference`
- `POST /v1/links`, `GET /v1/links`
- `GET /v1/timeline`, `POST /v1/timeline`
- `GET /v1/stats`
- `GET /v1/stream` (SSE)

### Human console (admin token)
- Existing workspace CRUD + stats + timeline.
- `GET/PUT /v1/admin/workspaces/{id}/slots...` — edit, human-lock/unlock.
- `GET /v1/admin/workspaces/{id}/tasks`, task review/escalate.
- `GET /v1/admin/workspaces/{id}/documents`, edit/lock.
- Agent-token create/list/disable.
- Audit list + batch undo.
- SSE stream.

### MCP tools (16)
`workspace_sync`, `create_task`, `agent_claim_and_read`,
`agent_patch_shared_state`, `agent_handoff_task`, `agent_escalate_to_human`,
`review_task`, `upsert_shared_doc`, `patch_doc_section`, `query_workspace`,
`dereference`, `propose_document_change`, `vote_on_proposal`,
`list_document_proposals`, `request_human_review`, `draft_external_action`.

`agent_handoff_task.brief` is a typed `HandoffBrief` schema; `artifact_text` and
`artifact_uri` are documented as mutually exclusive. `upsert_shared_doc` and
`patch_doc_section` default to compact responses (`include_content=false`).
`workspace_sync(compact=true)` removes low-value taint/epoch fields.

All MCP tools accept an optional `session_batch_id`; when omitted the server
generates one and returns it.

## 6. Human console views

1. **Blackboard** — human approvals, live state grid with edit + human-lock,
   interrupt feed.
2. **Tasks** — Kanban columns across the canonical statuses, L1 brief cards,
   lease/expiry, ping-pong count, approve/reject/escalate.
3. **Documents** — namespace tree, frontmatter property panel, Markdown
   preview/editor, version + human lock.
4. **Agents** — scoped token creation, role, namespaces, active state.
5. **Audit** — session batches, diff count, undo.

SSE drives automatic refresh across all views.

## 7. Compatibility / migration

- `sql/migrations/v2_to_v3_collab.sql` is idempotent and preserves:
  - workspaces and API keys;
  - context-slot state and versions;
  - timeline events;
  - memory crystals, renamed to shared documents, with `doc_key`,
    `title`, `frontmatter`, `namespace`, `version`, `updated_by`;
  - causal edges, renamed to workspace links.
- Decay/salience/access columns are dropped after migration.
- Fresh installs use the canonical v3 `sql/init.sql`.
- `AUTO_INIT_DB=true` detects a v2 database and applies the migration.
- Existing workspace keys remain valid as COMMANDER keys.

## 8. Acceptance criteria

- v2 database upgrades in place with existing data visible as documents/slots.
- Scoped agents cannot cross role/namespace boundaries.
- Task claim is atomic; leases expire and are reaped; renew works.
- Handoff stores L1 + L2 and `dereference` retrieves L2.
- 3 review rejections escalate to human and block further agent retries.
- Human lock blocks agent mutations with 403 and emits an interrupt.
- Batch undo restores slot/document/task state and is itself audited.
- SSE delivers slot/task/doc changes within the same second.
- Dashboard exercises all five views and live updates.
