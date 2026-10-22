-- v3.19 agent onboarding over MCP (four protocol steps, never a gate).
-- One row per agent token in a workspace, keyed by the token's canonical id
-- (principal_key, never the secret). Steps: 1 get_orientation, 2 read and
-- ack_constitution, 3 read the namespace readme, 4 the role's first read and
-- first action. completed_at marks state "active"; hints stop from then on.

CREATE TABLE IF NOT EXISTS agent_onboarding (
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    principal_key VARCHAR(256) NOT NULL,
    token_id VARCHAR(40),
    agent_signature VARCHAR(128) NOT NULL,
    role VARCHAR(32) NOT NULL,
    state VARCHAR(32) NOT NULL DEFAULT 'new' CHECK (state IN (
        'new', 'oriented', 'needs_ack', 'needs_namespace_readme',
        'needs_first_action', 'active')),
    oriented_at TIMESTAMPTZ,
    constitution_read_at TIMESTAMPTZ,
    constitution_version_acked INT,
    acked_at TIMESTAMPTZ,
    namespace_readme_read_at TIMESTAMPTZ,
    readme_namespace VARCHAR(64),
    first_read_at TIMESTAMPTZ,
    first_task_at TIMESTAMPTZ,
    first_document_at TIMESTAMPTZ,
    first_review_at TIMESTAMPTZ,
    first_action_kind VARCHAR(32),
    completed_at TIMESTAMPTZ,
    last_prompted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (workspace_id, principal_key)
);

CREATE INDEX IF NOT EXISTS idx_agent_onboarding_state
    ON agent_onboarding (tenant_id, workspace_id, state);

DROP TRIGGER IF EXISTS trg_derive_tenant ON agent_onboarding;
CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
    ON agent_onboarding FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();
ALTER TABLE agent_onboarding ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_onboarding FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON agent_onboarding;
CREATE POLICY tenant_isolation ON agent_onboarding
    USING (tenant_id = ANY(agentgraph_tenant_scope())
           AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope())
           AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON agent_onboarding TO agentgraph_app;

-- Tokens that already worked here resume where they are. A token that was
-- oriented and has written or worked tasks is treated as onboarded (active),
-- so existing agents are not nudged; others resume at their next step.
INSERT INTO agent_onboarding (
    workspace_id, tenant_id, principal_key, token_id, agent_signature, role, state,
    oriented_at, constitution_version_acked, acked_at, first_document_at, first_task_at,
    first_action_kind, completed_at
)
SELECT
    t.workspace_id, t.tenant_id, t.canonical_id, t.canonical_id, t.agent_signature,
    t.role_scope::text, 'new',
    (SELECT MIN(a.created_at) FROM access_audit a
     WHERE a.workspace_id = t.workspace_id AND a.action = 'agent.oriented'
       AND a.details->>'principal' = t.agent_signature),
    (SELECT MAX(c.version) FROM constitution_acks c
     WHERE c.workspace_id = t.workspace_id AND c.principal_key = t.canonical_id),
    (SELECT MAX(c.acked_at) FROM constitution_acks c
     WHERE c.workspace_id = t.workspace_id AND c.principal_key = t.canonical_id),
    (SELECT MIN(d.created_at) FROM shared_documents d
     WHERE d.workspace_id = t.workspace_id AND d.created_by = t.agent_signature),
    (SELECT MIN(k.created_at) FROM collab_tasks k
     WHERE k.workspace_id = t.workspace_id
       AND (k.created_by = t.agent_signature OR k.claimed_by = t.agent_signature)),
    NULL, NULL
FROM agent_tokens t
ON CONFLICT (workspace_id, principal_key) DO NOTHING;

UPDATE agent_onboarding
SET completed_at = NOW(), state = 'active',
    first_action_kind = CASE WHEN first_task_at IS NOT NULL THEN 'task' ELSE 'document' END
WHERE completed_at IS NULL AND oriented_at IS NOT NULL
  AND (first_document_at IS NOT NULL OR first_task_at IS NOT NULL);

UPDATE agent_onboarding
SET state = CASE
    WHEN oriented_at IS NULL THEN 'new'
    WHEN constitution_version_acked IS NULL THEN 'oriented'
    ELSE 'needs_namespace_readme' END
WHERE completed_at IS NULL;
