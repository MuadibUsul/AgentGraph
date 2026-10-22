-- v3.13 asynchronous indexing (PRD AG-ST-13).
-- One row per document is both its index status and its embedding job:
-- writes set pending_version; the worker embeds only the latest version
-- (coalescing) and records indexed_version. Kept apart from shared_documents
-- so indexing never rewrites the writer's lineage stamp or emits doc events.

CREATE TABLE IF NOT EXISTS document_index (
    document_id UUID PRIMARY KEY REFERENCES shared_documents(id) ON DELETE CASCADE,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    state VARCHAR(16) NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'indexed', 'failed', 'deferred')),
    pending_version INT,
    indexed_version INT,
    attempts INT NOT NULL DEFAULT 0,
    superseded INT NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ,
    last_error TEXT,
    enqueued_at TIMESTAMPTZ,
    indexed_at TIMESTAMPTZ,
    last_lag_ms BIGINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_document_index_queue
    ON document_index (state, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_document_index_workspace
    ON document_index (tenant_id, workspace_id, state);

-- Existing documents were indexed synchronously when written.
INSERT INTO document_index (document_id, workspace_id, tenant_id, state, indexed_version, indexed_at)
SELECT id, workspace_id, tenant_id, 'indexed', version, updated_at FROM shared_documents
ON CONFLICT (document_id) DO NOTHING;

DROP TRIGGER IF EXISTS trg_derive_tenant ON document_index;
CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
    ON document_index FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();
ALTER TABLE document_index ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_index FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON document_index;
CREATE POLICY tenant_isolation ON document_index
    USING (tenant_id = ANY(agentgraph_tenant_scope())
           AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope())
           AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON document_index TO agentgraph_app;
