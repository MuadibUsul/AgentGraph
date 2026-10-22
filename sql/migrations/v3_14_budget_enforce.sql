-- v3.14 cost control enforcement (PRD AG-ST-15 R1.1).
-- * budget_backoff: server-enforced Retry-After per (workspace, principal,
--   dimension); early retries double the wait and can trip a circuit.
-- * budget_limits: admin overrides per workspace or tenant. A tenant row caps
--   the summed embedding usage of every workspace in its subtree. Read only by
--   the platform (owner role), so it carries no row security for agents.

CREATE TABLE IF NOT EXISTS budget_backoff (
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    principal_key VARCHAR(256) NOT NULL,
    dimension VARCHAR(32) NOT NULL,
    retry_until TIMESTAMPTZ NOT NULL,
    penalty_seconds INT NOT NULL,
    violations INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (workspace_id, principal_key, dimension)
);

DROP TRIGGER IF EXISTS trg_derive_tenant ON budget_backoff;
CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
    ON budget_backoff FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();
ALTER TABLE budget_backoff ENABLE ROW LEVEL SECURITY;
ALTER TABLE budget_backoff FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON budget_backoff;
CREATE POLICY tenant_isolation ON budget_backoff
    USING (tenant_id = ANY(agentgraph_tenant_scope())
           AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope())
           AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON budget_backoff TO agentgraph_app;

CREATE TABLE IF NOT EXISTS budget_limits (
    scope_kind VARCHAR(16) NOT NULL CHECK (scope_kind IN ('workspace', 'tenant')),
    scope_id VARCHAR(64) NOT NULL,
    embedding_tokens_per_day BIGINT CHECK (embedding_tokens_per_day >= 0),
    write_rate_per_minute INT CHECK (write_rate_per_minute >= 1),
    write_burst INT CHECK (write_burst >= 1),
    query_rate_per_minute INT CHECK (query_rate_per_minute >= 1),
    updated_by VARCHAR(256),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (scope_kind, scope_id)
);
REVOKE ALL ON budget_limits FROM agentgraph_app;
