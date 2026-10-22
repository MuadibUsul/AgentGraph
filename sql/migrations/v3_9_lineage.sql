-- v3.9 lineage (PRD AG-ID-03, day-zero contract 4).
--
-- * Token lineage fields; the workspace key is the workspace's root token.
-- * grant_snapshots: immutable record of what a token was granted at issue.
-- * Lineage stamps: the authenticated request publishes its stamp in the
--   transaction-local setting app.lineage_stamp; triggers copy it onto every
--   document, version, task, slot, proposal, action, audit row, event,
--   timeline entry and usage record. Stamps are fixed at write time, so a
--   later revocation or expiry never rewrites history.

-- --------------------------------------------------------- token lineage
ALTER TABLE workspaces
    ADD COLUMN IF NOT EXISTS root_token_id VARCHAR(40) NOT NULL DEFAULT agentgraph_new_id('tok');
CREATE UNIQUE INDEX IF NOT EXISTS uq_workspaces_root_token ON workspaces (root_token_id);

ALTER TABLE agent_tokens
    ADD COLUMN IF NOT EXISTS parent_token_id VARCHAR(40),
    ADD COLUMN IF NOT EXISTS root_token_id VARCHAR(40),
    ADD COLUMN IF NOT EXISTS depth INT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS issued_by_agent_id VARCHAR(256),
    ADD COLUMN IF NOT EXISTS purpose TEXT;

-- Existing branch tokens hang directly off their workspace's root token.
UPDATE agent_tokens t
SET parent_token_id = w.root_token_id,
    root_token_id = w.root_token_id,
    depth = 1,
    issued_by_agent_id = COALESCE(t.issued_by_agent_id, 'workspace_root')
FROM workspaces w
WHERE w.id = t.workspace_id AND t.root_token_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_agent_tokens_parent ON agent_tokens (tenant_id, parent_token_id);

-- ------------------------------------------------------- grant snapshots
CREATE TABLE IF NOT EXISTS grant_snapshots (
    id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('gs'),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    token_id VARCHAR(40) NOT NULL,
    token_kind VARCHAR(16) NOT NULL CHECK (token_kind IN ('root', 'branch', 'child')),
    parent_token_id VARCHAR(40),
    principal_id VARCHAR(256) NOT NULL,
    role_scope VARCHAR(32) NOT NULL,
    scopes TEXT[] NOT NULL,
    namespaces TEXT[] NOT NULL,
    depth INT NOT NULL,
    can_issue BOOLEAN NOT NULL DEFAULT FALSE,
    max_descendants INT NOT NULL DEFAULT 0,
    budget_limits JSONB NOT NULL DEFAULT '{}'::jsonb,
    purpose TEXT,
    ttl_seconds BIGINT,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    issuer_stamp JSONB,
    parent_effective_grant_hash VARCHAR(64),
    grant_hash VARCHAR(64) NOT NULL,
    backfilled BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_grant_snapshots_token
    ON grant_snapshots (tenant_id, token_id, issued_at DESC);

CREATE OR REPLACE FUNCTION agentgraph_grant_snapshot_immutable() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'grant snapshots are immutable' USING ERRCODE = 'check_violation';
END;
$$;

DROP TRIGGER IF EXISTS trg_grant_snapshot_immutable ON grant_snapshots;
CREATE TRIGGER trg_grant_snapshot_immutable
    BEFORE UPDATE ON grant_snapshots
    FOR EACH ROW EXECUTE FUNCTION agentgraph_grant_snapshot_immutable();

-- ------------------------------------------------------- lineage stamps
-- The request's stamp; writes made without one (background dispatchers,
-- migrations) are stamped as system writes so the chain is never empty.
CREATE OR REPLACE FUNCTION agentgraph_current_stamp() RETURNS JSONB
LANGUAGE sql STABLE AS $$
    SELECT COALESCE(
        NULLIF(current_setting('app.lineage_stamp', true), '')::jsonb,
        '{"kind": "system", "principal_id": "system", "token_id": null, "chain": ["system"]}'::jsonb
    )
$$;

CREATE OR REPLACE FUNCTION agentgraph_stamp_rows() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.created_stamp := agentgraph_current_stamp();
    ELSE
        NEW.created_stamp := OLD.created_stamp;
    END IF;
    NEW.updated_stamp := agentgraph_current_stamp();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION agentgraph_stamp_entry() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.stamp := agentgraph_current_stamp();
    ELSE
        NEW.stamp := OLD.stamp;  -- fixed at write time (invariant I3)
    END IF;
    RETURN NEW;
END;
$$;

DO $$
DECLARE
    target_table TEXT;
    mutable_tables TEXT[] := ARRAY[
        'shared_documents', 'collab_tasks', 'context_slots',
        'document_proposals', 'external_actions'
    ];
    entry_tables TEXT[] := ARRAY[
        'document_versions', 'state_audit_diffs', 'workspace_events',
        'timeline_streams', 'usage_ledger'
    ];
BEGIN
    FOREACH target_table IN ARRAY mutable_tables LOOP
        EXECUTE format(
            'ALTER TABLE %I ADD COLUMN IF NOT EXISTS created_stamp JSONB, '
            'ADD COLUMN IF NOT EXISTS updated_stamp JSONB', target_table
        );
        EXECUTE format(
            'UPDATE %I SET created_stamp = ''{"legacy": true, "chain": [], "token_id": null}''::jsonb, '
            'updated_stamp = ''{"legacy": true, "chain": [], "token_id": null}''::jsonb '
            'WHERE created_stamp IS NULL', target_table
        );
        EXECUTE format('DROP TRIGGER IF EXISTS trg_lineage_stamp ON %I', target_table);
        EXECUTE format(
            'CREATE TRIGGER trg_lineage_stamp BEFORE INSERT OR UPDATE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION agentgraph_stamp_rows()', target_table
        );
    END LOOP;
    FOREACH target_table IN ARRAY entry_tables LOOP
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS stamp JSONB', target_table);
        EXECUTE format(
            'UPDATE %I SET stamp = ''{"legacy": true, "chain": [], "token_id": null}''::jsonb '
            'WHERE stamp IS NULL', target_table
        );
        EXECUTE format('DROP TRIGGER IF EXISTS trg_lineage_stamp ON %I', target_table);
        EXECUTE format(
            'CREATE TRIGGER trg_lineage_stamp BEFORE INSERT OR UPDATE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION agentgraph_stamp_entry()', target_table
        );
    END LOOP;
END
$$;

CREATE INDEX IF NOT EXISTS idx_document_versions_stamp_chain
    ON document_versions USING GIN ((stamp -> 'chain'));

-- The outbox carries the actor's stamp in the live notification too.
CREATE OR REPLACE FUNCTION notify_workspace_change() RETURNS TRIGGER AS $$
DECLARE
    row_data JSONB;
    event_payload JSONB;
    new_event_id BIGINT;
BEGIN
    row_data := to_jsonb(NEW);
    event_payload := jsonb_build_object(
        'table', TG_TABLE_NAME,
        'action', TG_OP,
        'workspace_id', row_data->>'workspace_id',
        'record_id', row_data->>'id',
        'actor', COALESCE(
            row_data->>'updated_by',
            row_data->>'claimed_by',
            row_data->>'created_by',
            row_data->>'agent_signature',
            'system'
        ),
        'actor_stamp', COALESCE(
            NULLIF(current_setting('app.lineage_stamp', true), '')::jsonb,
            '{"kind": "system", "principal_id": "system", "token_id": null, "chain": ["system"]}'::jsonb
        ),
        'version', NULLIF(row_data->>'version', '')::int,
        'status', COALESCE(
            row_data->>'status',
            row_data->'frontmatter'->>'status',
            ''
        ),
        'slot_key', row_data->>'slot_key',
        'task_key', row_data->>'task_key',
        'doc_key', row_data->>'doc_key',
        'namespace', COALESCE(row_data->>'namespace', 'default'),
        'assigned_role', COALESCE(row_data->>'assigned_role', ''),
        'locked_by_human', COALESCE(row_data->>'locked_by_human', 'false')::boolean,
        'event_type', COALESCE(row_data->>'event_type', row_data->'payload'->>'event_type', ''),
        'timestamp', NOW()
    );
    INSERT INTO workspace_events (
        workspace_id, source_table, action, record_id, actor, version, status,
        slot_key, task_key, doc_key, namespace, assigned_role, locked_by_human,
        payload, created_at
    ) VALUES (
        (row_data->>'workspace_id')::uuid,
        TG_TABLE_NAME,
        TG_OP,
        (row_data->>'id')::uuid,
        event_payload->>'actor',
        NULLIF(event_payload->>'version', '')::int,
        NULLIF(event_payload->>'status', ''),
        event_payload->>'slot_key',
        event_payload->>'task_key',
        event_payload->>'doc_key',
        event_payload->>'namespace',
        NULLIF(event_payload->>'assigned_role', ''),
        COALESCE(event_payload->>'locked_by_human', 'false')::boolean,
        event_payload,
        NOW()
    ) RETURNING id INTO new_event_id;

    event_payload := event_payload || jsonb_build_object('id', new_event_id);
    PERFORM pg_notify('workspace_events', event_payload::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ------------------------------------------- tenant derivation and RLS
DROP TRIGGER IF EXISTS trg_derive_tenant ON grant_snapshots;
CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
    ON grant_snapshots FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();
ALTER TABLE grant_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE grant_snapshots FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON grant_snapshots;
CREATE POLICY tenant_isolation ON grant_snapshots
    USING (tenant_id = ANY(agentgraph_tenant_scope())
           AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope())
           AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
GRANT SELECT, INSERT ON grant_snapshots TO agentgraph_app;
GRANT EXECUTE ON FUNCTION agentgraph_current_stamp() TO agentgraph_app;
