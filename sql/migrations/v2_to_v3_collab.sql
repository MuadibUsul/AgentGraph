-- AgentGraph v2.0 -> v3.0 collaborative blackboard migration.
-- Idempotent. Preserves workspaces, slots, timeline and converts crystals/edges.
-- After this script, sql/init.sql is applied to fill any missing v3 tables/indexes.

BEGIN;

-- 1. Memory crystals -> shared documents --------------------------------------
DO $$
BEGIN
    IF to_regclass('memory_crystals') IS NOT NULL
       AND to_regclass('shared_documents') IS NULL THEN
        ALTER TABLE memory_crystals RENAME TO shared_documents;
    END IF;
END
$$;

ALTER TABLE shared_documents
    ADD COLUMN IF NOT EXISTS doc_key VARCHAR(256),
    ADD COLUMN IF NOT EXISTS title VARCHAR(256),
    ADD COLUMN IF NOT EXISTS version INT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS namespace VARCHAR(64) NOT NULL DEFAULT 'default',
    ADD COLUMN IF NOT EXISTS locked_by_human BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS human_note TEXT,
    ADD COLUMN IF NOT EXISTS updated_by VARCHAR(128),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;

-- The v2 metadata column becomes the v3 frontmatter property bag.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'shared_documents' AND column_name = 'metadata'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'shared_documents' AND column_name = 'frontmatter'
    ) THEN
        ALTER TABLE shared_documents RENAME COLUMN metadata TO frontmatter;
    END IF;
END
$$;

ALTER TABLE shared_documents
    ADD COLUMN IF NOT EXISTS frontmatter JSONB NOT NULL DEFAULT '{}'::jsonb;

UPDATE shared_documents SET doc_key = 'doc:' || id::text WHERE doc_key IS NULL;
UPDATE shared_documents
SET title = COALESCE(NULLIF(split_part(content, E'\n', 1), ''), 'Untitled Artifact')
WHERE title IS NULL OR title = '';
UPDATE shared_documents
SET updated_by = created_by
WHERE updated_by IS NULL;
UPDATE shared_documents
SET updated_at = created_at
WHERE updated_at IS NULL;
UPDATE shared_documents
SET frontmatter = '{}'::jsonb
WHERE frontmatter IS NULL;

ALTER TABLE shared_documents
    ALTER COLUMN doc_key SET NOT NULL,
    ALTER COLUMN title SET DEFAULT 'Untitled Artifact',
    ALTER COLUMN title SET NOT NULL,
    ALTER COLUMN updated_by SET NOT NULL,
    ALTER COLUMN updated_at SET DEFAULT NOW(),
    ALTER COLUMN updated_at SET NOT NULL;

-- Decay/salience/access bookkeeping is intentionally retired in v3.
ALTER TABLE shared_documents
    DROP COLUMN IF EXISTS salience_score,
    DROP COLUMN IF EXISTS decay_rate,
    DROP COLUMN IF EXISTS access_count,
    DROP COLUMN IF EXISTS last_accessed_at;

DO $$
BEGIN
    IF to_regclass('idx_crystals_workspace') IS NOT NULL
       AND to_regclass('idx_docs_workspace') IS NULL THEN
        ALTER INDEX idx_crystals_workspace RENAME TO idx_docs_workspace;
    END IF;
    IF to_regclass('idx_crystals_embedding') IS NOT NULL
       AND to_regclass('idx_docs_embedding') IS NULL THEN
        ALTER INDEX idx_crystals_embedding RENAME TO idx_docs_embedding;
    END IF;
END
$$;
DROP INDEX IF EXISTS idx_crystals_metadata;

-- 2. Context slots: human sovereignty + namespaces ----------------------------
ALTER TABLE context_slots
    ADD COLUMN IF NOT EXISTS schema_hint JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS namespace VARCHAR(64) NOT NULL DEFAULT 'default',
    ADD COLUMN IF NOT EXISTS locked_by_human BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS human_note TEXT;

-- 3. Timeline namespace --------------------------------------------------------
ALTER TABLE timeline_streams
    ADD COLUMN IF NOT EXISTS namespace VARCHAR(64) NOT NULL DEFAULT 'default';

-- 4. Causal edges -> workspace links ------------------------------------------
DO $$
BEGIN
    IF to_regclass('causal_edges') IS NOT NULL
       AND to_regclass('workspace_links') IS NULL THEN
        ALTER TABLE causal_edges RENAME TO workspace_links;
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
        WHERE t.typname = 'causal_relation' AND e.enumlabel = 'depends_on'
    ) THEN
        ALTER TYPE causal_relation ADD VALUE 'depends_on';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
        WHERE t.typname = 'causal_relation' AND e.enumlabel = 'references'
    ) THEN
        ALTER TYPE causal_relation ADD VALUE 'references';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
        WHERE t.typname = 'causal_relation' AND e.enumlabel = 'blocks'
    ) THEN
        ALTER TYPE causal_relation ADD VALUE 'blocks';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
        WHERE t.typname = 'causal_relation' AND e.enumlabel = 'produced_by'
    ) THEN
        ALTER TYPE causal_relation ADD VALUE 'produced_by';
    END IF;
END
$$;

DO $$
BEGIN
    IF to_regclass('idx_causal_source') IS NOT NULL
       AND to_regclass('idx_links_source') IS NULL THEN
        ALTER INDEX idx_causal_source RENAME TO idx_links_source;
    END IF;
    IF to_regclass('idx_causal_target') IS NOT NULL
       AND to_regclass('idx_links_target') IS NULL THEN
        ALTER INDEX idx_causal_target RENAME TO idx_links_target;
    END IF;
END
$$;

-- 5. Agent tokens --------------------------------------------------------------
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

-- 6. Task board ----------------------------------------------------------------
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

-- 7. Time-travel audit ---------------------------------------------------------
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

-- 8. New indexes on renamed tables --------------------------------------------
CREATE INDEX IF NOT EXISTS idx_docs_namespace
    ON shared_documents(workspace_id, namespace);
CREATE INDEX IF NOT EXISTS idx_docs_frontmatter
    ON shared_documents USING gin (frontmatter jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_slots_namespace
    ON context_slots(workspace_id, namespace);
CREATE INDEX IF NOT EXISTS idx_timeline_namespace
    ON timeline_streams(workspace_id, namespace);
CREATE INDEX IF NOT EXISTS idx_links_source
    ON workspace_links(workspace_id, source_id);
CREATE INDEX IF NOT EXISTS idx_links_target
    ON workspace_links(workspace_id, target_id);

-- 9. NOTIFY triggers -----------------------------------------------------------
CREATE OR REPLACE FUNCTION notify_workspace_change() RETURNS trigger AS $$
DECLARE
    row_data JSONB;
    event_payload JSONB;
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
        'version', row_data->>'version',
        'status', COALESCE(row_data->>'status', row_data->'frontmatter'->>'status', ''),
        'slot_key', row_data->>'slot_key',
        'task_key', row_data->>'task_key',
        'doc_key', row_data->>'doc_key',
        'namespace', COALESCE(row_data->>'namespace', 'default'),
        'assigned_role', COALESCE(row_data->>'assigned_role', ''),
        'locked_by_human', COALESCE(row_data->>'locked_by_human', 'false'),
        'timestamp', NOW()
    );
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

CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(32) PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO schema_migrations(version) VALUES ('3.0.0')
ON CONFLICT (version) DO NOTHING;

COMMIT;
