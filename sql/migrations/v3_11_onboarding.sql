-- v3.11 onboarding (PRD AG-OB-01/02).
-- * "system" becomes the platform namespace; documents that used it as an
--   ordinary namespace move to "system-legacy" (and the operator is told in
--   the release notes).
-- * constitution_acks: which constitution version each principal has read.
-- * access_audit: append-only audit of reads such as agent.oriented.

UPDATE shared_documents
SET namespace = 'system-legacy'
WHERE namespace = 'system' AND doc_key NOT LIKE 'system/%';

INSERT INTO namespaces (workspace_id, tenant_id, namespace, purpose, created_by, auto_registered)
SELECT DISTINCT d.workspace_id, d.tenant_id, 'system-legacy',
       'documents that used "system" before it became the platform namespace',
       'migration', TRUE
FROM shared_documents d
WHERE d.namespace = 'system-legacy'
ON CONFLICT (workspace_id, namespace) DO NOTHING;

CREATE TABLE IF NOT EXISTS constitution_acks (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    principal_key VARCHAR(256) NOT NULL,
    version INT NOT NULL,
    acked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, principal_key)
);

CREATE TABLE IF NOT EXISTS access_audit (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    action VARCHAR(64) NOT NULL,
    stamp JSONB,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_access_audit_workspace
    ON access_audit (tenant_id, workspace_id, created_at DESC);

DO $$
DECLARE
    target_table TEXT;
BEGIN
    FOREACH target_table IN ARRAY ARRAY['constitution_acks', 'access_audit'] LOOP
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
        EXECUTE format('GRANT SELECT, INSERT, UPDATE ON %I TO agentgraph_app', target_table);
    END LOOP;
END
$$;

-- access_audit is append-only: stamp is fixed at write time.
DROP TRIGGER IF EXISTS trg_lineage_stamp ON access_audit;
CREATE TRIGGER trg_lineage_stamp BEFORE INSERT OR UPDATE ON access_audit
    FOR EACH ROW EXECUTE FUNCTION agentgraph_stamp_entry();
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO agentgraph_app;
