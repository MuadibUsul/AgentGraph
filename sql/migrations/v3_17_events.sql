-- v3.17 unified event log (PRD AG-EV-01 / AG-EV-02).
-- workspace_events is already the outbox: rows are written by triggers in the
-- same transaction as the change. This migration gives each event
-- * seq: per workspace, gap-free and in commit order. The counter row in
--   workspace_event_seq is locked until the writing transaction ends, so seq
--   order equals commit order and a rolled-back write leaves no gap;
-- * event_id (evt_<ULID>), a dotted type (doc.updated, task.claimed, ...),
--   targets (addressed principals), refs (canonical pointers), a summary and
--   the shard_key (namespace) used by subscriptions.
-- The trigger function itself lives in init.sql (re-applied on every start).
-- * event_subscriptions: server-side prefix subscriptions.
-- seq is assigned at commit by a deferred constraint trigger: the counter
-- row is locked from that instant to the end of commit, never across the
-- writer's work (such as synchronous embedding), and a rollback undoes both.

CREATE TABLE IF NOT EXISTS workspace_event_seq (
    workspace_id UUID PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    seq BIGINT NOT NULL DEFAULT 0
);

ALTER TABLE workspace_events ADD COLUMN IF NOT EXISTS seq BIGINT;
ALTER TABLE workspace_events ADD COLUMN IF NOT EXISTS event_id VARCHAR(40);
ALTER TABLE workspace_events ADD COLUMN IF NOT EXISTS type VARCHAR(64);
ALTER TABLE workspace_events ADD COLUMN IF NOT EXISTS targets JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE workspace_events ADD COLUMN IF NOT EXISTS refs JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE workspace_events ADD COLUMN IF NOT EXISTS summary VARCHAR(200);
ALTER TABLE workspace_events ADD COLUMN IF NOT EXISTS shard_key VARCHAR(160);

-- Existing events keep their order; types for them are derived coarsely.
WITH numbered AS (
    SELECT id, row_number() OVER (PARTITION BY workspace_id ORDER BY id) AS n
    FROM workspace_events WHERE seq IS NULL
)
UPDATE workspace_events e
SET seq = numbered.n
        + COALESCE((SELECT MAX(seq) FROM workspace_events x
                    WHERE x.workspace_id = e.workspace_id AND x.seq IS NOT NULL), 0),
    event_id = agentgraph_new_id('evt'),
    shard_key = COALESCE(e.namespace, 'default'),
    type = CASE
        WHEN e.source_table = 'shared_documents' AND e.action = 'INSERT' THEN 'doc.created'
        WHEN e.source_table = 'shared_documents' THEN 'doc.updated'
        WHEN e.source_table = 'collab_tasks' AND e.action = 'INSERT' THEN 'task.created'
        WHEN e.source_table = 'collab_tasks' THEN 'task.updated'
        WHEN e.source_table = 'context_slots' AND e.action = 'INSERT' THEN 'slot.created'
        WHEN e.source_table = 'context_slots' THEN 'slot.updated'
        WHEN e.source_table = 'timeline_streams'
            THEN 'timeline.' || lower(COALESCE(NULLIF(e.payload->>'event_type', ''), 'event'))
        ELSE e.source_table || '.' || lower(e.action)
    END,
    refs = jsonb_strip_nulls(jsonb_build_object(
        'alias', COALESCE(e.doc_key, e.task_key, e.slot_key), 'version', e.version))
FROM numbered WHERE numbered.id = e.id;

INSERT INTO workspace_event_seq (workspace_id, tenant_id, seq)
SELECT e.workspace_id, w.tenant_id, MAX(e.seq)
FROM workspace_events e JOIN workspaces w ON w.id = e.workspace_id
GROUP BY e.workspace_id, w.tenant_id
ON CONFLICT (workspace_id) DO UPDATE SET seq = GREATEST(workspace_event_seq.seq, EXCLUDED.seq);

-- seq stays NULL only inside the writing transaction (see below).
ALTER TABLE workspace_events ALTER COLUMN event_id SET NOT NULL;
ALTER TABLE workspace_events ALTER COLUMN event_id SET DEFAULT agentgraph_new_id('evt');
CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_events_seq ON workspace_events (workspace_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_events_event_id ON workspace_events (event_id);
CREATE INDEX IF NOT EXISTS idx_workspace_events_shard
    ON workspace_events (workspace_id, shard_key, seq);

CREATE OR REPLACE FUNCTION agentgraph_assign_event_seq() RETURNS TRIGGER AS $$
DECLARE
    next_seq BIGINT;
BEGIN
    IF NEW.workspace_id IS NULL THEN
        RETURN NULL;
    END IF;
    INSERT INTO workspace_event_seq (workspace_id, seq) VALUES (NEW.workspace_id, 1)
    ON CONFLICT (workspace_id) DO UPDATE SET seq = workspace_event_seq.seq + 1
    RETURNING seq INTO next_seq;
    UPDATE workspace_events SET seq = next_seq WHERE id = NEW.id;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;

DROP TRIGGER IF EXISTS trg_assign_event_seq ON workspace_events;
CREATE CONSTRAINT TRIGGER trg_assign_event_seq AFTER INSERT ON workspace_events
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION agentgraph_assign_event_seq();

CREATE TABLE IF NOT EXISTS event_subscriptions (
    id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('sub'),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    owner VARCHAR(128) NOT NULL,
    owner_token_id UUID,
    name VARCHAR(128) NOT NULL,
    prefixes JSONB NOT NULL DEFAULT '[]'::jsonb,
    types JSONB,
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_polled_at TIMESTAMPTZ,
    last_seq BIGINT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_event_subscriptions_name
    ON event_subscriptions (workspace_id, owner, name);

DO $$
DECLARE
    target_table TEXT;
BEGIN
    FOREACH target_table IN ARRAY ARRAY['workspace_event_seq', 'event_subscriptions'] LOOP
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
