-- v3.21 token tree (PRD AG-ID-01, AG-ID-02, AG-ID-04, AG-ID-05).
--
-- * A real root token per workspace (kind=root): it only manages tokens.
--   Its secret (root_key_hash) is set when the workspace is created, or by an
--   operator rotation for existing workspaces.
-- * The workspace key keeps its token id (now workspaces.key_token_id) and
--   becomes a branch under the root, with the commander rights it always had.
-- * Agent tokens gain explicit scopes, split read/write namespaces, issuance
--   limits, a lineage array (root -> self) and revocation details.
--
-- Depth stays as stored since v3.9 (root 0, branch 1, child 2...); the API
-- reports the PRD numbering (branch 0, child >= 1).

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'workspaces' AND column_name = 'root_token_id')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'workspaces' AND column_name = 'key_token_id') THEN
        ALTER TABLE workspaces RENAME COLUMN root_token_id TO key_token_id;
    END IF;
END
$$;

ALTER TABLE workspaces
    ADD COLUMN IF NOT EXISTS root_token_id VARCHAR(40) NOT NULL DEFAULT agentgraph_new_id('tok'),
    ADD COLUMN IF NOT EXISTS root_key_hash VARCHAR(256),
    ADD COLUMN IF NOT EXISTS root_key_rotated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS max_token_depth INT NOT NULL DEFAULT 2
        CHECK (max_token_depth BETWEEN 1 AND 5),
    ADD COLUMN IF NOT EXISTS branch_token_quota INT NOT NULL DEFAULT 20
        CHECK (branch_token_quota BETWEEN 1 AND 10000),
    ADD COLUMN IF NOT EXISTS workspace_token_quota INT NOT NULL DEFAULT 100
        CHECK (workspace_token_quota BETWEEN 1 AND 100000);
CREATE UNIQUE INDEX IF NOT EXISTS uq_workspaces_root_token_id ON workspaces (root_token_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_workspaces_root_key_hash
    ON workspaces (root_key_hash) WHERE root_key_hash IS NOT NULL;

ALTER TABLE agent_tokens
    ADD COLUMN IF NOT EXISTS scopes TEXT[],
    ADD COLUMN IF NOT EXISTS read_namespaces TEXT[],
    ADD COLUMN IF NOT EXISTS write_namespaces TEXT[],
    ADD COLUMN IF NOT EXISTS can_issue BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS max_descendants INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS lineage VARCHAR(40)[],
    ADD COLUMN IF NOT EXISTS issued_by_token_id VARCHAR(40),
    ADD COLUMN IF NOT EXISTS issued_by_human UUID,
    ADD COLUMN IF NOT EXISTS agent_profile JSONB,
    ADD COLUMN IF NOT EXISTS revoke_reason TEXT,
    ADD COLUMN IF NOT EXISTS revoked_ancestor VARCHAR(40),
    ADD COLUMN IF NOT EXISTS expiring_notified_at TIMESTAMPTZ;

-- Existing tokens were minted from the console: branches of the new root.
UPDATE agent_tokens t
SET parent_token_id = w.root_token_id,
    root_token_id = w.root_token_id,
    depth = 1,
    lineage = ARRAY[w.root_token_id, t.canonical_id]::VARCHAR(40)[]
FROM workspaces w
WHERE w.id = t.workspace_id
  AND t.lineage IS NULL
  AND (t.parent_token_id IS NULL OR t.parent_token_id = w.key_token_id);

CREATE INDEX IF NOT EXISTS idx_agent_tokens_lineage ON agent_tokens USING GIN (lineage);
CREATE INDEX IF NOT EXISTS idx_agent_tokens_canonical ON agent_tokens (canonical_id);

-- ------------------------------------------------ directory (AG-ID-06)
-- One entry per agent identity. Its authority comes from the token tree: an
-- entry binds one or more tokens (token_ids) and records the signatures they
-- write under, so legacy updated_by strings can be shown as agent:<agent_id>.
CREATE TABLE IF NOT EXISTS agent_directory (
    principal_id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('prn'),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    agent_id VARCHAR(40) NOT NULL CHECK (agent_id ~ '^[a-z][a-z0-9-]{2,39}$'),
    display_name VARCHAR(64) NOT NULL,
    role VARCHAR(32),
    capabilities TEXT[] NOT NULL DEFAULT '{}',
    description VARCHAR(500),
    owner_human UUID,
    status VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'idle', 'offline', 'retired')),
    status_note VARCHAR(200),
    trust_level VARCHAR(16) NOT NULL DEFAULT 'internal'
        CHECK (trust_level IN ('internal', 'external')),
    token_ids TEXT[] NOT NULL DEFAULT '{}',
    signatures TEXT[] NOT NULL DEFAULT '{}',
    legacy_names TEXT[] NOT NULL DEFAULT '{}',
    registered_by VARCHAR(256) NOT NULL,
    version INT NOT NULL DEFAULT 1,
    last_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, agent_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_directory_ws ON agent_directory (workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_agent_directory_tokens ON agent_directory USING GIN (token_ids);

-- Renamed aliases redirect forever and are never handed out again.
CREATE TABLE IF NOT EXISTS agent_aliases (
    tenant_id VARCHAR(40) NOT NULL,
    alias VARCHAR(40) NOT NULL,
    principal_id VARCHAR(40) NOT NULL REFERENCES agent_directory(principal_id) ON DELETE CASCADE,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, alias)
);

DO $$
DECLARE
    target_table TEXT;
BEGIN
    FOREACH target_table IN ARRAY ARRAY['agent_directory', 'agent_aliases'] LOOP
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
