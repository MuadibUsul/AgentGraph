-- v3.20 durability and incident response (PRD R1.3: AG-ST-10, AG-TC-06).
--
-- Platform tables (no tenant row security; reached only by the admin console
-- and background workers through the platform session):
-- * backups          one row per backup run; files live outside the database
--                    (BACKUP_DIR), one archive per root tenant in the
--                    agentgraph-export/1 format, chained full -> incrementals.
-- * restore_runs     a restore request and its outcome.
-- * restore_drills   automatic or manual restore drills into staging.
-- * platform_alerts  alerts for the platform admin (drill failures, incident
--                    SLA checkpoints, dry-run previews).
-- * incidents, incident_nodes, incident_notices, incident_timeline
--                    security incidents, the computed scope, the append-only
--                    notice record and the timeline.
-- * email_outbox     outgoing mail; a worker delivers it when SMTP is set.
--
-- Tenant data:
-- * token_access_log per token, workspace and minute: request count and the
--                    objects read (capped). Lets incident scope resolution
--                    answer "what did this token touch" for reads as well as
--                    writes (writes carry lineage stamps already).
-- * tenants.security_contacts  root-node setting (AG-ID-10): extra incident
--                    notice recipients.

CREATE TABLE IF NOT EXISTS backups (
    id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('bkp'),
    type VARCHAR(16) NOT NULL CHECK (type IN ('full', 'incremental')),
    trigger VARCHAR(16) NOT NULL DEFAULT 'schedule'
        CHECK (trigger IN ('schedule', 'manual')),
    base_backup_id VARCHAR(40),
    prev_backup_id VARCHAR(40),
    status VARCHAR(16) NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'done', 'failed', 'expired')),
    location TEXT,
    entries JSONB NOT NULL DEFAULT '[]'::jsonb,
    documents INT NOT NULL DEFAULT 0,
    bytes BIGINT NOT NULL DEFAULT 0,
    sha256 VARCHAR(64),
    as_of TIMESTAMPTZ,
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    verified_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_backups_finished ON backups (status, finished_at DESC);

CREATE TABLE IF NOT EXISTS restore_runs (
    id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('rst'),
    backup_id VARCHAR(40),
    point_in_time TIMESTAMPTZ,
    target VARCHAR(16) NOT NULL CHECK (target IN ('staging', 'new_workspace', 'in_place')),
    target_tenant_id VARCHAR(40),
    requested_by VARCHAR(256) NOT NULL DEFAULT 'admin',
    status VARCHAR(16) NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'done', 'failed')),
    chain JSONB NOT NULL DEFAULT '[]'::jsonb,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    error JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS restore_drills (
    id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('drl'),
    backup_id VARCHAR(40),
    trigger VARCHAR(16) NOT NULL DEFAULT 'schedule'
        CHECK (trigger IN ('schedule', 'manual')),
    status VARCHAR(16) NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'passed', 'failed')),
    documents_expected INT,
    documents_restored INT,
    sampled INT,
    mismatches JSONB NOT NULL DEFAULT '[]'::jsonb,
    duration_ms BIGINT,
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_restore_drills_finished ON restore_drills (finished_at DESC);

CREATE TABLE IF NOT EXISTS platform_alerts (
    id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('alr'),
    kind VARCHAR(64) NOT NULL,
    severity VARCHAR(16) NOT NULL DEFAULT 'warning'
        CHECK (severity IN ('info', 'warning', 'critical')),
    message TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    dedupe_key VARCHAR(256),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    acknowledged_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_platform_alerts_dedupe
    ON platform_alerts (dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_platform_alerts_open ON platform_alerts (acknowledged_at, created_at DESC);

CREATE TABLE IF NOT EXISTS email_outbox (
    id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('eml'),
    to_email VARCHAR(320) NOT NULL,
    subject VARCHAR(512) NOT NULL,
    body TEXT NOT NULL,
    ref VARCHAR(80),
    status VARCHAR(16) NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'sent', 'failed', 'no_transport')),
    attempts INT NOT NULL DEFAULT 0,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_email_outbox_queued ON email_outbox (status, created_at);

-- ------------------------------------------------------------ incidents
CREATE TABLE IF NOT EXISTS incidents (
    id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('inc'),
    title VARCHAR(512) NOT NULL,
    severity VARCHAR(16) NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    status VARCHAR(16) NOT NULL DEFAULT 'investigating'
        CHECK (status IN ('investigating', 'confirmed', 'notifying', 'closed')),
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    scope_inputs JSONB NOT NULL DEFAULT '{}'::jsonb,
    scope_resolved_at TIMESTAMPTZ,
    summary TEXT,
    measures_taken TEXT,
    recommended_actions TEXT,
    contact TEXT,
    owner_admin VARCHAR(256),
    reminders_sent JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One row per affected tenant node, from the latest scope resolution.
CREATE TABLE IF NOT EXISTS incident_nodes (
    incident_id VARCHAR(40) NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    root_tenant_id VARCHAR(40) NOT NULL,
    tenant_path VARCHAR(40)[] NOT NULL,
    affected_objects JSONB NOT NULL DEFAULT '{}'::jsonb,
    may_include_personal_info BOOLEAN NOT NULL DEFAULT TRUE,
    evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    notified_at TIMESTAMPTZ,
    sla_breached BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (incident_id, tenant_id)
);

-- Append-only: one row per node and recipient per sending. Only
-- delivery_status and superseded_by may change after insert.
CREATE TABLE IF NOT EXISTS incident_notices (
    id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('ntc'),
    incident_id VARCHAR(40) NOT NULL REFERENCES incidents(id),
    kind VARCHAR(16) NOT NULL CHECK (kind IN ('initial', 'update')),
    tenant_id VARCHAR(40) NOT NULL,
    root_tenant_id VARCHAR(40) NOT NULL,
    recipient JSONB NOT NULL,
    channels JSONB NOT NULL,
    content JSONB NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    delivery_status JSONB NOT NULL DEFAULT '{}'::jsonb,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    superseded_by VARCHAR(40)
);
CREATE INDEX IF NOT EXISTS idx_incident_notices_tenant ON incident_notices (tenant_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_incident_notices_incident ON incident_notices (incident_id, tenant_id);

CREATE OR REPLACE FUNCTION agentgraph_notice_append_only() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'incident notices are append-only' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.incident_id IS DISTINCT FROM OLD.incident_id
       OR NEW.kind IS DISTINCT FROM OLD.kind
       OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.recipient IS DISTINCT FROM OLD.recipient
       OR NEW.channels IS DISTINCT FROM OLD.channels
       OR NEW.content IS DISTINCT FROM OLD.content
       OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
       OR NEW.sent_at IS DISTINCT FROM OLD.sent_at THEN
        RAISE EXCEPTION 'incident notices are append-only' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_notice_append_only ON incident_notices;
CREATE TRIGGER trg_notice_append_only BEFORE UPDATE OR DELETE ON incident_notices
    FOR EACH ROW EXECUTE FUNCTION agentgraph_notice_append_only();

CREATE TABLE IF NOT EXISTS incident_timeline (
    id BIGSERIAL PRIMARY KEY,
    incident_id VARCHAR(40) NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kind VARCHAR(64) NOT NULL,
    actor VARCHAR(256) NOT NULL DEFAULT 'admin',
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_incident_timeline ON incident_timeline (incident_id, id);

ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS security_contacts JSONB NOT NULL DEFAULT '[]'::jsonb;

-- ------------------------------------------------------ token access log
CREATE TABLE IF NOT EXISTS token_access_log (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    token_id VARCHAR(40) NOT NULL DEFAULT '',
    principal_id VARCHAR(256) NOT NULL,
    bucket TIMESTAMPTZ NOT NULL,
    requests INT NOT NULL DEFAULT 0,
    writes INT NOT NULL DEFAULT 0,
    documents TEXT[] NOT NULL DEFAULT '{}',
    tasks TEXT[] NOT NULL DEFAULT '{}',
    UNIQUE (workspace_id, principal_id, token_id, bucket)
);
CREATE INDEX IF NOT EXISTS idx_token_access_token ON token_access_log (token_id, bucket);
CREATE INDEX IF NOT EXISTS idx_token_access_principal ON token_access_log (principal_id, bucket);

DROP TRIGGER IF EXISTS trg_derive_tenant ON token_access_log;
CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
    ON token_access_log FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();
ALTER TABLE token_access_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE token_access_log FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON token_access_log;
CREATE POLICY tenant_isolation ON token_access_log
    USING (tenant_id = ANY(agentgraph_tenant_scope())
           AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope())
           AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON token_access_log TO agentgraph_app;
GRANT USAGE, SELECT ON SEQUENCE token_access_log_id_seq TO agentgraph_app;
