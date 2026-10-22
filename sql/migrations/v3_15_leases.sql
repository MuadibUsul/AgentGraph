-- v3.15 unified leases (PRD AG-CO-03).
-- * leases: one row per grant. The task row stays the fast path for checks
--   (lease_owner / lease_expires_at / lease_epoch); the lease row is the
--   addressable record (lease_<ULID>) with heartbeats and how it ended.
--   fencing_token is the task's lease_epoch at grant time, so it only grows.
-- * collab_tasks gains orphan bookkeeping and an optional assignee.
-- * A trigger keeps the two in step: a new epoch ends the previous active
--   lease (state released unless the caller ended it first as expired or
--   revoked), and a new expiry on the task extends the active lease.

CREATE TABLE IF NOT EXISTS leases (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    lease_id VARCHAR(40) NOT NULL DEFAULT agentgraph_new_id('lease'),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    resource_kind VARCHAR(16) NOT NULL CHECK (resource_kind IN ('task', 'resource', 'slot')),
    resource_id UUID NOT NULL,
    resource_ref VARCHAR(256) NOT NULL,
    fencing_token INT NOT NULL,
    holder VARCHAR(128) NOT NULL,
    holder_token_id UUID,
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    ttl_seconds INT NOT NULL,
    heartbeat_interval_s INT NOT NULL,
    last_heartbeat_at TIMESTAMPTZ,
    heartbeat_note TEXT,
    progress DOUBLE PRECISION,
    heartbeats JSONB NOT NULL DEFAULT '[]'::jsonb,
    heartbeat_missed_at TIMESTAMPTZ,
    state VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'released', 'expired', 'revoked')),
    ended_at TIMESTAMPTZ,
    end_reason VARCHAR(64)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_leases_lease_id ON leases (lease_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_leases_active_resource
    ON leases (resource_kind, resource_id) WHERE state = 'active';
CREATE INDEX IF NOT EXISTS idx_leases_holder
    ON leases (tenant_id, workspace_id, holder, state);
CREATE INDEX IF NOT EXISTS idx_leases_active_expiry
    ON leases (state, expires_at);

ALTER TABLE collab_tasks ADD COLUMN IF NOT EXISTS orphaned_from VARCHAR(128);
ALTER TABLE collab_tasks ADD COLUMN IF NOT EXISTS orphan_count INT NOT NULL DEFAULT 0;
ALTER TABLE collab_tasks ADD COLUMN IF NOT EXISTS assignee VARCHAR(160);

DROP TRIGGER IF EXISTS trg_derive_tenant ON leases;
CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
    ON leases FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();
ALTER TABLE leases ENABLE ROW LEVEL SECURITY;
ALTER TABLE leases FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON leases;
CREATE POLICY tenant_isolation ON leases
    USING (tenant_id = ANY(agentgraph_tenant_scope())
           AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope())
           AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON leases TO agentgraph_app;

CREATE OR REPLACE FUNCTION agentgraph_sync_task_lease() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.lease_epoch IS DISTINCT FROM OLD.lease_epoch THEN
        UPDATE leases
        SET state = 'released', ended_at = NOW(), end_reason = COALESCE(end_reason, 'released')
        WHERE resource_kind = 'task' AND resource_id = NEW.id AND state = 'active'
          AND fencing_token <> NEW.lease_epoch;
    ELSIF NEW.lease_expires_at IS DISTINCT FROM OLD.lease_expires_at
          AND NEW.lease_expires_at IS NOT NULL THEN
        UPDATE leases SET expires_at = NEW.lease_expires_at
        WHERE resource_kind = 'task' AND resource_id = NEW.id AND state = 'active'
          AND fencing_token = NEW.lease_epoch;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_sync_task_lease ON collab_tasks;
CREATE TRIGGER trg_sync_task_lease AFTER UPDATE OF lease_epoch, lease_expires_at
    ON collab_tasks FOR EACH ROW EXECUTE FUNCTION agentgraph_sync_task_lease();

-- Existing leases become lease records. Leases without an expiry get 24 h so
-- that the upgrade does not orphan every running task at once.
UPDATE collab_tasks SET lease_expires_at = NOW() + INTERVAL '24 hours'
WHERE lease_owner IS NOT NULL AND lease_expires_at IS NULL;

INSERT INTO leases (workspace_id, tenant_id, resource_kind, resource_id, resource_ref,
                    fencing_token, holder, acquired_at, expires_at, ttl_seconds,
                    heartbeat_interval_s, last_heartbeat_at)
SELECT t.workspace_id, t.tenant_id, 'task', t.id, t.task_key, t.lease_epoch, t.lease_owner,
       t.updated_at, t.lease_expires_at,
       GREATEST(60, EXTRACT(EPOCH FROM (t.lease_expires_at - t.updated_at))::int),
       GREATEST(20, EXTRACT(EPOCH FROM (t.lease_expires_at - t.updated_at))::int / 3),
       t.updated_at
FROM collab_tasks t
WHERE t.lease_owner IS NOT NULL AND t.lease_expires_at > NOW()
  AND NOT EXISTS (
      SELECT 1 FROM leases l WHERE l.resource_kind = 'task' AND l.resource_id = t.id
        AND l.state = 'active'
  );
