# AgentGraph Agent Feedback — 2026-10-21

**Source:** feedback from an agent that used AgentGraph for a full working day.
**Status:** implemented (2026-10-20). This document remains the source of truth
for the pass; the implementation map is at the end of this file.

## Verdict

AgentGraph is unusually agent-friendly because it is designed as a shared
blackboard for multiple agents, not as a database with an API bolted on. The
core primitives are sound, but it still needs a few infrastructure-grade
pieces before it can be treated as reliable storage rather than an early
product with a strong design philosophy.

## What already works

1. **Self-description.** The root endpoint exposes the service name, version
   and primitives; MCP tool schemas are complete enough to build a connector
   and exercise all reads/writes without external documentation.
2. **Versioning and optimistic concurrency.** Documents carry `version` and
   `content_hash`; conflicting writes fail instead of silently overwriting.
   Concurrent writes from four processes did not corrupt server data.
3. **Structured errors.** `not_found` and validation failures return
   `code`, `message`, and `details`; 422 identifies the failing field.
4. **Human sovereignty is systemic.** Agent output defaults to draft,
   external actions can only be drafted, and human-locked documents reject
   agent writes. It is enforced in permissions, not just displayed as a badge.

## Gaps observed

1. **No exhaustive listing / census primitive.** `query_workspace` is semantic
   discovery, not a complete enumeration: 46 target documents returned 37,
   `total` can fluctuate, `limit` is capped at 50, and there is no “list every
   key in a namespace”. Reconciliation/audit/full export requires fetching
   known keys one by one.
2. **Agents cannot read the API documentation.** `/docs` and `/openapi.json`
   are protected by admin HTTP Basic auth. Workspace-token agents cannot read
   the API they are using, while humans can sign into the console.
3. **New agent writes are invisible by default.** Agent-authored documents are
   `DRAFT`/`UNTRUSTED`; queries exclude unverified documents unless
   `include_unverified=true`. An agent cannot retrieve what it just wrote
   without knowing the trap.
4. **No batch operations.** 46 targets meant 46 round trips, each repeating MCP
   initialization (1–3 s per call). Sync/migration workloads are amplified by an
   order of magnitude.
5. **No delete/archive.** Mistakes, test residue and duplicates remain forever;
   a space written by agents at high frequency will become a junk drawer.
6. **Frontmatter filters are equality/containment only.** No range comparison
   (`gte`/`lte`) or `exists`; “goals due within 30 days” or “invoices above X”
   must be fetched and filtered client-side.
7. **No change notifications.** Collaboration is polling-only: no incremental
   cursor or webhook; a new task is invisible until another full
   `workspace_sync` pull.

## Prioritized requirements

### P0 — make it usable as a storage layer

#### 1. Exhaustive namespace listing with cursors

Add a primitive that guarantees complete enumeration of a namespace.

Proposed MCP tool:

```text
list_documents(namespace, cursor=None, limit=100, include_archived=False)
```

Response:

```json
{
  "items": [
    {
      "doc_key": "...",
      "version": 4,
      "content_hash": "...",
      "frontmatter": {},
      "verification_status": "VERIFIED",
      "trust_level": "TRUSTED",
      "tainted": false,
      "updated_at": "..."
    }
  ],
  "next_cursor": "opaque-or-null",
  "namespace": "system",
  "count": 100
}
```

Acceptance criteria:

- Cursor is stable and opaque; ordered by `(updated_at, id)` or `doc_key`.
- `next_cursor=null` means the namespace has been fully enumerated.
- `limit` maximum at least 100 (prefer 500); no hidden `total` dependency.
- Default excludes archived documents; `include_archived=true` includes them.
- REST equivalent: `GET /v1/documents?namespace=…&cursor=…&limit=…`.

#### 2. Agent-readable API docs

Give agent tokens a read-only path to the API/MCP contract.

Preferred: expose an MCP resource (or tool) such as
`agentgraph://docs/openapi` that returns the OpenAPI document and connection
guide, without requiring the admin token. Alternative: allow agent tokens on
`/openapi.json` and `/docs` while keeping the interactive admin console
protected.

Acceptance criteria:

- Agent token can fetch the OpenAPI/schema without admin Basic auth.
- No admin-only examples, tenant data or secrets are exposed.
- Human console documentation remains protected where appropriate.

#### 3. Batch read/write

Add bounded batch primitives:

```text
dereference_many(uris: list[str])                 # max 25
upsert_many(documents: list[DocumentUpsert])       # max 25
```

Acceptance criteria:

- Per-item result with `ok`, `code`, `message`, and `details`; one failure does
  not hide successful results.
- Explicit transaction semantics: either all-or-nothing or documented
  per-item commit; prefer all-or-nothing for `upsert_many`.
- Max 25 items per request and a documented payload-size cap.
- Cuts a 46-document sync from 46 handshakes to two MCP calls.

### P1 — prevent silent traps and data decay

#### 4. Soft delete / archive

Add `archived_at`, `archived_by`, and `archive_reason` to documents (and
possibly tasks). Add `archive_document` / `restore_document` primitives.

Acceptance criteria:

- Author/OWNER/COMMANDER can archive; archived documents are excluded from
  ordinary list/query by default.
- `include_archived=true` lists them; direct dereference may still work for
  audit.
- Every archive/restore writes an audit row and timeline event.
- No hard-delete by default; an admin purge remains a separate explicit path.

#### 5. Correct the “own writes are invisible” default

Either:

- query defaults to including the caller's own `DRAFT` documents while
  excluding other agents' unverified documents; or
- `include_unverified` is explicit and prominent in every query tool
  description/schema.

Acceptance criteria:

- A writer can retrieve a document it just created without hidden flags.
- Cross-agent unverified content remains excluded by default.
- Documented in the tool description, README and tests.

#### 6. Frontmatter range filters and better conflicts

Support comparison filters in structured query:

```json
{"filters": {"due_at": {"gte": "2026-11-01", "lte": "2026-11-30"},
             "amount": {"gt": 1000},
             "owner": {"exists": true}}}
```

Conflict errors should return `current_version` (and preferably
`current_hash`) directly, avoiding a second read before retry.

Acceptance criteria:

- `eq/gt/gte/lt/lte/exists` work on scalar frontmatter values.
- Invalid filter operators return a structured 422 with the field path.
- `version_conflict` details include `current_version` and `current_hash`.

#### 7. Incremental workspace sync

Add `since` / cursor support to `workspace_sync` and include per-namespace
counts. A later phase may add webhook delivery.

Acceptance criteria:

- `workspace_sync(since=<opaque-cursor>)` returns changed tasks, documents,
  slots and timeline events since the cursor, plus `next_cursor`.
- Counts are available per namespace.
- A client can stop full polling; a webhook can be layered on later.

## Suggested implementation order

1. List/census primitive (P0.1).
2. Agent-readable docs (P0.2).
3. Batch read/write (P0.3).
4. Own-write query default + documented semantics (P1.5).
5. Soft archive (P1.4).
6. Frontmatter range filters + conflict payloads (P1.6).
7. Incremental sync (P1.7); webhook only after cursor semantics are stable.

## Out of scope for this document

- UI work (login/console design is tracked separately).
- Billing/tier enforcement changes.
- Hard deletion of tenant data.
- Cross-region replication or backup/restore drills.


## Implementation map (2026-10-20)

| Requirement | Shipped surface |
|---|---|
| P0.1 exhaustive listing | MCP `list_documents(namespace, cursor, limit, include_archived, filters)` and `GET /v1/documents?namespace=&cursor=&limit=&include_archived=`; opaque `(updated_at, id)` cursor; `next_cursor=null` terminates; `limit` up to 500; `count` per page |
| P0.2 agent-readable docs | `GET /v1/agent-docs` (workspace/agent token, admin paths stripped) and MCP `get_api_docs`; `/docs` and `/openapi.json` remain admin-protected |
| P0.3 batch I/O | MCP `upsert_many` / `dereference_many`; REST `POST /v1/documents/batch` (single transaction, rollback on any error) and `POST /v1/dereference-many` (per-item `ok`/`error`); both capped at 25 items, batch content cap 5,000,000 bytes; failure error carries `details.index` and `details.doc_key` |
| P1.4 soft archive | `archived_at` / `archived_by` / `archive_reason` columns + migration `3.3.0`; MCP `archive_document` / `restore_document`; REST `POST /v1/documents/{doc_key}/archive|restore`; creator or commander only; audit row + `DOC_ARCHIVED`/`DOC_RESTORED` timeline; excluded from ordinary list/query/map but still dereferenceable |
| P1.5 own-write visibility | `query_workspace` and `POST /v1/query` default to `VERIFIED OR created_by=caller`; other agents' drafts require `include_unverified=true`; documented in README and tool descriptions |
| P1.6 range filters/conflicts | `eq`, `gte`, `lte`, `gt`, `lt`, `exists` (with numeric JSONB type guard); invalid operators return `422 invalid_filter_operator` with `details.field`; `version_conflict` details now include `current_version`, `current_hash`, and `latest` |
| P1.7 incremental sync | `workspace_sync(since=...)` and `GET /v1/workspace/map?since=...`; returns changed tasks/docs/slots, timeline events, `namespace_counts`, `incremental`, and `next_cursor`; invalid cursor returns `400 invalid_cursor` |

Frontmatter object equality now uses the explicit `{"field": {"eq": {...}}}`
form because bare objects in a filter value are parsed as operator expressions.

Tests: `tests/test_feedback.py` covers cursor completeness, opaque/invalid
cursors, archive permissions and hiding, atomic batch writes, per-item batch
dereference, range filters, conflict payloads, incremental sync and agent docs.
