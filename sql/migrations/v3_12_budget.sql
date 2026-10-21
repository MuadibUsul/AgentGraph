-- v3.12 budget metering (PRD AG-ST-15, shadow mode first).
-- * budget_buckets: token-bucket state per (workspace, principal, dimension).
-- * budget_circuits: runaway-loop circuit breakers a human can clear.

CREATE TABLE IF NOT EXISTS budget_buckets (
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    bucket_key VARCHAR(256) NOT NULL,
    tokens DOUBLE PRECISION NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (workspace_id, bucket_key)
);

CREATE TABLE IF NOT EXISTS budget_circuits (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    principal_key VARCHAR(256) NOT NULL,
    reason VARCHAR(64) NOT NULL,
    doc_key VARCHAR(512),
    opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reopen_at TIMESTAMPTZ NOT NULL,
    cleared_by VARCHAR(256),
    cleared_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_budget_circuits_open
    ON budget_circuits (tenant_id, workspace_id, principal_key, reopen_at);
CREATE INDEX IF NOT EXISTS idx_usage_ledger_category_time
    ON usage_ledger (tenant_id, workspace_id, category, created_at);
CREATE INDEX IF NOT EXISTS idx_document_versions_recent
    ON document_versions (document_id, created_at);

DO $$
DECLARE
    target_table TEXT;
BEGIN
    FOREACH target_table IN ARRAY ARRAY['budget_buckets', 'budget_circuits'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_derive_tenant ON %I', target_table);
        EXECUTE format(
            'CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id '
            'ON %I FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant()', target_table
        );
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', target_table);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', target_table);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', target_table);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING '
            '(tenant_id = ANY(agentgraph_tenant_scope()) AND '
            'workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid) '
            'WITH CHECK '
            '(tenant_id = ANY(agentgraph_tenant_scope()) AND '
            'workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)',
            target_table
        );
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO agentgraph_app', target_table);
    END LOOP;
END
$$;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO agentgraph_app;
