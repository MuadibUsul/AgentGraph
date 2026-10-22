-- v3.27 Phase 3 (AG-ST-11, AG-EV-04, AG-EV-05, AG-CO-06, AG-CO-07, AG-CO-09
-- reservation, AG-CO-12 display).
--
-- * Document lifecycle: last_read_at for cold detection; expires_at /
--   superseded_by live in frontmatter, lifecycle is computed.
-- * room_id (AG-CO-09): reserved and nullable on documents, tasks and events.
-- * reviews: the peer reviews bound to a document's versions (AG-CO-06).
-- * watermarks: named cursors owned by a principal, not a token (AG-EV-04).
-- * webhooks: first failure time for the 24-hour auto-disable (AG-EV-05).
-- * resources + resource_queue: scarce resources with FIFO queues on top of
--   the unified lease table (AG-CO-07).

ALTER TABLE shared_documents
    ADD COLUMN IF NOT EXISTS last_read_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS reviews JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS room_id VARCHAR(40);
ALTER TABLE collab_tasks ADD COLUMN IF NOT EXISTS room_id VARCHAR(40);
ALTER TABLE workspace_events ADD COLUMN IF NOT EXISTS room_id VARCHAR(40);
ALTER TABLE webhook_rules
    ADD COLUMN IF NOT EXISTS failing_since TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS disabled_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS created_by VARCHAR(256);

CREATE TABLE IF NOT EXISTS watermarks (
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    principal_ref VARCHAR(256) NOT NULL,
    name VARCHAR(64) NOT NULL,
    seq BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (workspace_id, principal_ref, name)
);

CREATE TABLE IF NOT EXISTS resources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    resource_key VARCHAR(128) NOT NULL,
    capacity INT NOT NULL DEFAULT 1 CHECK (capacity BETWEEN 1 AND 100),
    description TEXT,
    max_hold_seconds INT NOT NULL DEFAULT 900 CHECK (max_hold_seconds BETWEEN 1 AND 3600),
    created_by VARCHAR(256) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, resource_key)
);

CREATE TABLE IF NOT EXISTS resource_queue (
    ticket VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('rqt'),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    resource_id UUID NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    holder VARCHAR(256) NOT NULL,
    ttl_seconds INT NOT NULL,
    state VARCHAR(12) NOT NULL DEFAULT 'waiting'
        CHECK (state IN ('waiting', 'offered', 'granted', 'abandoned', 'cancelled')),
    offered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_resource_queue_live
    ON resource_queue (resource_id, created_at) WHERE state IN ('waiting', 'offered');

DO $$
DECLARE
    target_table TEXT;
BEGIN
    FOREACH target_table IN ARRAY ARRAY['watermarks', 'resources', 'resource_queue'] LOOP
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
