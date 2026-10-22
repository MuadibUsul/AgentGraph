-- v3.23 collaboration (PRD Phase 2 R2.1: AG-CO-02, AG-EV-03, AG-CO-04,
-- AG-CO-05, AG-CO-08).
--
-- * Tasks carry pointers, not copies: context_docs (pinned to versions at the
--   first claim), done_definition, output_to, required role/capabilities, kind.
-- * The latest continuation pack (a document in handoff/) is referenced from
--   the task; continuation_at / claimed_at tell whether this round wrote one.
-- * Task trees: parent/root/depth, a lead, children_done and the rollup of
--   children's outputs.
-- * escalations: an agent's question to a human with options, blocking the
--   task (WAITING_HUMAN, lease paused) until it is resolved or times out.
-- * Statuses WAITING_HUMAN and CANCELLED.

ALTER TYPE task_status ADD VALUE IF NOT EXISTS 'WAITING_HUMAN';
ALTER TYPE task_status ADD VALUE IF NOT EXISTS 'CANCELLED';

ALTER TABLE collab_tasks
    ADD COLUMN IF NOT EXISTS context_docs JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS context_versions_pinned JSONB,
    ADD COLUMN IF NOT EXISTS done_definition TEXT,
    ADD COLUMN IF NOT EXISTS output_to JSONB,
    ADD COLUMN IF NOT EXISTS output_summary TEXT,
    ADD COLUMN IF NOT EXISTS required_role VARCHAR(32),
    ADD COLUMN IF NOT EXISTS required_capabilities TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS kind VARCHAR(16) NOT NULL DEFAULT 'work'
        CHECK (kind IN ('work', 'question', 'escalation', 'peer_review')),
    ADD COLUMN IF NOT EXISTS require_continuation BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS continuation JSONB,
    ADD COLUMN IF NOT EXISTS continuation_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS parent_task_id UUID REFERENCES collab_tasks(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS root_task_id UUID,
    ADD COLUMN IF NOT EXISTS parent_task_key VARCHAR(128),
    ADD COLUMN IF NOT EXISTS root_task_key VARCHAR(128),
    ADD COLUMN IF NOT EXISTS tree_depth INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS lead VARCHAR(160),
    ADD COLUMN IF NOT EXISTS children_done BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS rollup JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS lease_paused_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS mentions JSONB NOT NULL DEFAULT '[]'::jsonb;
CREATE INDEX IF NOT EXISTS idx_collab_tasks_parent ON collab_tasks (parent_task_id);
CREATE INDEX IF NOT EXISTS idx_collab_tasks_root ON collab_tasks (root_task_id);

CREATE TABLE IF NOT EXISTS escalations (
    id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('esc'),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    task_id UUID REFERENCES collab_tasks(id) ON DELETE SET NULL,
    escalation_task_id UUID REFERENCES collab_tasks(id) ON DELETE SET NULL,
    raised_by VARCHAR(160) NOT NULL,
    raised_by_token VARCHAR(40),
    question TEXT NOT NULL,
    options JSONB NOT NULL DEFAULT '[]'::jsonb,
    recommended_option VARCHAR(64),
    context_docs JSONB NOT NULL DEFAULT '[]'::jsonb,
    tainted_context BOOLEAN NOT NULL DEFAULT FALSE,
    blocking BOOLEAN NOT NULL DEFAULT TRUE,
    due_at TIMESTAMPTZ,
    default_on_timeout VARCHAR(64),
    route_to VARCHAR(160),
    status VARCHAR(16) NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    option_id VARCHAR(64),
    answer TEXT,
    note TEXT,
    resolved_by VARCHAR(160),
    resolved_at TIMESTAMPTZ,
    last_reminded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_escalations_open ON escalations (workspace_id, status, due_at);

DROP TRIGGER IF EXISTS trg_derive_tenant ON escalations;
CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
    ON escalations FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();
ALTER TABLE escalations ENABLE ROW LEVEL SECURITY;
ALTER TABLE escalations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON escalations;
CREATE POLICY tenant_isolation ON escalations
    USING (tenant_id = ANY(agentgraph_tenant_scope())
           AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope())
           AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON escalations TO agentgraph_app;
