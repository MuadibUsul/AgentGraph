-- AgentGraph v3.2 production expansion.
-- Adds proposals/arena, path policy, taint provenance, human accounts, typed
-- links, chunked hybrid retrieval, outbound webhooks/actions, budgets and RLS.
-- Idempotent: safe to run on every boot after the v3.1 schema exists.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 1. Existing tables: additive columns ---------------------------------------
ALTER TABLE agent_tokens
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS per_minute_limit INT,
    ADD COLUMN IF NOT EXISTS monthly_token_budget BIGINT;

ALTER TABLE shared_documents
    ADD COLUMN IF NOT EXISTS tainted BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS taint_sources JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS taint_cleared_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS taint_cleared_by VARCHAR(128),
    ADD COLUMN IF NOT EXISTS policy_flags JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE context_slots
    ADD COLUMN IF NOT EXISTS tainted BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS taint_sources JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE collab_tasks
    ADD COLUMN IF NOT EXISTS tainted BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS taint_sources JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE document_versions
    ADD COLUMN IF NOT EXISTS tainted BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS taint_sources JSONB NOT NULL DEFAULT '[]'::jsonb;

-- Typed workspace link columns give each link real foreign keys while preserving
-- the generic source_id/target_id API shape.
ALTER TABLE workspace_links
    ADD COLUMN IF NOT EXISTS source_type VARCHAR(16),
    ADD COLUMN IF NOT EXISTS target_type VARCHAR(16),
    ADD COLUMN IF NOT EXISTS source_task_id UUID REFERENCES collab_tasks(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS source_slot_id UUID REFERENCES context_slots(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS source_doc_id UUID REFERENCES shared_documents(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS target_task_id UUID REFERENCES collab_tasks(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS target_slot_id UUID REFERENCES context_slots(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS target_doc_id UUID REFERENCES shared_documents(id) ON DELETE CASCADE;

-- 2. Chunked hybrid retrieval ------------------------------------------------
CREATE TABLE IF NOT EXISTS document_chunks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    document_id UUID NOT NULL REFERENCES shared_documents(id) ON DELETE CASCADE,
    doc_key VARCHAR(256) NOT NULL,
    chunk_index INT NOT NULL CHECK (chunk_index >= 0),
    heading TEXT,
    content TEXT NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    embedding VECTOR(1024),
    embedding_model VARCHAR(128),
    search_vector TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(heading, '') || ' ' || content)
    ) STORED,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_document_chunks_workspace
    ON document_chunks(workspace_id);
CREATE INDEX IF NOT EXISTS idx_document_chunks_doc
    ON document_chunks(document_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding
    ON document_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_document_chunks_fts
    ON document_chunks USING gin (search_vector);
CREATE INDEX IF NOT EXISTS idx_document_chunks_trgm
    ON document_chunks USING gin (content gin_trgm_ops);

-- 3. Proposals and Proposal Arena --------------------------------------------
CREATE TABLE IF NOT EXISTS document_proposals (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    doc_key VARCHAR(256) NOT NULL,
    title VARCHAR(256) NOT NULL,
    content TEXT NOT NULL,
    frontmatter JSONB NOT NULL DEFAULT '{}'::jsonb,
    base_version INT NOT NULL CHECK (base_version >= 1),
    base_content_hash VARCHAR(64) NOT NULL,
    mode VARCHAR(16) NOT NULL DEFAULT 'single'
        CHECK (mode IN ('single', 'arena')),
    status VARCHAR(24) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'APPLIED', 'REJECTED', 'SUPERSEDED')),
    created_by VARCHAR(128) NOT NULL,
    reviewed_by VARCHAR(128),
    review_note TEXT,
    applied_version INT,
    applied_by VARCHAR(128),
    session_batch_id UUID,
    tainted BOOLEAN NOT NULL DEFAULT FALSE,
    taint_sources JSONB NOT NULL DEFAULT '[]'::jsonb,
    conflict_details JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_document_proposals_doc
    ON document_proposals(workspace_id, doc_key, status);
CREATE INDEX IF NOT EXISTS idx_document_proposals_status
    ON document_proposals(workspace_id, status);

CREATE TABLE IF NOT EXISTS proposal_votes (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    proposal_id UUID NOT NULL REFERENCES document_proposals(id) ON DELETE CASCADE,
    voter VARCHAR(128) NOT NULL,
    score INT NOT NULL CHECK (score BETWEEN 0 AND 5),
    comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(proposal_id, voter)
);

CREATE INDEX IF NOT EXISTS idx_proposal_votes_proposal
    ON proposal_votes(proposal_id);

-- 4. Path-level policy / JSON Schema gates -----------------------------------
CREATE TABLE IF NOT EXISTS path_policies (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name VARCHAR(128) NOT NULL,
    resource_type VARCHAR(16) NOT NULL
        CHECK (resource_type IN ('document', 'slot', 'task')),
    pattern VARCHAR(256) NOT NULL,
    priority INT NOT NULL DEFAULT 0,
    require_proposal BOOLEAN NOT NULL DEFAULT FALSE,
    allowed_roles TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
    json_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    max_content_bytes INT,
    require_verified_sources BOOLEAN NOT NULL DEFAULT FALSE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(workspace_id, resource_type, pattern)
);

CREATE INDEX IF NOT EXISTS idx_path_policies_lookup
    ON path_policies(workspace_id, resource_type, active);

-- 5. Human accounts and sessions ---------------------------------------------
CREATE TABLE IF NOT EXISTS human_users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    email VARCHAR(320) NOT NULL,
    password_hash VARCHAR(512) NOT NULL,
    display_name VARCHAR(128) NOT NULL,
    role VARCHAR(16) NOT NULL DEFAULT 'REVIEWER'
        CHECK (role IN ('OWNER', 'REVIEWER', 'VIEWER')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at TIMESTAMPTZ,
    UNIQUE(email)
);

CREATE INDEX IF NOT EXISTS idx_human_users_workspace
    ON human_users(workspace_id);

CREATE TABLE IF NOT EXISTS human_sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES human_users(id) ON DELETE CASCADE,
    token_hash VARCHAR(64) UNIQUE NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    user_agent TEXT,
    ip_address VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS idx_human_sessions_user
    ON human_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_human_sessions_expiry
    ON human_sessions(expires_at);

-- 6. Taint provenance graph ---------------------------------------------------
CREATE TABLE IF NOT EXISTS taint_edges (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    source_doc_id UUID NOT NULL REFERENCES shared_documents(id) ON DELETE CASCADE,
    target_type VARCHAR(16) NOT NULL
        CHECK (target_type IN ('document', 'slot', 'task')),
    target_id UUID NOT NULL,
    reason VARCHAR(64) NOT NULL DEFAULT 'source',
    depth INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(workspace_id, source_doc_id, target_type, target_id)
);

CREATE INDEX IF NOT EXISTS idx_taint_edges_target
    ON taint_edges(workspace_id, target_type, target_id);

-- 7. External action outbox + compensation ----------------------------------
CREATE TABLE IF NOT EXISTS external_actions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    action_type VARCHAR(64) NOT NULL,
    target_url TEXT NOT NULL,
    method VARCHAR(8) NOT NULL DEFAULT 'POST'
        CHECK (method IN ('POST', 'PUT', 'PATCH', 'DELETE')),
    headers JSONB NOT NULL DEFAULT '{}'::jsonb,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key VARCHAR(128) NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'PENDING_APPROVAL'
        CHECK (status IN (
            'PENDING_APPROVAL', 'APPROVED', 'EXECUTING', 'SUCCEEDED',
            'FAILED', 'REJECTED', 'COMPENSATION_PENDING', 'COMPENSATED',
            'CANCELLED'
        )),
    attempts INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 5,
    last_error TEXT,
    response_status INT,
    response_body TEXT,
    approved_by VARCHAR(128),
    approved_at TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    compensates_action_id UUID REFERENCES external_actions(id) ON DELETE SET NULL,
    compensation_payload JSONB,
    namespace VARCHAR(64) NOT NULL DEFAULT 'default',
    session_batch_id UUID,
    created_by VARCHAR(128) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(workspace_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_external_actions_status
    ON external_actions(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_external_actions_batch
    ON external_actions(workspace_id, session_batch_id);

CREATE TABLE IF NOT EXISTS external_action_attempts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    action_id UUID NOT NULL REFERENCES external_actions(id) ON DELETE CASCADE,
    attempt INT NOT NULL,
    status VARCHAR(16) NOT NULL,
    response_status INT,
    error TEXT,
    duration_ms INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE external_action_attempts
    ADD COLUMN IF NOT EXISTS workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE;
UPDATE external_action_attempts a
SET workspace_id = e.workspace_id
FROM external_actions e
WHERE a.action_id = e.id AND a.workspace_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_external_action_attempts_action
    ON external_action_attempts(workspace_id, action_id, created_at);

-- 8. Budgets, usage ledger, fixed-window rate limits -------------------------
CREATE TABLE IF NOT EXISTS workspace_budgets (
    workspace_id UUID PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    monthly_token_budget BIGINT NOT NULL DEFAULT 10000000,
    monthly_cost_budget_usd NUMERIC(12, 4) NOT NULL DEFAULT 1000,
    max_requests_per_minute INT NOT NULL DEFAULT 600,
    max_concurrent_leases INT NOT NULL DEFAULT 100,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS usage_ledger (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    agent_signature VARCHAR(128),
    category VARCHAR(32) NOT NULL,
    amount BIGINT NOT NULL DEFAULT 0,
    cost_usd NUMERIC(12, 6) NOT NULL DEFAULT 0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_usage_ledger_workspace_time
    ON usage_ledger(workspace_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_ledger_category
    ON usage_ledger(workspace_id, category);

CREATE TABLE IF NOT EXISTS rate_limit_windows (
    workspace_id UUID,
    bucket VARCHAR(160) NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    count INT NOT NULL DEFAULT 0,
    PRIMARY KEY (bucket, window_start)
);

-- 9. Persistent webhook trigger rules ---------------------------------------
CREATE TABLE IF NOT EXISTS webhook_rules (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name VARCHAR(128) NOT NULL,
    url TEXT NOT NULL,
    secret VARCHAR(256) NOT NULL,
    event_types TEXT[] NOT NULL DEFAULT ARRAY['*']::text[],
    tables TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
    namespaces TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
    active BOOLEAN NOT NULL DEFAULT TRUE,
    last_event_id BIGINT NOT NULL DEFAULT 0,
    consecutive_failures INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_webhook_rules_workspace
    ON webhook_rules(workspace_id, active);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    rule_id UUID NOT NULL REFERENCES webhook_rules(id) ON DELETE CASCADE,
    event_id BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'SUCCEEDED', 'FAILED')),
    attempts INT NOT NULL DEFAULT 0,
    response_status INT,
    last_error TEXT,
    next_attempt_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivered_at TIMESTAMPTZ,
    UNIQUE(rule_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_pending
    ON webhook_deliveries(status, next_attempt_at);

-- 10. Typed link backfill + trigger ------------------------------------------
UPDATE workspace_links l
SET source_type = 'task', source_task_id = l.source_id
FROM collab_tasks t
WHERE l.workspace_id = t.workspace_id
  AND l.source_id = t.id
  AND l.source_type IS NULL;

UPDATE workspace_links l
SET source_type = 'slot', source_slot_id = l.source_id
FROM context_slots s
WHERE l.workspace_id = s.workspace_id
  AND l.source_id = s.id
  AND l.source_type IS NULL;

UPDATE workspace_links l
SET source_type = 'document', source_doc_id = l.source_id
FROM shared_documents d
WHERE l.workspace_id = d.workspace_id
  AND l.source_id = d.id
  AND l.source_type IS NULL;

UPDATE workspace_links l
SET target_type = 'task', target_task_id = l.target_id
FROM collab_tasks t
WHERE l.workspace_id = t.workspace_id
  AND l.target_id = t.id
  AND l.target_type IS NULL;

UPDATE workspace_links l
SET target_type = 'slot', target_slot_id = l.target_id
FROM context_slots s
WHERE l.workspace_id = s.workspace_id
  AND l.target_id = s.id
  AND l.target_type IS NULL;

UPDATE workspace_links l
SET target_type = 'document', target_doc_id = l.target_id
FROM shared_documents d
WHERE l.workspace_id = d.workspace_id
  AND l.target_id = d.id
  AND l.target_type IS NULL;

-- Links whose endpoints disappeared before typed integrity existed cannot be
-- represented under the new contract. Removing them is safer than leaving
-- dangling graph edges.
DELETE FROM workspace_links
WHERE source_type IS NULL OR target_type IS NULL;

CREATE OR REPLACE FUNCTION fill_workspace_link_types() RETURNS trigger AS $$
BEGIN
    IF NEW.source_type IS NULL THEN
        IF EXISTS (SELECT 1 FROM collab_tasks
                   WHERE id = NEW.source_id AND workspace_id = NEW.workspace_id) THEN
            NEW.source_type := 'task';
        ELSIF EXISTS (SELECT 1 FROM context_slots
                      WHERE id = NEW.source_id AND workspace_id = NEW.workspace_id) THEN
            NEW.source_type := 'slot';
        ELSIF EXISTS (SELECT 1 FROM shared_documents
                      WHERE id = NEW.source_id AND workspace_id = NEW.workspace_id) THEN
            NEW.source_type := 'document';
        ELSE
            RAISE EXCEPTION 'workspace link source % does not exist in workspace %',
                NEW.source_id, NEW.workspace_id;
        END IF;
    END IF;

    IF NEW.target_type IS NULL THEN
        IF EXISTS (SELECT 1 FROM collab_tasks
                   WHERE id = NEW.target_id AND workspace_id = NEW.workspace_id) THEN
            NEW.target_type := 'task';
        ELSIF EXISTS (SELECT 1 FROM context_slots
                      WHERE id = NEW.target_id AND workspace_id = NEW.workspace_id) THEN
            NEW.target_type := 'slot';
        ELSIF EXISTS (SELECT 1 FROM shared_documents
                      WHERE id = NEW.target_id AND workspace_id = NEW.workspace_id) THEN
            NEW.target_type := 'document';
        ELSE
            RAISE EXCEPTION 'workspace link target % does not exist in workspace %',
                NEW.target_id, NEW.workspace_id;
        END IF;
    END IF;

    NEW.source_task_id := CASE WHEN NEW.source_type = 'task' THEN NEW.source_id END;
    NEW.source_slot_id := CASE WHEN NEW.source_type = 'slot' THEN NEW.source_id END;
    NEW.source_doc_id := CASE WHEN NEW.source_type = 'document' THEN NEW.source_id END;
    NEW.target_task_id := CASE WHEN NEW.target_type = 'task' THEN NEW.target_id END;
    NEW.target_slot_id := CASE WHEN NEW.target_type = 'slot' THEN NEW.target_id END;
    NEW.target_doc_id := CASE WHEN NEW.target_type = 'document' THEN NEW.target_id END;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_fill_workspace_link_types ON workspace_links;
CREATE TRIGGER trg_fill_workspace_link_types
    BEFORE INSERT OR UPDATE OF source_id, target_id, source_type, target_type
    ON workspace_links
    FOR EACH ROW EXECUTE FUNCTION fill_workspace_link_types();

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_workspace_links_source_typed'
    ) THEN
        ALTER TABLE workspace_links ADD CONSTRAINT chk_workspace_links_source_typed
            CHECK (
                (source_type = 'task' AND source_task_id IS NOT NULL
                    AND source_slot_id IS NULL AND source_doc_id IS NULL)
                OR (source_type = 'slot' AND source_slot_id IS NOT NULL
                    AND source_task_id IS NULL AND source_doc_id IS NULL)
                OR (source_type = 'document' AND source_doc_id IS NOT NULL
                    AND source_task_id IS NULL AND source_slot_id IS NULL)
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_workspace_links_target_typed'
    ) THEN
        ALTER TABLE workspace_links ADD CONSTRAINT chk_workspace_links_target_typed
            CHECK (
                (target_type = 'task' AND target_task_id IS NOT NULL
                    AND target_slot_id IS NULL AND target_doc_id IS NULL)
                OR (target_type = 'slot' AND target_slot_id IS NOT NULL
                    AND target_task_id IS NULL AND target_doc_id IS NULL)
                OR (target_type = 'document' AND target_doc_id IS NOT NULL
                    AND target_task_id IS NULL AND target_slot_id IS NULL)
            );
    END IF;
END
$$;

-- 11. Outbox triggers for proposal/action changes ----------------------------
-- Keep the outbox payload event_type-aware for webhook matching.
CREATE OR REPLACE FUNCTION notify_workspace_change() RETURNS trigger AS $$
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

DROP TRIGGER IF EXISTS trg_proposals_notify ON document_proposals;
CREATE TRIGGER trg_proposals_notify AFTER INSERT OR UPDATE ON document_proposals
    FOR EACH ROW EXECUTE FUNCTION notify_workspace_change();

DROP TRIGGER IF EXISTS trg_external_actions_notify ON external_actions;
CREATE TRIGGER trg_external_actions_notify AFTER INSERT OR UPDATE ON external_actions
    FOR EACH ROW EXECUTE FUNCTION notify_workspace_change();

-- 12. Tenant role + row-level security ---------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agentgraph_app') THEN
        CREATE ROLE agentgraph_app NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO agentgraph_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO agentgraph_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO agentgraph_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO agentgraph_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO agentgraph_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO agentgraph_app;

DO $$
DECLARE
    target_table TEXT;
    tables TEXT[] := ARRAY[
        'workspaces', 'agent_tokens', 'shared_documents', 'context_slots',
        'collab_tasks', 'workspace_links', 'timeline_streams',
        'state_audit_diffs', 'document_versions', 'workspace_events',
        'document_chunks', 'document_proposals', 'proposal_votes',
        'path_policies', 'human_users', 'human_sessions', 'taint_edges',
        'external_actions', 'external_action_attempts', 'workspace_budgets',
        'usage_ledger', 'webhook_rules', 'webhook_deliveries'
    ];
BEGIN
    FOREACH target_table IN ARRAY tables LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', target_table);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', target_table);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', target_table);
        IF target_table = 'workspaces' THEN
            EXECUTE format(
                'CREATE POLICY tenant_isolation ON %I USING '
                '(id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid '
                'OR current_setting(''app.bypass_rls'', true) = ''on'') '
                'WITH CHECK '
                '(id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid '
                'OR current_setting(''app.bypass_rls'', true) = ''on'')',
                target_table
            );
        ELSE
            EXECUTE format(
                'CREATE POLICY tenant_isolation ON %I USING '
                '(workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid '
                'OR current_setting(''app.bypass_rls'', true) = ''on'') '
                'WITH CHECK '
                '(workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid '
                'OR current_setting(''app.bypass_rls'', true) = ''on'')',
                target_table
            );
        END IF;
    END LOOP;
END
$$;

-- Rate limit counters are intentionally not RLS-protected: they contain no
-- business data and login attempts must be counted before a workspace is known.
