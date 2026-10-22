-- v3.18 console onboarding progress (getting-started checklist).
-- One row per human user and workspace: steps the user ticked or skipped by
-- hand and whether the checklist was closed. Steps the console can observe
-- (token minted, orientation called, first content, first review) are
-- derived from workspace data on read and never stored here.

CREATE TABLE IF NOT EXISTS onboarding_progress (
    user_id UUID NOT NULL REFERENCES human_users(id) ON DELETE CASCADE,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    completed JSONB NOT NULL DEFAULT '[]'::jsonb,
    skipped JSONB NOT NULL DEFAULT '[]'::jsonb,
    dismissed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, workspace_id)
);

DROP TRIGGER IF EXISTS trg_derive_tenant ON onboarding_progress;
CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
    ON onboarding_progress FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();
ALTER TABLE onboarding_progress ENABLE ROW LEVEL SECURITY;
ALTER TABLE onboarding_progress FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON onboarding_progress;
CREATE POLICY tenant_isolation ON onboarding_progress
    USING (tenant_id = ANY(agentgraph_tenant_scope())
           AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope())
           AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON onboarding_progress TO agentgraph_app;
