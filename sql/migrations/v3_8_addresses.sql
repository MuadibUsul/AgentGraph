-- v3.8 address specification (PRD AG-ST-17, day-zero contract 1).
--
-- * Canonical ids (<type>_<ULID>) for workspaces, documents, tasks, slots,
--   agent tokens and human principals: opaque, never changed, never reused.
-- * alias_redirects: renamed aliases resolve permanently to the same object.
-- * tombstones: purged objects leave a content-free record; their ids and
--   aliases are never assigned again (410 gone / 409 alias_tombstoned).
-- * namespaces: register-before-use registry, backfilled from existing data.
-- * document_refs: declared {"$ref"} edges from document frontmatter.

-- -------------------------------------------------------- canonical ids
DO $$
DECLARE
    spec RECORD;
BEGIN
    FOR spec IN
        SELECT * FROM (VALUES
            ('workspaces', 'ws'),
            ('shared_documents', 'doc'),
            ('collab_tasks', 'task'),
            ('context_slots', 'slot'),
            ('agent_tokens', 'tok'),
            ('human_users', 'prn')
        ) AS t(table_name, prefix)
    LOOP
        -- A volatile default is evaluated per existing row on ADD COLUMN.
        EXECUTE format(
            'ALTER TABLE %I ADD COLUMN IF NOT EXISTS canonical_id VARCHAR(40) '
            'NOT NULL DEFAULT agentgraph_new_id(%L)',
            spec.table_name, spec.prefix
        );
        EXECUTE format(
            'CREATE UNIQUE INDEX IF NOT EXISTS %I ON %I (canonical_id)',
            'uq_' || spec.table_name || '_canonical', spec.table_name
        );
    END LOOP;
END
$$;

-- Keys that predate the address grammar stay readable as legacy aliases.
ALTER TABLE shared_documents
    ADD COLUMN IF NOT EXISTS alias_legacy BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE shared_documents
SET alias_legacy = NOT (
    namespace ~ '^[a-z][a-z0-9-]{1,31}$'
    AND length(doc_key) <= 256
    AND doc_key !~ '(^|/)\.{1,2}(/|$)'
    AND array_length(string_to_array(doc_key, '/'), 1) <= 8
    AND NOT EXISTS (
        SELECT 1 FROM unnest(string_to_array(doc_key, '/')) AS segment
        WHERE segment !~ '^[a-z0-9][a-z0-9._-]{0,63}$'
    )
);

-- ------------------------------------------------------- alias redirects
CREATE TABLE IF NOT EXISTS alias_redirects (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    object_type VARCHAR(16) NOT NULL CHECK (object_type IN ('document', 'task')),
    old_alias VARCHAR(512) NOT NULL,
    target_canonical_id VARCHAR(40) NOT NULL,
    renamed_by VARCHAR(256) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, object_type, old_alias)
);

-- ------------------------------------------------------------ tombstones
CREATE TABLE IF NOT EXISTS tombstones (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    object_type VARCHAR(16) NOT NULL,
    canonical_id VARCHAR(40) NOT NULL UNIQUE,
    alias VARCHAR(512) NOT NULL,
    namespace VARCHAR(64),
    last_version INT,
    last_content_hash VARCHAR(128),
    purged_by VARCHAR(256) NOT NULL,
    purged_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reason TEXT,
    UNIQUE (workspace_id, object_type, alias)
);

-- ------------------------------------------------------------ namespaces
CREATE TABLE IF NOT EXISTS namespaces (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    namespace VARCHAR(64) NOT NULL,
    purpose TEXT,
    owner VARCHAR(256),
    doc_type_default VARCHAR(16) NOT NULL DEFAULT 'collaborative'
        CHECK (doc_type_default IN ('collaborative', 'log')),
    write_policy VARCHAR(32) NOT NULL DEFAULT 'open',
    auto_registered BOOLEAN NOT NULL DEFAULT FALSE,
    created_by VARCHAR(256) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, namespace)
);

-- Existing namespaces are registered by the migration (PRD AG-ST-17 #7).
INSERT INTO namespaces (workspace_id, tenant_id, namespace, purpose, created_by, auto_registered)
SELECT DISTINCT w.id, w.tenant_id, source.namespace,
       'registered by the v3.8 migration from existing data', 'migration', TRUE
FROM (
    SELECT workspace_id, namespace FROM shared_documents
    UNION SELECT workspace_id, namespace FROM context_slots
    UNION SELECT workspace_id, namespace FROM collab_tasks
    UNION SELECT id, 'default' FROM workspaces
) AS source
JOIN workspaces w ON w.id = source.workspace_id
ON CONFLICT (workspace_id, namespace) DO NOTHING;

-- Every workspace has the implicit "default" namespace from birth.
CREATE OR REPLACE FUNCTION agentgraph_workspace_default_namespace() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
    INSERT INTO namespaces (workspace_id, tenant_id, namespace, purpose, created_by)
    VALUES (NEW.id, NEW.tenant_id, 'default', 'default namespace', 'system')
    ON CONFLICT (workspace_id, namespace) DO NOTHING;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_workspace_default_namespace ON workspaces;
CREATE TRIGGER trg_workspace_default_namespace
    AFTER INSERT ON workspaces
    FOR EACH ROW EXECUTE FUNCTION agentgraph_workspace_default_namespace();

-- -------------------------------------------------------- document refs
CREATE TABLE IF NOT EXISTS document_refs (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    source_document_id UUID NOT NULL REFERENCES shared_documents(id) ON DELETE CASCADE,
    source_version INT NOT NULL,
    field_path VARCHAR(512) NOT NULL,
    target_canonical_id VARCHAR(40),
    target_ref VARCHAR(512) NOT NULL,
    kind VARCHAR(16) NOT NULL DEFAULT 'declared',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_document_refs_source
    ON document_refs (tenant_id, source_document_id);
CREATE INDEX IF NOT EXISTS idx_document_refs_target
    ON document_refs (tenant_id, target_canonical_id);

-- ------------------------------- tenant derivation and row security
DO $$
DECLARE
    target_table TEXT;
    tables TEXT[] := ARRAY['alias_redirects', 'tombstones', 'namespaces', 'document_refs'];
BEGIN
    FOREACH target_table IN ARRAY tables LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_derive_tenant ON %I', target_table);
        EXECUTE format(
            'CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id '
            'ON %I FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant()',
            target_table
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
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO agentgraph_app', target_table
        );
    END LOOP;
END
$$;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO agentgraph_app;
GRANT EXECUTE ON FUNCTION agentgraph_new_id(TEXT) TO agentgraph_app;
