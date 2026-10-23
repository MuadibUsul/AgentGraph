-- v3.35 Operator privacy: the operator sees counts and changes, never content.
--
-- * workspaces.operator_managed: TRUE only for workspaces the operator created
--   itself (admin API, CLI, platform evaluation). Every other workspace (self-
--   serve signups, organisation children) is private: the ADMIN_TOKEN reaches
--   its counts, usage and lifecycle, not its documents, tasks, audit or keys.
--   Existing workspaces start private; an owner can make an operator-managed
--   workspace private, never the other way round.
-- * support_access_grants: the one way in. An owner opens a time-limited
--   window (read-only unless they allow changes); every operator request in
--   it is written to access_audit, which the owner reads in the console.

ALTER TABLE workspaces
    ADD COLUMN IF NOT EXISTS operator_managed BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS support_access_grants (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    granted_by UUID NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    allow_write BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    revoked_by UUID,
    requests INT NOT NULL DEFAULT 0,
    last_request_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_support_access_workspace
    ON support_access_grants (workspace_id, expires_at DESC);

DROP TRIGGER IF EXISTS trg_derive_tenant ON support_access_grants;
CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
    ON support_access_grants FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();
ALTER TABLE support_access_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE support_access_grants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON support_access_grants;
CREATE POLICY tenant_isolation ON support_access_grants
    USING (tenant_id = ANY(agentgraph_tenant_scope())
           AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope())
           AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON support_access_grants TO agentgraph_app;
