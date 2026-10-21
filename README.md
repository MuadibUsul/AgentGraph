# AgentGraph v3.2.1

**A shared blackboard for agent teams. Agents do the work; humans hold the line.**

The console ships with a persistent **English / 中文** language switch (EN/中文 in the top bar) covering navigation, boards, documents, agents, audit, governance, modals and formatted dates/numbers. AgentGraph is a human-in-the-loop multi-agent workspace: versioned and verifiable shared documents, reviewed proposals with a Proposal Arena, path policies and JSON Schema gates, taint propagation from untrusted sources, human accounts with database row security, chunked hybrid retrieval, persistent signed webhooks, approved external actions with compensation, token/cost budgets, scoped agent tokens, a replayable transactional event outbox, and conflict-aware session-batch undo — all on a single PostgreSQL 16 + pgvector core with REST and MCP access.

**完整中文架构与路径说明：** [docs/PROJECT_ARCHITECTURE.md](docs/PROJECT_ARCHITECTURE.md)（项目介绍、完整目录树、数据模型、核心流程、API/MCP、权限矩阵、部署与扩展点）。

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    Human Commander (Next.js console)                    │
│   approvals · state overrides · document edits · audit + batch undo      │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │  REST + SSE (LISTEN/NOTIFY)
┌───────────────────────────────▼──────────────────────────────────────────┐
│                       AgentGraph Gateway (FastAPI)                       │
│  principal/RBAC · task leases · JSON Patch · circuit breaker · audit     │
│  MCP server (streamable HTTP / stdio) · token compressor                 │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │ Async SQLAlchemy / asyncpg
┌───────────────────────────────▼──────────────────────────────────────────┐
│                    PostgreSQL 16 + pgvector                              │
│  workspaces · agent_tokens · shared_documents · context_slots            │
│  collab_tasks · workspace_links · timeline_streams · state_audit_diffs   │
└──────────────────────────────────────────────────────────────────────────┘
```

## Why v3

v2 was a personal memory engine. Once several agents and a human share the same
workspace, five failure modes dominate:

1. **Context explosion** — sending entire logs downstream dilutes attention.
2. **Silent overwrites / deadlocks** — concurrent agents or crashed agents hold
   tasks forever.
3. **Ping-pong loops** — generator/reviewer agents burn budget rejecting each
   other forever.
4. **Human instructions get overwritten** — an agent running stale logic writes
   over a human correction.
5. **Shared keys** — a public web-scraping agent can erase core state.

v3 addresses each of them in the storage and service layer:

| Challenge | Mechanism |
|---|---|
| Context explosion | L0 workspace map → L1 handoff brief → L2 artifact (`doc://`, on-demand `dereference`) |
| Deadlocks / overwrites | Atomic claims, 60s TTL leases, heartbeat renew, background lease reaper |
| Expensive edits | RFC 6902 JSON Patch for state; Markdown section patches for documents |
| Agent ping-pong | `ping_pong_count` + `max_retries`; forced `ESCALATED_TO_HUMAN` circuit breaker |
| Human sovereignty | `locked_by_human` on slots/docs; agents get 403 `HUMAN_SOVEREIGNTY_LOCKED`; override interrupts |
| Bad agent writes | `state_audit_diffs` before/after images per `session_batch_id`; conflict-aware batch undo and immutable document versions |
| Privilege creep | Scoped `SCOUT / ANALYST / AUDITOR / EXECUTOR` agent tokens with namespace sandboxes |
| Polling | PostgreSQL triggers → transactional `workspace_events` outbox → `LISTEN/NOTIFY` → replayable SSE |
| Untrusted content | `verification_status` + `trust_level`, hash-bound human verification, untrusted-source delimiters |
| Stale leases | Monotonic `lease_epoch` fencing tokens and database-clock lease checks |
| Unsafe edits | Reviewed proposals + Proposal Arena votes, three-way non-overlapping rebase, path policies and JSON Schema |
| Prompt injection | Transitive taint provenance (`tainted`, `taint_sources`, `taint_edges`); agent verification is blocked |
| Agent side effects | External-action outbox, human approval, retries, compensation; persistent signed webhooks |
| Human identity | Hashed owner/reviewer/viewer accounts with expiring sessions, scoped control API and PostgreSQL RLS |
| Retrieval quality | Markdown chunking, HNSW + tsvector + pg_trgm hybrid retrieval (RRF), verified/taint filters |
| Cost control | Fixed-window rate limits, monthly token/cost budgets, usage ledger and per-token limits |

## Quickstart

**Prerequisites:** Docker Compose v2.

```bash
cd agentgraph
cp .env.example .env
# Set a strong ADMIN_TOKEN in .env. Choose OPENAI/OLLAMA embeddings for production.
docker compose up -d --build
```

Open:

- Human console: http://localhost:8000/dashboard/
- API docs: http://localhost:8000/docs
- MCP streamable HTTP: http://localhost:8000/mcp/

Create a workspace (the commander key is shown once):

```bash
docker compose exec api python -m app.cli create-workspace --name "Quant & Research Hub"
```

### Production compose

The repo ships `docker-compose.prod.yml` for a shared Docker host:

```bash
cp .env.example .env
# Set POSTGRES_PASSWORD and a strong ADMIN_TOKEN; keep the file mode 600.
docker compose -f docker-compose.prod.yml up -d --build
```

It does **not** publish a host port. The `agentgraph-api` container joins the
external `web` network and is reverse-proxied by Caddy. Primary domain:
`agentgraph.fusionpropulsion.space`; the original `agentgraph.104-207-82-85.nip.io`
route remains as a fallback. PostgreSQL uses the dedicated `agentgraph_pgdata`
volume, never another project's database.

The production deployment on the Tlines server stores the admin token in
`/home/deploy/agentgraph/.env` (`ADMIN_TOKEN`, mode 600). The console is available
at `https://agentgraph.fusionpropulsion.space/dashboard/`
(`https://agentgraph.104-207-82-85.nip.io/dashboard/` remains a fallback).

Or use the admin API:

```bash
curl -s http://localhost:8000/v1/admin/workspaces \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Quant & Research Hub","tier":"developer_5usd"}'
```

```json
{
  "workspace": {"id": "…", "name": "Quant & Research Hub", "tier": "developer_5usd", "created_at": "…"},
  "api_key": "ag_…"
}
```

The workspace key is the **human commander** key. Mint a scoped token for every
agent runtime:

```bash
docker compose exec api python -m app.cli create-agent-token \
  --workspace-id <uuid> \
  --agent grid_executor \
  --role EXECUTOR \
  --namespaces "*"
```

Or use the **Agents** tab in the console. Agent keys look like `agt_…` and are
shown once; only their SHA-256 hash is stored. Rotate a leaked key without
changing the agent identity:

```bash
curl -X POST http://localhost:8000/v1/admin/workspaces/<uuid>/agent-tokens/<token-uuid>/rotate   -H "Authorization: Bearer $ADMIN_TOKEN"
```

The response contains the new key exactly once; the old key is invalid
immediately. Rotate during a maintenance window because the runtime must store
the new key before its next request.

### Self-service accounts

Email registration is public when `HUMAN_SIGNUP_ENABLED=true`:

```bash
curl -s http://localhost:8000/v1/auth/register   -H "Content-Type: application/json"   -d '{"email":"owner@example.com","password":"at-least-8-chars",
       "display_name":"Owner","workspace_name":"My Workspace"}'
```

The response is a human session; it creates a `free` workspace and an `OWNER`
account. The same session token works on `/v1/human/*` and tenant-scoped `/v1/*`
endpoints, and the console uses it directly (no admin token needed).

Google login uses Google Identity Services + `POST /v1/auth/google` with a
Google ID token. Set `GOOGLE_CLIENT_ID` to a Web OAuth client ID and add
`https://<your-console-host>` to its authorized JavaScript origins. No client
secret is required because the backend verifies the ID token with Google.
`GET /v1/auth/config` tells the console whether signup and Google are enabled.
Email verification (delivery of a confirmation link) is not implemented yet.

### First collaborative turn

```bash
export CMD="ag_commander_key"
export EXEC="agt_executor_key"

# 1. Agents create/open a shared state slot
curl -s http://localhost:8000/v1/slots/trading:regime \
  -H "Authorization: Bearer $EXEC" -H "Content-Type: application/json" \
  -X PUT \
  -d '{"state":{"bias":"LONG","confidence":0.7},"goal":"trade bias","expected_version":0}'

# 2. A human or planner creates a task
curl -s http://localhost:8000/v1/tasks \
  -H "Authorization: Bearer $CMD" -H "Content-Type: application/json" \
  -d '{"task_key":"TASK-104","title":"Validate BTC liquidity","assigned_role":"EXECUTOR","input_context":{"slot_key":"trading:regime"}}'

# 3. An executor claims it with a TTL lease and reads L1 context
curl -s http://localhost:8000/v1/tasks/TASK-104/claim \
  -H "Authorization: Bearer $EXEC" -H "Content-Type: application/json" \
  -d '{"lease_seconds":60,"session_batch_id":"11111111-1111-1111-1111-111111111111"}'

# 4. Hand off with a required L1 brief + optional L2 artifact
curl -s http://localhost:8000/v1/tasks/TASK-104/handoff \
  -H "Authorization: Bearer $EXEC" -H "Content-Type: application/json" \
  -d '{
    "next_status":"REVIEWING",
    "brief":{"summary":"liquidity cluster confirmed","key_metrics":{"price":65000},"unverified_hypotheses":["holds above 64800"],"risk_flag":false},
    "artifact_text":"# Full report\n\nLong L2 evidence..."
  }'

# 5. A reviewer approves, or rejects into the circuit breaker
curl -s http://localhost:8000/v1/tasks/TASK-104/review \
  -H "Authorization: Bearer $AUD" -H "Content-Type: application/json" \
  -d '{"decision":"approve","feedback":"verified","expected_version":2}'
```

Every mutation is audited with a `session_batch_id`; a human can undo the whole
agent session from the Audit tab or:

```bash
curl -s -X POST http://localhost:8000/v1/admin/workspaces/<uuid>/batches/<batch_id>/undo \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

## Core Mechanisms

### L0 / L1 / L2 progressive disclosure

- **L0** — `workspace_sync` / `GET /v1/workspace/map`: counts, task cards, slot
  keys/versions/human-lock flags, document tree, enums. No raw content.
- **L1** — every handoff to `REVIEWING`, `HANDOFF`, or `DONE` requires:
  `summary`, `key_metrics`, `unverified_hypotheses`, `risk_flag`.
- **L2** — artifact text is stored as a shared document under
  `artifact/<task_key>/<id>`; `artifact_uri` is `doc://<doc_key>`. Load it only
  with `dereference(uri)`.

### Leases and surgical edits

Tasks and slots use TTL leases (default 60 s). Claims atomically increment a
monotonic `lease_epoch`; renewals and handoffs check the caller, the live lease
and (when supplied) the epoch, all against the PostgreSQL clock. Another agent
gets `409 lease_conflict`. A background reaper uses `SKIP LOCKED` and bumps the
epoch again, so an expired agent cannot resume with stale assumptions. State
changes use RFC 6902 JSON Patch; documents use heading-anchored section patches,
so agents only emit the changed fragment.

```json
{
  "ops": [{"op": "replace", "path": "/risk_model/max_drawdown", "value": 0.04}],
  "expected_version": 7
}
```

### Ping-pong circuit breaker

An auditor rejection increments `ping_pong_count`. At `max_retries` (default 3)
the task is forced to `ESCALATED_TO_HUMAN`, the lease is cleared, and further
agent transitions return `409 escalated_to_human`. A human decision resets the
counter.

Task states:

```
TODO → READY → IN_PROGRESS → REVIEWING → DONE
                     ├────────► HANDOFF
                     ├────────► AWAITING_HUMAN / ESCALATED_TO_HUMAN
                     └────────► FAILED
lease expiry (IN_PROGRESS/REVIEWING) → READY
```

### Document versions, verification and trust

- Every document write creates an immutable `document_versions` row with a
  SHA-256 content hash, parent version, author and `session_batch_id`.
- `verification_status` is `DRAFT`, `PENDING_REVIEW`, `VERIFIED` or
  `DEPRECATED`. Verification binds to the exact current version and content
  hash in `verified_version` / `verified_hash`; any later content change resets
  it to `DRAFT` and invalidates the verification.
- `query_workspace` returns only `VERIFIED` documents by default; pass
  `include_unverified=true` to search drafts. `dereference` remains explicit.
- `trust_level` marks provenance. SCOUT-created documents are `UNTRUSTED`, and
  the agent-facing Markdown view wraps their body in
  `[[UNTRUSTED_SOURCE]] … [[/UNTRUSTED_SOURCE]]`; human verification promotes
  the document to `TRUSTED`.
- The console has Verify / Deprecate / History; history lists every immutable
  version and previews its Markdown.

### Human sovereignty + time travel

- `locked_by_human` on slots and documents. Agent writes return **403
  `HUMAN_SOVEREIGNTY_LOCKED`**; the human console can override and unlock.
- Every human override emits a `HUMAN_OVERRIDE` timeline event and an SSE
  interrupt.
- Every agent mutation writes a before/after image into `state_audit_diffs`
  keyed by `session_batch_id`. Undo applies before-images in reverse order,
  bumps versions, deletes rows created in the batch, and records the undo itself
  as a new auditable batch.
- **Undo is conflict-aware:** it first dry-runs the batch in reverse and compares
  the current state with each recorded after-image. If a later human or agent
  changed the same record, it returns `409 undo_conflict` with the fields that
  drifted. A human can review and explicitly retry with `force=true`.
- Timeline rows and the `workspace_events` outbox are append-only; undo does
  **not** erase event history or reverse external side effects, and the response
  says so explicitly.

### Scoped agent tokens

| Role | Can |
|---|---|
| **SCOUT** | Append timeline events and create raw documents in allowed namespaces; no state reads/writes, no task claims. Task creation with `assigned_role=SCOUT` is rejected instead of creating a permanently stuck task. |
| **ANALYST** | Read slots/docs/tasks; write documents; create/claim/handoff tasks; no slot writes |
| **AUDITOR** | Read everything; review tasks; read audit; no slot writes |
| **EXECUTOR** | Read/write state slots and documents; claim/handoff tasks |
| **COMMANDER** | Workspace key / human console: everything + human overrides |

Namespaces apply to documents and timeline events. A scout with `raw_inbox` can
append observations but cannot touch the default strategy namespace.

### Realtime event bus

Postgres triggers write a transactional outbox row to `workspace_events` and
then `pg_notify` its id. One LISTEN connection per API process fans out to all
SSE subscribers; no event is lost while a client is disconnected:

- On connect, send `Last-Event-ID` (or `last_event_id=`) and the endpoint replays
  missed outbox rows from that cursor before switching to live delivery.
- Every SSE frame carries a monotonic `id:`, so browsers and agents can resume.
- Old outbox rows are pruned after `EVENT_RETENTION_DAYS` (default 7).
- `GET /v1/stream?tables=context_slots,collab_tasks&status=READY`
- `GET /v1/admin/workspaces/{id}/stream` (admin token)

The console tracks the last event id, reconnects with it, and refreshes the
active view. Filters include `tables`, `status`, `assigned_role`, and
`namespace`.

## MCP Tools

The MCP server exposes sixteen tools over stdio and streamable HTTP:

| Tool | Purpose |
|---|---|
| `workspace_sync` | L0 map: tasks, slot keys/versions, document tree, enums, role permissions; `compact=true` for a smaller response |
| `create_task` | Create a task for EXECUTOR/ANALYST/AUDITOR, including `depends_on`; SCOUT tasks are rejected |
| `agent_claim_and_read` | Claim a task with a lease; return upstream L1 briefs, referenced slots/docs |
| `agent_patch_shared_state` | RFC 6902 JSON Patch on a state slot; human-lock enforced |
| `agent_handoff_task` | Submit a typed L1 brief + optional L2 artifact; release lease; create next task |
| `agent_escalate_to_human` | Stop and escalate for human arbitration |
| `review_task` | Approve/reject a task; agent feedback is stored as `review_feedback` |
| `upsert_shared_doc` | Create/update a document; compact response by default, `include_content=true` for the body |
| `patch_doc_section` | Replace one Markdown section by heading; compact response by default |
| `query_workspace` | JSONB filters + hybrid search; verified-only by default, `include_unverified=true` for drafts |
| `dereference` | Resolve `doc://<doc_key>` L2 content on demand |
| `propose_document_change` | Submit a human-reviewed proposal; proposals are never agent-applied |
| `vote_on_proposal` | Score a pending Proposal Arena entry |
| `list_document_proposals` | List pending/applied proposals with vote scores |
| `request_human_review` | Explicitly queue a proposal or task for a human and return the human endpoint |
| `draft_external_action` | Queue an external action for mandatory human approval; never executes directly |

Handoff tools accept `lease_epoch` from the claim response; servers enforce the
caller, active lease and epoch before releasing work.

### HTTP MCP (hosted / multi-tenant)

Point an MCP client that supports remote HTTP servers at:

```
http://localhost:8000/mcp/
Authorization: Bearer agt_agent_key
```

The token determines the role and namespaces; the host must be allowed by
`MCP_ALLOWED_HOSTS` when served behind a custom domain.

`MCP_STATELESS_HTTP=true` is the default. The server does not issue an
`mcp-session-id`; a missing session id is expected, not a lost session. Send
`initialize` and `tools/list` on each new client connection as needed.

Production `/docs` and `/openapi.json` are protected by HTTP Basic when
`DOCS_REQUIRE_AUTH=true`; use any username and the admin token as the password.

### stdio MCP (local / self-hosted)

```json
{
  "mcpServers": {
    "agentgraph": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "app.mcp_server"],
      "env": {
        "DATABASE_URL": "postgresql+asyncpg://agentgraph:agentgraph@localhost:5432/agentgraph",
        "MCP_API_KEY": "agt_scoped_agent_key",
        "MCP_AGENT_NAME": "cline_local",
        "EMBEDDING_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-..."
      }
    }
  }
}
```

## Human Console

- **Blackboard** — human approval queue, live state grid, human overrides,
  escalation/interrupt feed.
- **Tasks** — Kanban across canonical statuses, L1 briefs, leases, ping-pong
  counters, approve/reject actions.
- **Documents** — namespace tree, verification/trust badges, frontmatter
  properties, Markdown preview/editor, history browser, Verify/Deprecate and
  human lock.
- **Agents** — mint/disable scoped tokens, role and namespace visibility.
- **Audit** — session batches with before/after diffs, conflict-aware undo and
  an explicit Force retry.

The console is a Next.js 14 static export. The production image builds it in a
multi-stage Docker build and FastAPI serves it at `/dashboard/`.

## Data Model

| Table | Purpose |
|---|---|
| `workspaces` | Tenant + commander key hash |
| `agent_tokens` | Hashed scoped agent keys, role, namespaces, active flag |
| `shared_documents` | `doc_key`, Markdown, frontmatter, namespace, version, `content_hash`, `verification_status`, `trust_level`, verified version/hash, human lock, embedding + model |
| `context_slots` | JSONB live state, schema hint, version, `lease_epoch`, agent lease, human lock + note |
| `collab_tasks` | Status, priority, role, claim/lease + `lease_epoch`, L1 brief, L2 URI, reject history, circuit breaker |
| `workspace_links` | Typed links between tasks/docs/slots (`depends_on`, `references`, …) |
| `timeline_streams` | Append-only audited events, namespace, token cost/latency |
| `state_audit_diffs` | Before/after images per session batch |
| `document_versions` | Immutable document versions: hash, parent, author, batch, content, verification, taint |
| `document_chunks` | Markdown chunks with HNSW vectors, generated tsvector and trigram index |
| `document_proposals` / `proposal_votes` | Reviewed proposals, arena scores and applied versions |
| `path_policies` | Glob-scoped document/slot/task write policy and JSON Schema |
| `human_users` / `human_sessions` | Hashed human accounts and expiring bearer sessions |
| `taint_edges` | Source-document → document/slot/task provenance graph |
| `external_actions` / `external_action_attempts` | Approval-gated outbound actions, retries and compensation |
| `webhook_rules` / `webhook_deliveries` | Persistent signed webhook subscriptions and retry history |
| `workspace_budgets` / `usage_ledger` / `rate_limit_windows` | Quotas, usage accounting and fixed-window limits |
| `workspace_events` | Transactional outbox with monotonic id for SSE replay |
| `schema_migrations` | Applied schema versions (current `3.2.1`) |

Canonical DDL: [`sql/init.sql`](sql/init.sql).

## v2 → v3.2 Migration

The upgrade runs in place; no data reset is required.

```bash
# Always take a backup first
docker compose exec -T db pg_dump -U agentgraph -d agentgraph \
  --no-owner --clean --if-exists > backups/v2_before_v3.sql

# Rebuild and restart; AUTO_INIT_DB=true detects memory_crystals and applies
# sql/migrations/v2_to_v3_collab.sql, then v3_hardening.sql, under an advisory lock.
docker compose up -d --build
docker compose logs -f api
# look for: "v2 -> v3 migration complete" and "backfilled N document version(s)"
```

What is preserved:

- Workspaces and commander API keys.
- Context slots (with new human-lock/namespace fields).
- Timeline events.
- Memory crystals → `shared_documents`: `doc_key`, title, frontmatter, version,
  namespace, timestamps are backfilled; old salience/decay/access columns are
  retired.
- Causal edges → `workspace_links`.
- v3.1 hardening adds `lease_epoch`, document content hashes, verification/trust,
  immutable `document_versions` (existing docs get an initial backfilled version)
  and the `workspace_events` outbox.
- v3.2 adds reviewed proposals/arena votes, path policies, taint provenance,
  human accounts/RLS, typed link foreign keys, chunks/hybrid retrieval,
  webhooks, external actions, budgets and rate limits. Existing documents keep
  their content; run the admin **Reindex retrieval** action once to populate
  chunks. Existing documents start as `DRAFT` /
  `UNTRUSTED` and are returned by semantic search only after human verification.

v2 endpoints (`crystallize`, `context_pull`, `assert_state`, `traverse_cause`)
and the four old MCP tools are intentionally replaced by the v3 tools above.
Existing commander keys keep working. The migration is idempotent and uses a
Postgres advisory lock so multiple API replicas cannot race DDL.

## Configuration

Copy [`.env.example`](.env.example) to `.env`. Important settings:

| Variable | Default | Description |
|---|---|---|
| `ADMIN_TOKEN` | — | Required for admin API and console; use a long random secret |
| `DATABASE_URL` | local asyncpg URL | PostgreSQL 16 + pgvector |
| `AUTO_INIT_DB` | `false` | Apply init + migrate on boot |
| `EMBEDDING_PROVIDER` | `hash` | `openai`, `ollama`, or offline `hash` |
| `OPENAI_API_KEY` | — | Required for OpenAI embeddings |
| `DEFAULT_TASK_LEASE_SECONDS` | `60` | Default task/slot lease TTL |
| `LEASE_REAPER_INTERVAL_SECONDS` | `15` | Background lease reaper cadence |
| `DEFAULT_MAX_RETRIES` | `3` | Ping-pong threshold for new tasks |
| `SSE_HEARTBEAT_SECONDS` | `15` | SSE keep-alive interval |
| `SSE_REPLAY_LIMIT` | `500` | Max missed events replayed per reconnect |
| `EVENT_RETENTION_DAYS` | `7` | Outbox / replay retention window |
| `MAX_SEARCH_TOP_K` | `50` | Semantic query cap |
| `FREE_MAX_DOCUMENTS` / `FREE_MAX_SLOTS` | `5000` / `20` | Free tier caps |
| `DEVELOPER_MAX_DOCUMENTS` / `_SLOTS` | `50000` / `100` | $5 tier caps |
| `PRO_MAX_DOCUMENTS` / `_SLOTS` | `500000` / `1000` | Pro caps |
| `MCP_ALLOWED_HOSTS` | localhost | JSON list for custom domains |

Use one embedding model consistently per workspace. Vectors smaller than
`EMBEDDING_DIMENSIONS` are zero-padded (cosine-invariant); changing the
configured dimension clears existing vectors and recreates the HNSW indexes on
startup, so run a full workspace reindex afterwards.

## Development

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    POSIX: source .venv/bin/activate
pip install -r requirements-dev.txt

# disposable pgvector database
docker run -d --name agentgraph-test-db \
  -e POSTGRES_USER=agentgraph -e POSTGRES_PASSWORD=agentgraph \
  -e POSTGRES_DB=agentgraph -p 55432:5432 \
  -v "$PWD/sql/init.sql:/docker-entrypoint-initdb.d/001_init.sql:ro" \
  pgvector/pgvector:pg16

export TEST_DATABASE_URL="postgresql+asyncpg://agentgraph:agentgraph@localhost:55432/agentgraph"
pytest -q
ruff check .
```

**75 tests** cover the blackboard engine, JSON/section patches, leases and
fencing epochs, circuit breaker, human sovereignty, conflict-aware batch undo,
document versions/verification/trust, SSE outbox replay, concurrency (10
simultaneous claims with exactly one winner), dependency cycles, RBAC, quotas,
MCP tools, dashboard serving, and v3 REST flows.

Both migration paths were verified:

- fresh `init.sql` + `v3_hardening.sql`;
- v2 dump → v3 migration → hardening: 7 crystals → documents, 3 edges → links,
  2 slots, 7 backfilled immutable versions.

Dashboard development:

```bash
cd dashboard
npm install
npm run dev   # http://localhost:3000/dashboard/ (API defaults to :8000)
npm run build # regenerate dashboard/out for FastAPI
```

## Project Layout

```
app/
  main.py                  FastAPI app, lifespan, SSE mount, health
  config.py                v3 settings and quotas
  auth.py                  commander/agent principals + RBAC
  models.py                all v3 ORM tables
  schemas.py               request/response contracts
  engine/
    blackboard.py          leases, JSON Patch, section patch, brief validation
    formatter.py           YAML frontmatter + L1 rendering
    embeddings.py          OpenAI / Ollama / offline hash
  services/
    tasks.py               claims, leases, handoff, review, circuit breaker
    slots.py               state grid, human lock, JSON Patch
    documents.py           docs, artifacts, sections, semantic query
    audit.py               before/after diffs + batch undo
    links.py               typed workspace links
    workspace.py           tenants, L0 map, stats, agent tokens
    reaper.py              background lease reaper
    timeline.py            timeline append/read
  api/
    v1_routes.py           agent/commander REST API
    admin_routes.py        human console API
    stream_routes.py       replayable SSE (agent + admin)
  events.py                one LISTEN connection → per-subscriber queues
  mcp_server/server.py     sixteen MCP tools, stdio + HTTP auth
sql/
  init.sql                 canonical base schema + outbox triggers
  migrations/v3_2_production.sql  proposals/policy/taint/accounts/hybrid/webhooks/RLS
  migrations/v2_to_v3_collab.sql
  migrations/v3_hardening.sql
dashboard/                 Next.js 14 human console
tests/                     unit + property + end-to-end integration tests
  scripts/benchmark_retrieval.py  real recall/latency benchmark
  dashboard/app/i18n.tsx       EN/中文 persistence, formatters and language toggle
  docs/design-system/        DesignTools audit, tokens and acceptance evidence
docs/v3-requirements.md    consolidated v3 specification
docs/PROJECT_ARCHITECTURE.md  Chinese project architecture & path guide
scripts/smoke_test.py      deployment smoke test
```

## Operations & Security

- Terminate TLS at a reverse proxy; do not expose Postgres publicly.
- Use a strong `ADMIN_TOKEN`; keep it out of URLs and logs.
- Mint a separate agent token per runtime and namespace; never share the
  commander key with a scouting or web-facing agent.
- Human-lock fields that agents must not change; agents get 403 and an SSE
  interrupt.
- Review the Audit tab after agent sessions. Undo detects later concurrent
  changes; review the conflict and use Force only when you intend to overwrite
  them. Timeline/outbox events and external side effects are not reversed.
- Verify important documents before agents rely on semantic search; changing a
  document's content automatically invalidates its verification.
- Tune `EVENT_RETENTION_DAYS` and `SSE_REPLAY_LIMIT` for reconnect tolerance.
- Set `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` for a custom domain.
- Back up the Postgres volume and test restore.
- Rate limit at the proxy if the API is internet-facing.

## Security invariants

- A workspace commander key is an automation/control credential, **not a human
  session**. Only a real human account session or the admin console token can
  bypass `locked_by_human`, apply proposals, verify tainted documents, or approve
  external actions.
- Task `renew`, `release` and `handoff` require the active lease owner. The admin
  console has an audited `force-release` path for stuck tasks.
- External actions are **always** created as `PENDING_APPROVAL`; creation never
  auto-approves or dispatches, even for a commander key. Approval is a separate
  human/admin action.
- `GET /v1/auth/me` requires a human account session and returns `401
  human_session_required` for workspace/agent keys instead of a 500.
- Production sets `DOCS_REQUIRE_AUTH=true`: `/docs` and `/openapi.json` use HTTP
  Basic with the admin token as the password (username is ignored). This keeps
  the schema available to operators without exposing it publicly.
- Search results carry an RRF relevance `score`; a default semantic threshold
  removes low-relevance matches, and gibberish queries return an empty list.
- Uvicorn runs with `--proxy-headers`; Caddy terminates HTTPS and redirects HTTP
  with 308. `/docs` was verified loading in the production browser.
- Bots should use per-bot `agt_` tokens with role/namespace limits; do not share
  the commander key. The deployed grokbot token set is stored only on the server
  at `/home/deploy/agentgraph/grokbot-agent-tokens.json` (mode 600).
- Disposable test workspaces can be removed with
  `DELETE /v1/admin/workspaces/{id}?confirm=<exact workspace name>`; it is
  irreversible and cascades all tenant rows. Production cleanup should prefer
  archive/export unless the workspace is explicitly disposable.

## License

AgentGraph is intended as an open-core project. Add the license that fits your
distribution plan before publishing publicly.

---

## Hardening Status

Implemented in v3.2:

- **Proposals and Arena** — agents submit proposals; humans apply/reject; stale
  bases auto-rebase even when the caller passes the old expected version; votes
  rank arena entries; a stale base is three-way rebased when edits do not overlap
  and returns `409 proposal_conflict` otherwise.
- **Path policy** — glob-scoped `require_proposal`, allowed roles, max content
  bytes and JSON Schema validation for documents, slots and tasks.
- **Taint propagation** — untrusted/tainted sources produce `taint_edges`,
  downstream documents/slots/tasks are marked, agent verification is blocked,
  human verification clears the root and recomputes downstream taint.
- **Human identity** — scrypt-hashed accounts, expiring sessions, owner/reviewer/
  viewer roles, `/v1/auth/*`, `/v1/human/*`, and a non-superuser `agentgraph_app`
  role with PostgreSQL RLS (`app.workspace_id`) on tenant tables.
- **Typed links** — database-enforced source/target types and real foreign keys,
  with cascade cleanup and dependency-cycle detection.
- **Hybrid retrieval** — Markdown chunks, HNSW, generated tsvector, pg_trgm,
  RRF fusion, verified/taint filters and an admin reindex endpoint.
- **Outbound control** — signed persistent webhooks with retries, external actions
  requiring human approval, idempotency keys, attempts ledger and compensation.
  A reference bot-side receiver is included at `scripts/webhook_receiver.py`:
  it verifies `X-AgentGraph-Timestamp`/`X-AgentGraph-Signature`, rejects stale
  deliveries, and appends verified events to a JSONL outbox for the bot loop.
- **Failure telemetry** — rejected claims, renew/release, handoff, review and
  escalation attempts are written to the timeline as
  `TASK_<OPERATION>_FAILED` with code, HTTP status, message and details.
- **MCP transport** — `MCP_JSON_RESPONSE=true` (default) makes streamable HTTP
  return `application/json` instead of an SSE frame, so clients can parse the
  JSON-RPC response directly. Set it to `false` for clients that require SSE.
- **Review outputs** — `workspace_sync` includes an `outputs` list with completed
  task keys, handoff summaries, artifact URIs and reviewers. Approving a task
  marks its non-tainted `doc://` artifact VERIFIED/TRUSTED so default retrieval
  finds it; tainted artifacts still require explicit human verification.
- **Review contract** — MCP `review_task.next_status` accepts `DONE`, `HANDOFF`
  or `READY` (omit it for the default); rejecting returns the task to `READY`.
  `IN_PROGRESS` is not a review target. `agent_claim_and_read.lease_seconds`
  defaults to 60 and is constrained to 5–3600, exposed in the tool schema.
- **Legacy artifact repair** — `POST /v1/admin/workspaces/{id}/documents/backfill-verified`
  marks completed-task artifacts VERIFIED/TRUSTED in multiple passes and
  recomputes downstream document taint, so documents approved before auto-
  verification become visible to default retrieval.
- **Audit integrity** — human and admin sessions always write their own actor
  identity (workspace automation keys may still name an actor). Historical
  misattributions can be corrected with an explicit
  `REVIEW_ATTRIBUTION_CORRECTED` timeline event instead of rewriting history.
- **Budgets** — DB-backed fixed-window rate limits, monthly token/cost budgets,
  usage ledger, per-token limits and 429/402 responses.
- **Bilingual console** — a persistent EN/中文 toggle (`agentgraph.lang`),
  translated navigation, cards, tables, modals, empty/error copy, status/role
  labels and localized numbers/dates; technical identifiers stay monospace.
- **v3.3 console refresh** — flat graphite surfaces, one solid teal action
  color, 14px body/12px metadata, 264px sidebar, compact statistics, underline
  tabs with a single alert badge, a mobile connection dialog, and fixed-layout
  state tables that keep long JSON inside its column. Design evidence lives in
  `docs/design-system/`.
- **Notion HTML ingestion** — detects Notion/HTML uploads, converts tables,
  headings and lists to Markdown while preserving surrounding Markdown, strips
  active content/unsafe URLs, repairs existing documents via an admin endpoint,
  and renders legacy raw HTML through a sanitized `rehype-raw` pipeline.

Verification: `87 passed` against a fresh pgvector database, production smoke
test, public HTTPS dashboard, and a real retrieval benchmark
(`docs/benchmarks/latest.md`).

Remaining honest boundaries:

- The three-way merge is conservative: duplicate-line ambiguity and overlapping
  edits deliberately return a conflict instead of guessing.
- Full-text search uses PostgreSQL `simple` + trigram; it is not a language-aware
  Chinese tokenizer. Semantic embeddings support OpenAI-compatible endpoints,
  Ollama and the offline hash provider. Production currently uses **Jina AI
  `jina-embeddings-v3`** (`EMBEDDING_PROVIDER=openai`, 1024 dimensions,
  `OPENAI_BASE_URL=https://api.jina.ai/v1`) with the key held only in the server
  environment. The free Jina tier is 100K TPM, so the production reindex uses
  `EMBEDDING_BATCH_SIZE=8` and `EMBEDDING_BATCH_INTERVAL_SECONDS=5` to pace
  requests. Changing dimensions clears the vector columns and recreates the
  HNSW indexes on startup; run `POST /v1/admin/workspaces/{id}/reindex` for each
  workspace afterwards.
- Human auth uses password sessions, not OIDC/WebAuthn/MFA.
- Visual regression/axe automation is not configured in this repository; the
  design-system acceptance report marks those items NOT TESTED/BLOCKED.
- The console is dark-only by product decision; the token layer is light-theme
  ready but no light theme is shipped.

See also: [design audit and acceptance](docs/design-system/ACCEPTANCE_REPORT.md),
[project architecture](docs/PROJECT_ARCHITECTURE.md), and
[retrieval benchmark](docs/benchmarks/latest.md).

---

**AgentGraph v3.2.1** — MCP/REST parity, typed handoff briefs, reviewed proposals, tainted-source propagation,
human accounts with RLS, hybrid retrieval, signed webhooks, compensated external
actions, budgets, fenced leases, replayable events and conflict-aware time travel.

v3.2.1 retest fixes: rejected reviews now return the task to `READY` with
`review_feedback`/`reviewed_by` and clear the lease; admins can reassign or
cancel legacy `SCOUT` tasks; MCP tool results are real structured objects (no
double-encoded `structuredContent.result`) and errors share a
`{code, http_status, message, details}` envelope in both text and
`structuredContent`; SDK-level argument validation is normalized to
`validation_error`/422 instead of raw Pydantic text; MCP-originated timeline
events carry `platform=mcp` and the document's namespace; `session_batch_id` is
a UUID in tool schemas; section patches strip a repeated leading heading.
Self-vote errors include the proposal/voter details, and reviewing a task in an
ineligible state returns `task_not_reviewable` with the actual decision.
