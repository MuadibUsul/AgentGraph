-- v3.32 Automated quality control 1.0 (工程设计 执行版 1.0).
--
-- * collab_tasks.acceptance / acceptance_locked: the acceptance items a task is
--   judged against, locked at the first claim.
-- * quality_gate_runs: machine gate results (latest per gate kind wins).
-- * audits: the structured projection of every review verdict.
-- * autonomy_tiers: per-domain autonomy (no row = tier 0, supervised).
-- * defect_escapes, audit_samples: escapes and the sampling queue.
-- * change_claims: changes made outside AgentGraph (a PR, a commit).
-- * quality_circuit: per-domain circuit breaker.
-- * poison_marks: planted defects; never readable by agents.
-- * audit_view_marks: who opened the audit view in which review round.
-- * quality_releases: how each change was released (for the daily cap and the
--   supervised change after a circuit closes).

ALTER TABLE collab_tasks
    ADD COLUMN IF NOT EXISTS acceptance JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS acceptance_locked BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS change_claims (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    reporter VARCHAR(256) NOT NULL,
    source VARCHAR(16) NOT NULL DEFAULT 'AGENT' CHECK (source IN ('AGENT', 'WEBHOOK')),
    external_ref TEXT NOT NULL,
    commit_sha VARCHAR(64) NOT NULL,
    related_doc_key VARCHAR(256),
    related_task_key VARCHAR(128),
    scope_key VARCHAR(512) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'CLAIMED'
        CHECK (status IN ('CLAIMED', 'VERIFIED', 'REFUTED')),
    verified_by VARCHAR(256),
    verified_at TIMESTAMPTZ,
    verify_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, external_ref, commit_sha)
);
CREATE INDEX IF NOT EXISTS idx_change_claims_status ON change_claims (workspace_id, status);

CREATE TABLE IF NOT EXISTS quality_gate_runs (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    task_id UUID REFERENCES collab_tasks(id) ON DELETE CASCADE,
    claim_id UUID REFERENCES change_claims(id) ON DELETE CASCADE,
    scope_key VARCHAR(512) NOT NULL,
    gate_kind VARCHAR(32) NOT NULL
        CHECK (gate_kind IN ('CI', 'TESTS', 'ACCEPTANCE', 'SPEC_LINT', 'CUSTOM')),
    status VARCHAR(16) NOT NULL CHECK (status IN ('PASS', 'FAIL', 'ERROR', 'SKIPPED')),
    source VARCHAR(16) NOT NULL CHECK (source IN ('RUNNER', 'AGENT_CLAIM')),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    reported_by VARCHAR(256) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK ((task_id IS NULL) <> (claim_id IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_quality_gate_runs_task
    ON quality_gate_runs (task_id, gate_kind, created_at);
CREATE INDEX IF NOT EXISTS idx_quality_gate_runs_claim
    ON quality_gate_runs (claim_id, gate_kind, created_at);

CREATE TABLE IF NOT EXISTS audits (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    task_id UUID NOT NULL REFERENCES collab_tasks(id) ON DELETE CASCADE,
    auditor VARCHAR(256) NOT NULL,
    verdict VARCHAR(8) NOT NULL CHECK (verdict IN ('APPROVE', 'REJECT')),
    items JSONB NOT NULL DEFAULT '[]'::jsonb,
    cited_clauses JSONB NOT NULL DEFAULT '[]'::jsonb,
    feedback TEXT,
    review_round INT NOT NULL DEFAULT 0,
    view_used BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audits_task ON audits (task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audits_auditor ON audits (workspace_id, auditor, created_at);

CREATE TABLE IF NOT EXISTS autonomy_tiers (
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    scope_type VARCHAR(16) NOT NULL CHECK (scope_type IN ('NAMESPACE', 'REPO_PATH')),
    scope_key VARCHAR(512) NOT NULL,
    tier SMALLINT NOT NULL CHECK (tier BETWEEN 0 AND 2),
    updated_by VARCHAR(256) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (workspace_id, scope_type, scope_key)
);

CREATE TABLE IF NOT EXISTS defect_escapes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    task_id UUID REFERENCES collab_tasks(id) ON DELETE SET NULL,
    claim_id UUID REFERENCES change_claims(id) ON DELETE SET NULL,
    scope_key VARCHAR(512),
    source VARCHAR(16) NOT NULL
        CHECK (source IN ('PROD_INCIDENT', 'HUMAN_SAMPLE', 'POISON_TEST', 'EXTERNAL_REPORT')),
    severity VARCHAR(2) NOT NULL CHECK (severity IN ('S1', 'S2', 'S3')),
    description TEXT NOT NULL,
    root_gate VARCHAR(16)
        CHECK (root_gate IN ('CI', 'TESTS', 'ACCEPTANCE', 'AUDIT', 'SPEC', 'NONE')),
    status VARCHAR(8) NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'FIXED', 'WAIVED')),
    fix_ref TEXT,
    waive_reason TEXT,
    created_by VARCHAR(256) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_defect_escapes_status ON defect_escapes (workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_defect_escapes_gate ON defect_escapes (workspace_id, root_gate);

CREATE TABLE IF NOT EXISTS audit_samples (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    task_id UUID NOT NULL REFERENCES collab_tasks(id) ON DELETE CASCADE,
    reason VARCHAR(8) NOT NULL CHECK (reason IN ('RANDOM', 'POISON', 'RISK')),
    assigned_to VARCHAR(256) NOT NULL,
    verdict VARCHAR(8) NOT NULL DEFAULT 'PENDING' CHECK (verdict IN ('PENDING', 'OK', 'DEFECT')),
    escape_id UUID REFERENCES defect_escapes(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_audit_samples_queue
    ON audit_samples (workspace_id, assigned_to, verdict);

CREATE TABLE IF NOT EXISTS quality_circuit (
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    scope_key VARCHAR(512) NOT NULL,
    state VARCHAR(8) NOT NULL DEFAULT 'CLOSED' CHECK (state IN ('CLOSED', 'OPEN')),
    consecutive_failures INT NOT NULL DEFAULT 0,
    opened_at TIMESTAMPTZ,
    opened_reason TEXT,
    degraded_tier SMALLINT,
    last_closed_at TIMESTAMPTZ,
    supervised_next BOOLEAN NOT NULL DEFAULT FALSE,
    opened_count INT NOT NULL DEFAULT 0,
    PRIMARY KEY (workspace_id, scope_key)
);

CREATE TABLE IF NOT EXISTS poison_marks (
    task_id UUID PRIMARY KEY REFERENCES collab_tasks(id) ON DELETE CASCADE,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    defect_description TEXT NOT NULL,
    expected_gate VARCHAR(32) NOT NULL,
    created_by VARCHAR(256) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS audit_view_marks (
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    task_id UUID NOT NULL REFERENCES collab_tasks(id) ON DELETE CASCADE,
    viewer VARCHAR(256) NOT NULL,
    review_round INT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (task_id, viewer, review_round)
);

CREATE TABLE IF NOT EXISTS quality_releases (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    task_id UUID REFERENCES collab_tasks(id) ON DELETE CASCADE,
    claim_id UUID REFERENCES change_claims(id) ON DELETE CASCADE,
    scope_key VARCHAR(512) NOT NULL,
    tier SMALLINT NOT NULL,
    path VARCHAR(8) NOT NULL CHECK (path IN ('AUTO', 'AUDITED', 'HUMAN')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_quality_releases_scope
    ON quality_releases (workspace_id, scope_key, created_at);

DO $$
DECLARE
    target_table TEXT;
BEGIN
    FOREACH target_table IN ARRAY ARRAY[
        'change_claims', 'quality_gate_runs', 'audits', 'autonomy_tiers', 'defect_escapes',
        'audit_samples', 'quality_circuit', 'poison_marks', 'audit_view_marks',
        'quality_releases'] LOOP
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
GRANT USAGE, SELECT ON SEQUENCE quality_gate_runs_id_seq, audits_id_seq,
    quality_releases_id_seq TO agentgraph_app;
