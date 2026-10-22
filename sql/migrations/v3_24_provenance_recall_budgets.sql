-- v3.24 provenance, recall and token budgets (PRD Phase 2 R2.2: AG-TC-01,
-- AG-TC-02, AG-ST-16).
--
-- * Every write records where it came from in doc_derivations (declared
--   derived_from, the writer's 15-minute read window, the context of the task
--   it holds), tainted or not: recall walks these edges downstream.
-- * provenance_taint keeps the write-time taint sources (external principal,
--   tainted inputs, declared origin, recall) next to the reference-based ones.
-- * token_reads is the per-token read window (platform-only, short-lived).
-- * recalls / recall_nodes: a recall and the tree of versions it reached.
-- * token_budgets / token_usage: per-token daily limits; usage is added to
--   the token and every ancestor up to the branch.

ALTER TABLE shared_documents
    ADD COLUMN IF NOT EXISTS provenance_taint JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS recall_severity VARCHAR(8)
        CHECK (recall_severity IS NULL OR recall_severity IN ('warn', 'block')),
    ADD COLUMN IF NOT EXISTS content_origin VARCHAR(16) NOT NULL DEFAULT 'internal';
ALTER TABLE document_versions
    ADD COLUMN IF NOT EXISTS content_origin VARCHAR(16) NOT NULL DEFAULT 'internal';
ALTER TABLE collab_tasks
    ADD COLUMN IF NOT EXISTS recall_flags JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE external_actions
    ADD COLUMN IF NOT EXISTS tainted_inputs JSONB NOT NULL DEFAULT '[]'::jsonb;

CREATE TABLE IF NOT EXISTS doc_derivations (
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    from_document_id UUID NOT NULL REFERENCES shared_documents(id) ON DELETE CASCADE,
    from_version INT NOT NULL,
    to_document_id UUID NOT NULL REFERENCES shared_documents(id) ON DELETE CASCADE,
    to_version INT NOT NULL,
    kind VARCHAR(16) NOT NULL CHECK (kind IN ('declared', 'read_window', 'task_context')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (from_document_id, from_version, to_document_id, to_version)
);
CREATE INDEX IF NOT EXISTS idx_doc_derivations_from
    ON doc_derivations (from_document_id, from_version);
CREATE INDEX IF NOT EXISTS idx_doc_derivations_to ON doc_derivations (to_document_id, to_version);

CREATE TABLE IF NOT EXISTS recalls (
    id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('rcl'),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    document_id UUID NOT NULL REFERENCES shared_documents(id) ON DELETE CASCADE,
    versions INT[] NOT NULL,
    reason TEXT NOT NULL,
    severity VARCHAR(8) NOT NULL CHECK (severity IN ('warn', 'block')),
    max_depth INT NOT NULL,
    depth_reached INT NOT NULL DEFAULT 0,
    truncated BOOLEAN NOT NULL DEFAULT FALSE,
    boundary JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_by VARCHAR(256) NOT NULL,
    status VARCHAR(8) NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS recall_nodes (
    recall_id VARCHAR(40) NOT NULL REFERENCES recalls(id) ON DELETE CASCADE,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    document_id UUID NOT NULL REFERENCES shared_documents(id) ON DELETE CASCADE,
    version INT NOT NULL,
    hop INT NOT NULL,
    via_document_id UUID,
    via_version INT,
    status VARCHAR(10) NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'resolved', 'dismissed')),
    resolved_by VARCHAR(256),
    resolved_at TIMESTAMPTZ,
    PRIMARY KEY (recall_id, document_id, version)
);
CREATE INDEX IF NOT EXISTS idx_recall_nodes_doc ON recall_nodes (document_id, status);

DO $$
DECLARE
    target_table TEXT;
BEGIN
    FOREACH target_table IN ARRAY ARRAY['doc_derivations', 'recalls', 'recall_nodes'] LOOP
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

-- The read window: written by the platform session after a read, read back
-- by the writer's own (row-scoped) session.
CREATE TABLE IF NOT EXISTS token_reads (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    reader VARCHAR(256) NOT NULL,
    document_id UUID NOT NULL REFERENCES shared_documents(id) ON DELETE CASCADE,
    version INT NOT NULL,
    tainted BOOLEAN NOT NULL DEFAULT FALSE,
    read_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_token_reads_reader ON token_reads (workspace_id, reader, read_at);
DROP TRIGGER IF EXISTS trg_derive_tenant ON token_reads;
CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
    ON token_reads FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();
ALTER TABLE token_reads ENABLE ROW LEVEL SECURITY;
ALTER TABLE token_reads FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON token_reads;
CREATE POLICY tenant_isolation ON token_reads
    USING (tenant_id = ANY(agentgraph_tenant_scope())
           AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope())
           AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
GRANT SELECT ON token_reads TO agentgraph_app;

-- Platform-only: budgets and their daily counters.
CREATE TABLE IF NOT EXISTS token_budgets (
    token_id VARCHAR(40) PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    writes BIGINT CHECK (writes >= 0),
    embedding_tokens BIGINT CHECK (embedding_tokens >= 0),
    queries BIGINT CHECK (queries >= 0),
    updated_by VARCHAR(256),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS token_usage (
    token_id VARCHAR(40) NOT NULL,
    day DATE NOT NULL,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    writes BIGINT NOT NULL DEFAULT 0,
    embedding_tokens BIGINT NOT NULL DEFAULT 0,
    queries BIGINT NOT NULL DEFAULT 0,
    warned TEXT[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (token_id, day)
);
DROP TRIGGER IF EXISTS trg_derive_tenant ON token_budgets;
CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
    ON token_budgets FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();
DROP TRIGGER IF EXISTS trg_derive_tenant ON token_usage;
CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
    ON token_usage FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();
REVOKE ALL ON token_budgets FROM agentgraph_app;
REVOKE ALL ON token_usage FROM agentgraph_app;
