-- AgentGraph v3.0.x production hardening:
--   * document content hashes / verification / trust metadata
--   * immutable document_versions
--   * lease_epoch fencing tokens
--   * transactional workspace_events outbox + replayable SSE
-- Idempotent; safe on a fresh v3 schema.

BEGIN;

ALTER TABLE shared_documents
    ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64),
    ADD COLUMN IF NOT EXISTS verification_status VARCHAR(32) NOT NULL DEFAULT 'DRAFT'
        CHECK (verification_status IN ('DRAFT', 'PENDING_REVIEW', 'VERIFIED', 'DEPRECATED')),
    ADD COLUMN IF NOT EXISTS trust_level VARCHAR(16) NOT NULL DEFAULT 'UNTRUSTED'
        CHECK (trust_level IN ('TRUSTED', 'UNTRUSTED')),
    ADD COLUMN IF NOT EXISTS verified_version INT,
    ADD COLUMN IF NOT EXISTS verified_hash VARCHAR(64),
    ADD COLUMN IF NOT EXISTS embedding_model VARCHAR(128);

ALTER TABLE context_slots
    ADD COLUMN IF NOT EXISTS lease_epoch INT NOT NULL DEFAULT 0;

ALTER TABLE collab_tasks
    ADD COLUMN IF NOT EXISTS lease_epoch INT NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_docs_verification
    ON shared_documents(workspace_id, verification_status);

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

COMMIT;
