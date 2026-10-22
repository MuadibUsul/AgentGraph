-- AgentGraph v3.1 canonical schema (PostgreSQL 16 + pgvector)
-- Shared Blackboard Workspace: tasks, state grid, documents, links, audit, SSE.
-- Idempotent: safe to execute on every boot via AUTO_INIT_DB=true.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";

CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(32) PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 1. Tenant + commander API key ------------------------------------------------
CREATE TABLE IF NOT EXISTS workspaces (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(128) NOT NULL,
    api_key_hash VARCHAR(256) UNIQUE NOT NULL,
    tier VARCHAR(32) NOT NULL DEFAULT 'developer_5usd',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. Scoped agent tokens -------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_tokens (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    agent_signature VARCHAR(128) NOT NULL,
    api_key_hash VARCHAR(256) UNIQUE NOT NULL,
    role_scope VARCHAR(32) NOT NULL
        CHECK (role_scope IN ('SCOUT', 'ANALYST', 'AUDITOR', 'EXECUTOR')),
    allowed_namespaces TEXT[] NOT NULL DEFAULT ARRAY['*'],
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ,
    UNIQUE(workspace_id, agent_signature)
);

CREATE INDEX IF NOT EXISTS idx_agent_tokens_workspace
    ON agent_tokens(workspace_id);

-- 3. Shared documents (former memory crystals) --------------------------------
CREATE TABLE IF NOT EXISTS shared_documents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    doc_key VARCHAR(256) NOT NULL,
    title VARCHAR(256) NOT NULL DEFAULT 'Untitled Artifact',
    content TEXT NOT NULL,
    content_hash VARCHAR(64),
    frontmatter JSONB NOT NULL DEFAULT '{}'::jsonb,
    verification_status VARCHAR(32) NOT NULL DEFAULT 'DRAFT'
        CHECK (verification_status IN ('DRAFT', 'PENDING_REVIEW', 'VERIFIED', 'DEPRECATED')),
    trust_level VARCHAR(16) NOT NULL DEFAULT 'UNTRUSTED'
        CHECK (trust_level IN ('TRUSTED', 'UNTRUSTED')),
    verified_version INT,
    verified_hash VARCHAR(64),
    archived_at TIMESTAMPTZ,
    archived_by VARCHAR(128),
    archive_reason TEXT,
    embedding VECTOR(1024),
    embedding_model VARCHAR(128),
    namespace VARCHAR(64) NOT NULL DEFAULT 'default',
    version INT NOT NULL DEFAULT 1 CHECK (version > 0),
    locked_by_human BOOLEAN NOT NULL DEFAULT FALSE,
    human_note TEXT,
    created_by VARCHAR(128) NOT NULL,
    updated_by VARCHAR(128) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(workspace_id, doc_key)
);

CREATE INDEX IF NOT EXISTS idx_docs_workspace
    ON shared_documents(workspace_id);
CREATE INDEX IF NOT EXISTS idx_docs_namespace
    ON shared_documents(workspace_id, namespace);
CREATE INDEX IF NOT EXISTS idx_docs_verification
    ON shared_documents(workspace_id, verification_status);
CREATE INDEX IF NOT EXISTS idx_docs_frontmatter
    ON shared_documents USING gin (frontmatter jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_docs_embedding
    ON shared_documents USING hnsw (embedding vector_cosine_ops);

-- 4. Live state grid (former context slots) ------------------------------------
CREATE TABLE IF NOT EXISTS context_slots (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    slot_key VARCHAR(256) NOT NULL,
    goal TEXT,
    current_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    schema_hint JSONB NOT NULL DEFAULT '{}'::jsonb,
    blockers JSONB NOT NULL DEFAULT '[]'::jsonb,
    namespace VARCHAR(64) NOT NULL DEFAULT 'default',
    version INT NOT NULL DEFAULT 1 CHECK (version > 0),
    lease_epoch INT NOT NULL DEFAULT 0,
    locked_by VARCHAR(128),
    lock_expires_at TIMESTAMPTZ,
    locked_by_human BOOLEAN NOT NULL DEFAULT FALSE,
    human_note TEXT,
    updated_by VARCHAR(128) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(workspace_id, slot_key)
);

CREATE INDEX IF NOT EXISTS idx_slots_lookup
    ON context_slots(workspace_id, slot_key);
CREATE INDEX IF NOT EXISTS idx_slots_namespace
    ON context_slots(workspace_id, namespace);

-- 5. Multi-agent task board ----------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'task_status') THEN
        CREATE TYPE task_status AS ENUM (
            'TODO', 'READY', 'IN_PROGRESS', 'REVIEWING', 'HANDOFF',
            'AWAITING_HUMAN', 'ESCALATED_TO_HUMAN', 'DONE', 'FAILED'
        );
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS collab_tasks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    task_key VARCHAR(128) NOT NULL,
    title VARCHAR(256) NOT NULL,
    description TEXT,
    status task_status NOT NULL DEFAULT 'READY',
    priority INT NOT NULL DEFAULT 0 CHECK (priority BETWEEN -100 AND 100),
    assigned_role VARCHAR(64),
    namespace VARCHAR(64) NOT NULL DEFAULT 'default',
    claimed_by VARCHAR(128),
    lease_owner VARCHAR(128),
    lease_expires_at TIMESTAMPTZ,
    input_context JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_artifact JSONB NOT NULL DEFAULT '{}'::jsonb,
    handoff_brief JSONB,
    artifact_uri TEXT,
    human_feedback TEXT,
    reject_history JSONB NOT NULL DEFAULT '[]'::jsonb,
    ping_pong_count INT NOT NULL DEFAULT 0,
    max_retries INT NOT NULL DEFAULT 3,
    version INT NOT NULL DEFAULT 1 CHECK (version > 0),
    lease_epoch INT NOT NULL DEFAULT 0,
    created_by VARCHAR(128) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(workspace_id, task_key)
);

CREATE INDEX IF NOT EXISTS idx_tasks_board
    ON collab_tasks(workspace_id, status, priority DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_lease
    ON collab_tasks(workspace_id, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_tasks_role
    ON collab_tasks(workspace_id, assigned_role, status);

-- 6. Workspace links (former causal edges) ------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'causal_relation') THEN
        CREATE TYPE causal_relation AS ENUM (
            'triggers', 'contradicts', 'supports', 'precedes', 'resolves',
            'depends_on', 'references', 'blocks', 'produced_by'
        );
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_enum e
        JOIN pg_type t ON t.oid = e.enumtypid
        WHERE t.typname = 'causal_relation' AND e.enumlabel = 'depends_on'
    ) THEN
        ALTER TYPE causal_relation ADD VALUE 'depends_on';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_enum e
        JOIN pg_type t ON t.oid = e.enumtypid
        WHERE t.typname = 'causal_relation' AND e.enumlabel = 'references'
    ) THEN
        ALTER TYPE causal_relation ADD VALUE 'references';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_enum e
        JOIN pg_type t ON t.oid = e.enumtypid
        WHERE t.typname = 'causal_relation' AND e.enumlabel = 'blocks'
    ) THEN
        ALTER TYPE causal_relation ADD VALUE 'blocks';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_enum e
        JOIN pg_type t ON t.oid = e.enumtypid
        WHERE t.typname = 'causal_relation' AND e.enumlabel = 'produced_by'
    ) THEN
        ALTER TYPE causal_relation ADD VALUE 'produced_by';
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS workspace_links (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    source_id UUID NOT NULL,
    target_id UUID NOT NULL,
    relation_type causal_relation NOT NULL,
    confidence FLOAT NOT NULL DEFAULT 1.0
        CHECK (confidence >= 0.0 AND confidence <= 1.0),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(workspace_id, source_id, target_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_links_source
    ON workspace_links(workspace_id, source_id);
CREATE INDEX IF NOT EXISTS idx_links_target
    ON workspace_links(workspace_id, target_id);

-- 7. Timeline / audit stream ---------------------------------------------------
CREATE TABLE IF NOT EXISTS timeline_streams (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    agent_signature VARCHAR(128) NOT NULL,
    platform VARCHAR(64) NOT NULL DEFAULT 'unknown',
    event_type VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    namespace VARCHAR(64) NOT NULL DEFAULT 'default',
    token_cost INT NOT NULL DEFAULT 0,
    latency_ms INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_timeline_workspace_time
    ON timeline_streams(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_timeline_namespace
    ON timeline_streams(workspace_id, namespace);

-- 8. Time-travel audit ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS state_audit_diffs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    session_batch_id UUID NOT NULL,
    agent_signature VARCHAR(128) NOT NULL,
    target_table VARCHAR(64) NOT NULL,
    target_record_id UUID NOT NULL,
    operation VARCHAR(32) NOT NULL,
    before_diff JSONB NOT NULL,
    after_diff JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_batch
    ON state_audit_diffs(session_batch_id);
CREATE INDEX IF NOT EXISTS idx_audit_workspace_time
    ON state_audit_diffs(workspace_id, created_at DESC);

-- 9. Immutable document history -----------------------------------------------
CREATE TABLE IF NOT EXISTS document_versions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    document_id UUID NOT NULL REFERENCES shared_documents(id) ON DELETE CASCADE,
    version INT NOT NULL CHECK (version > 0),
    parent_version INT,
    content_hash VARCHAR(64) NOT NULL,
    title VARCHAR(256) NOT NULL,
    content TEXT NOT NULL,
    frontmatter JSONB NOT NULL DEFAULT '{}'::jsonb,
    namespace VARCHAR(64) NOT NULL,
    verification_status VARCHAR(32) NOT NULL DEFAULT 'DRAFT',
    trust_level VARCHAR(16) NOT NULL DEFAULT 'UNTRUSTED',
    author VARCHAR(128) NOT NULL,
    session_batch_id UUID,
    change_summary TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(document_id, version)
);

CREATE INDEX IF NOT EXISTS idx_doc_versions_doc
    ON document_versions(document_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_doc_versions_workspace
    ON document_versions(workspace_id, created_at DESC);

-- 10. Transactional event outbox ----------------------------------------------
CREATE TABLE IF NOT EXISTS workspace_events (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    source_table VARCHAR(64) NOT NULL,
    action VARCHAR(16) NOT NULL,
    record_id UUID,
    actor VARCHAR(128),
    version INT,
    status VARCHAR(64),
    slot_key VARCHAR(256),
    task_key VARCHAR(128),
    doc_key VARCHAR(256),
    namespace VARCHAR(64),
    assigned_role VARCHAR(64),
    locked_by_human BOOLEAN,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_workspace_events_cursor
    ON workspace_events(workspace_id, id);
CREATE INDEX IF NOT EXISTS idx_workspace_events_time
    ON workspace_events(created_at DESC);

-- 11. Reactive event bus -------------------------------------------------------
-- Base definition so the triggers below can be created. The current
-- definition (typed, seq-numbered events) is sql/functions/
-- notify_workspace_change.sql, applied after every migration on each start.
CREATE OR REPLACE FUNCTION notify_workspace_change() RETURNS trigger AS $$
DECLARE
    row_data JSONB;
    event_payload JSONB;
    new_event_id BIGINT;
BEGIN
    row_data := to_jsonb(NEW);
    event_payload := jsonb_build_object(
        'table', TG_TABLE_NAME,
        'action', TG_OP,
        'workspace_id', row_data->>'workspace_id',
        'record_id', row_data->>'id',
        'actor', COALESCE(
            row_data->>'updated_by',
            row_data->>'claimed_by',
            row_data->>'created_by',
            row_data->>'agent_signature',
            'system'
        ),
        'actor_stamp', COALESCE(
            NULLIF(current_setting('app.lineage_stamp', true), '')::jsonb,
            '{"kind": "system", "principal_id": "system", "token_id": null, "chain": ["system"]}'::jsonb
        ),
        'version', NULLIF(row_data->>'version', '')::int,
        'status', COALESCE(
            row_data->>'status',
            row_data->'frontmatter'->>'status',
            ''
        ),
        'slot_key', row_data->>'slot_key',
        'task_key', row_data->>'task_key',
        'doc_key', row_data->>'doc_key',
        'namespace', COALESCE(row_data->>'namespace', 'default'),
        'assigned_role', COALESCE(row_data->>'assigned_role', ''),
        'locked_by_human', COALESCE(row_data->>'locked_by_human', 'false')::boolean,
        'event_type', COALESCE(row_data->>'event_type', row_data->'payload'->>'event_type', ''),
        'timestamp', NOW()
    );
    INSERT INTO workspace_events (
        workspace_id, source_table, action, record_id, actor, version, status,
        slot_key, task_key, doc_key, namespace, assigned_role, locked_by_human,
        payload, created_at
    ) VALUES (
        (row_data->>'workspace_id')::uuid,
        TG_TABLE_NAME,
        TG_OP,
        (row_data->>'id')::uuid,
        event_payload->>'actor',
        NULLIF(event_payload->>'version', '')::int,
        NULLIF(event_payload->>'status', ''),
        event_payload->>'slot_key',
        event_payload->>'task_key',
        event_payload->>'doc_key',
        event_payload->>'namespace',
        NULLIF(event_payload->>'assigned_role', ''),
        COALESCE(event_payload->>'locked_by_human', 'false')::boolean,
        event_payload,
        NOW()
    ) RETURNING id INTO new_event_id;

    event_payload := event_payload || jsonb_build_object('id', new_event_id);
    PERFORM pg_notify('workspace_events', event_payload::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_slots_notify ON context_slots;
CREATE TRIGGER trg_slots_notify AFTER INSERT OR UPDATE ON context_slots
    FOR EACH ROW EXECUTE FUNCTION notify_workspace_change();

DROP TRIGGER IF EXISTS trg_tasks_notify ON collab_tasks;
CREATE TRIGGER trg_tasks_notify AFTER INSERT OR UPDATE ON collab_tasks
    FOR EACH ROW EXECUTE FUNCTION notify_workspace_change();

DROP TRIGGER IF EXISTS trg_docs_notify ON shared_documents;
CREATE TRIGGER trg_docs_notify AFTER INSERT OR UPDATE ON shared_documents
    FOR EACH ROW EXECUTE FUNCTION notify_workspace_change();

DROP TRIGGER IF EXISTS trg_timeline_notify ON timeline_streams;
CREATE TRIGGER trg_timeline_notify AFTER INSERT OR UPDATE ON timeline_streams
    FOR EACH ROW EXECUTE FUNCTION notify_workspace_change();

