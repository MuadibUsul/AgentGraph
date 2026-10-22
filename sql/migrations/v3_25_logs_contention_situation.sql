-- v3.25 log documents, hot-document protection, human edits and situation
-- alerts (PRD Phase 2 R2.3: AG-ST-07/08/09/14, AG-TC-03, AG-TC-05).
--
-- * doc_type log: append-only entries with a per-document gapless seq;
--   embeddings are rebuilt per batch of entries, not per append.
-- * doc_contention: version conflicts per document in the last 10 seconds;
--   five or more make the document contended for a minute.
-- * last_human_edit / human_edited: a person's save is marked, trust unchanged.
-- * principal_metrics (per minute) and situation_alerts (anomaly rules).
-- * agentgraph_try_timestamptz: frontmatter date comparisons that never throw.

ALTER TABLE shared_documents
    ADD COLUMN IF NOT EXISTS doc_type VARCHAR(16) NOT NULL DEFAULT 'collaborative'
        CHECK (doc_type IN ('collaborative', 'log')),
    ADD COLUMN IF NOT EXISTS log_seq BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS log_unindexed INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS log_unindexed_since TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS log_index_batches INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_human_edit JSONB;
ALTER TABLE document_versions
    ADD COLUMN IF NOT EXISTS human_edited BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS log_entries (
    entry_id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('ent'),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    document_id UUID NOT NULL REFERENCES shared_documents(id) ON DELETE CASCADE,
    seq BIGINT NOT NULL,
    body TEXT NOT NULL,
    frontmatter JSONB NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key VARCHAR(128),
    author VARCHAR(256) NOT NULL,
    stamp JSONB,
    tainted BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (document_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_log_entries_idem
    ON log_entries (document_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

-- No foreign key to shared_documents: conflicts are recorded from a separate
-- session while the conflicting request still holds the document row lock.
CREATE TABLE IF NOT EXISTS doc_contention (
    document_id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    conflicts TIMESTAMPTZ[] NOT NULL DEFAULT '{}',
    conflicts_total BIGINT NOT NULL DEFAULT 0,
    contended_until TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS principal_metrics (
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    principal_key VARCHAR(256) NOT NULL,
    minute TIMESTAMPTZ NOT NULL,
    requests INT NOT NULL DEFAULT 0,
    reads INT NOT NULL DEFAULT 0,
    writes INT NOT NULL DEFAULT 0,
    noop_writes INT NOT NULL DEFAULT 0,
    conflicts INT NOT NULL DEFAULT 0,
    rate_limited INT NOT NULL DEFAULT 0,
    errors_5xx INT NOT NULL DEFAULT 0,
    PRIMARY KEY (workspace_id, principal_key, minute)
);
CREATE INDEX IF NOT EXISTS idx_principal_metrics_minute ON principal_metrics (minute);

CREATE TABLE IF NOT EXISTS situation_alerts (
    id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('alr'),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    rule VARCHAR(32) NOT NULL,
    severity VARCHAR(8) NOT NULL CHECK (severity IN ('high', 'medium', 'low')),
    subject_kind VARCHAR(16) NOT NULL,
    subject_ref VARCHAR(256) NOT NULL,
    state VARCHAR(10) NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'acked', 'resolved')),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_notified_at TIMESTAMPTZ,
    notify_count INT NOT NULL DEFAULT 0,
    acked_by VARCHAR(256),
    acked_at TIMESTAMPTZ,
    note TEXT,
    resolved_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_situation_alerts_live
    ON situation_alerts (workspace_id, rule, subject_ref) WHERE state <> 'resolved';

DO $$
DECLARE
    target_table TEXT;
BEGIN
    FOREACH target_table IN ARRAY ARRAY['log_entries', 'doc_contention', 'principal_metrics',
                                        'situation_alerts'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_derive_tenant ON %I', target_table);
        EXECUTE format(
            'CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id '
            'ON %I FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant()', target_table);
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', target_table);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', target_table);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', target_table);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I '
            'USING (tenant_id = ANY(agentgraph_tenant_scope()) '
            'AND workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope()) '
            'AND workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)',
            target_table);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO agentgraph_app',
                       target_table);
    END LOOP;
END
$$;

CREATE OR REPLACE FUNCTION agentgraph_try_timestamptz(value TEXT) RETURNS TIMESTAMPTZ AS $$
BEGIN
    IF value IS NULL OR value !~ '^\d{4}-\d{2}-\d{2}' THEN
        RETURN NULL;
    END IF;
    RETURN value::timestamptz;
EXCEPTION WHEN others THEN
    RETURN NULL;
END;
$$ LANGUAGE plpgsql IMMUTABLE;
GRANT EXECUTE ON FUNCTION agentgraph_try_timestamptz(TEXT) TO agentgraph_app;
