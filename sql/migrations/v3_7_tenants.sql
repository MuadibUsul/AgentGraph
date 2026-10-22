-- v3.7 tenant dimension (PRD AG-ID-07, day-zero contract 2).
--
-- * tenants: one node of the tenant tree (personal / organization / company,
--   depth <= 3). Type/parent rules, cycles and depth are enforced in the
--   database; path, root_tenant_id and depth are maintained by a trigger.
-- * Every tenant-owned table gets a NOT NULL tenant_id that a trigger derives
--   from the owning workspace: clients can never supply it.
-- * memberships: human roles on a tenant node (owner/admin/member/guest).
-- * Row security now requires the tenant scope (app.tenant_scope) in addition
--   to the workspace, so a connection without tenant context sees nothing.
--
-- Existing workspaces each become their own root organization tenant. The PRD
-- suggests one ten_default for all existing data, but self-service signup means
-- existing workspaces belong to different customers; merging them would put
-- strangers under one governance root.

-- ------------------------------------------------------------------ ids
-- Opaque canonical ids: <prefix>_<ULID> (48-bit ms time + 80 random bits,
-- Crockford base32). Shared with the application's app/ids.py.
CREATE OR REPLACE FUNCTION agentgraph_new_id(prefix TEXT) RETURNS TEXT
LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    alphabet CONSTANT TEXT := '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
    ms BIGINT := floor(extract(epoch FROM clock_timestamp()) * 1000);
    out TEXT := '';
    i INT;
BEGIN
    FOR i IN REVERSE 9..0 LOOP
        out := out || substr(alphabet, ((ms >> (i * 5)) & 31)::INT + 1, 1);
    END LOOP;
    FOR i IN 1..16 LOOP
        out := out || substr(alphabet, floor(random() * 32)::INT + 1, 1);
    END LOOP;
    RETURN prefix || '_' || out;
END;
$$;

-- --------------------------------------------------------------- tenants
CREATE TABLE IF NOT EXISTS tenants (
    id VARCHAR(40) PRIMARY KEY,
    alias VARCHAR(64) NOT NULL UNIQUE,
    display_name VARCHAR(256) NOT NULL,
    tenant_type VARCHAR(16) NOT NULL
        CHECK (tenant_type IN ('personal', 'organization', 'company')),
    parent_tenant_id VARCHAR(40) REFERENCES tenants(id),
    root_tenant_id VARCHAR(40) NOT NULL,
    path VARCHAR(40)[] NOT NULL,
    depth INT NOT NULL CHECK (depth BETWEEN 1 AND 3),
    billing_owner_tenant_id VARCHAR(40) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'suspended', 'deleting', 'deleted')),
    status_reason TEXT,
    status_changed_at TIMESTAMPTZ,
    deletion_admin_confirmed_at TIMESTAMPTZ,
    deletion_owner_confirmed_at TIMESTAMPTZ,
    deletion_owner_confirmed_by VARCHAR(256),
    purge_after TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ,
    final_export_id VARCHAR(64),
    signing_key_id VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT tenant_company_is_root
        CHECK (tenant_type <> 'company' OR parent_tenant_id IS NULL),
    CONSTRAINT tenant_alias_syntax CHECK (alias ~ '^[a-z0-9][a-z0-9-]{0,63}$')
);

CREATE INDEX IF NOT EXISTS idx_tenants_parent ON tenants (parent_tenant_id);
CREATE INDEX IF NOT EXISTS idx_tenants_root ON tenants (root_tenant_id);
CREATE INDEX IF NOT EXISTS idx_tenants_purge ON tenants (status, purge_after);

CREATE OR REPLACE FUNCTION agentgraph_tenant_tree() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    parent tenants%ROWTYPE;
BEGIN
    IF NEW.id IS NULL THEN
        NEW.id := agentgraph_new_id('ten');
    END IF;
    IF NEW.signing_key_id IS NULL THEN
        NEW.signing_key_id := agentgraph_new_id('key');
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NEW.id <> OLD.id THEN
            RAISE EXCEPTION 'tenant ids never change' USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.parent_tenant_id IS NOT DISTINCT FROM OLD.parent_tenant_id
           AND NEW.tenant_type = OLD.tenant_type THEN
            NEW.updated_at := NOW();
            RETURN NEW;
        END IF;
        IF EXISTS (SELECT 1 FROM tenants WHERE parent_tenant_id = NEW.id) THEN
            -- Moving a subtree re-roots descendants (AG-ID-09); not supported here.
            RAISE EXCEPTION 'tenant_has_children: move or retype a leaf only'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF NEW.parent_tenant_id IS NULL THEN
        NEW.root_tenant_id := NEW.id;
        NEW.path := ARRAY[NEW.id]::VARCHAR(40)[];
        NEW.depth := 1;
    ELSE
        SELECT * INTO parent FROM tenants WHERE id = NEW.parent_tenant_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'parent tenant % not found', NEW.parent_tenant_id
                USING ERRCODE = 'foreign_key_violation';
        END IF;
        IF parent.tenant_type = 'personal' THEN
            RAISE EXCEPTION 'tenant_type_invalid_parent: personal tenants have no children'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.tenant_type = 'organization'
           AND parent.tenant_type NOT IN ('company', 'organization') THEN
            RAISE EXCEPTION 'tenant_type_invalid_parent: organization needs a company or organization parent'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.id = ANY(parent.path) THEN
            RAISE EXCEPTION 'tenant_cycle' USING ERRCODE = 'check_violation';
        END IF;
        NEW.root_tenant_id := parent.root_tenant_id;
        NEW.path := parent.path || NEW.id::VARCHAR(40);
        NEW.depth := parent.depth + 1;
    END IF;
    IF NEW.billing_owner_tenant_id IS NULL THEN
        NEW.billing_owner_tenant_id := NEW.root_tenant_id;
    END IF;
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_tenant_tree ON tenants;
CREATE TRIGGER trg_tenant_tree
    BEFORE INSERT OR UPDATE ON tenants
    FOR EACH ROW EXECUTE FUNCTION agentgraph_tenant_tree();

-- ------------------------------------------------------------ workspaces
ALTER TABLE workspaces
    ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(40) REFERENCES tenants(id);

CREATE OR REPLACE FUNCTION agentgraph_tenant_alias(seed TEXT, tenant_id TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$
    SELECT left(
        COALESCE(
            NULLIF(trim(BOTH '-' FROM regexp_replace(lower(COALESCE(seed, '')), '[^a-z0-9]+', '-', 'g')), ''),
            'tenant'
        ),
        40
    ) || '-' || lower(right(tenant_id, 8))
$$;

-- A workspace created without a tenant gets its own root organization tenant.
-- SECURITY DEFINER so the insert works while the app role is row-restricted.
CREATE OR REPLACE FUNCTION agentgraph_workspace_tenant() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
    new_tenant TEXT;
BEGIN
    IF NEW.tenant_id IS NULL THEN
        new_tenant := agentgraph_new_id('ten');
        INSERT INTO tenants (id, alias, display_name, tenant_type)
        VALUES (
            new_tenant,
            agentgraph_tenant_alias(NEW.name, new_tenant),
            COALESCE(NULLIF(NEW.name, ''), 'Tenant'),
            'organization'
        );
        NEW.tenant_id := new_tenant;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_workspace_tenant ON workspaces;
CREATE TRIGGER trg_workspace_tenant
    BEFORE INSERT ON workspaces
    FOR EACH ROW EXECUTE FUNCTION agentgraph_workspace_tenant();

-- Backfill: one root organization tenant per existing workspace.
DO $$
DECLARE
    ws RECORD;
    new_tenant TEXT;
BEGIN
    FOR ws IN SELECT id, name FROM workspaces WHERE tenant_id IS NULL ORDER BY created_at LOOP
        new_tenant := agentgraph_new_id('ten');
        INSERT INTO tenants (id, alias, display_name, tenant_type)
        VALUES (
            new_tenant,
            agentgraph_tenant_alias(ws.name, new_tenant),
            COALESCE(NULLIF(ws.name, ''), 'Tenant'),
            'organization'
        );
        UPDATE workspaces SET tenant_id = new_tenant WHERE id = ws.id;
    END LOOP;
END
$$;

ALTER TABLE workspaces ALTER COLUMN tenant_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_workspaces_tenant ON workspaces (tenant_id);

-- --------------------------------------------- tenant_id on owned tables
CREATE OR REPLACE FUNCTION agentgraph_derive_tenant() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
    -- Always derived from the owning workspace, never trusted from the writer.
    NEW.tenant_id := (SELECT tenant_id FROM workspaces WHERE id = NEW.workspace_id);
    IF NEW.tenant_id IS NULL THEN
        RAISE EXCEPTION 'workspace % has no tenant', NEW.workspace_id
            USING ERRCODE = 'not_null_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION agentgraph_derive_root_tenant() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
    NEW.tenant_id := (SELECT tenant_id FROM workspaces WHERE id = NEW.workspace_id);
    NEW.root_tenant_id := (SELECT root_tenant_id FROM tenants WHERE id = NEW.tenant_id);
    IF NEW.tenant_id IS NULL THEN
        RAISE EXCEPTION 'workspace % has no tenant', NEW.workspace_id
            USING ERRCODE = 'not_null_violation';
    END IF;
    RETURN NEW;
END;
$$;

ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS root_tenant_id VARCHAR(40);
ALTER TABLE state_audit_diffs ADD COLUMN IF NOT EXISTS root_tenant_id VARCHAR(40);

DO $$
DECLARE
    target_table TEXT;
    rooted TEXT[] := ARRAY['usage_ledger', 'state_audit_diffs'];
    tables TEXT[] := ARRAY[
        'agent_tokens', 'shared_documents', 'context_slots', 'collab_tasks',
        'workspace_links', 'timeline_streams', 'state_audit_diffs',
        'document_versions', 'workspace_events', 'document_chunks',
        'document_proposals', 'proposal_votes', 'path_policies', 'human_users',
        'human_sessions', 'taint_edges', 'external_actions',
        'external_action_attempts', 'workspace_budgets', 'usage_ledger',
        'webhook_rules', 'webhook_deliveries'
    ];
BEGIN
    FOREACH target_table IN ARRAY tables LOOP
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(40)', target_table);
        EXECUTE format(
            'UPDATE %I t SET tenant_id = w.tenant_id FROM workspaces w '
            'WHERE w.id = t.workspace_id AND t.tenant_id IS DISTINCT FROM w.tenant_id',
            target_table
        );
        IF target_table = ANY(rooted) THEN
            EXECUTE format(
                'UPDATE %I t SET root_tenant_id = tn.root_tenant_id FROM tenants tn '
                'WHERE tn.id = t.tenant_id', target_table
            );
            EXECUTE format('ALTER TABLE %I ALTER COLUMN root_tenant_id SET NOT NULL', target_table);
        END IF;
        EXECUTE format('ALTER TABLE %I ALTER COLUMN tenant_id SET NOT NULL', target_table);
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON %I (tenant_id, workspace_id)',
            'idx_' || target_table || '_tenant', target_table
        );
        EXECUTE format('DROP TRIGGER IF EXISTS trg_derive_tenant ON %I', target_table);
        EXECUTE format(
            'CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id '
            'ON %I FOR EACH ROW EXECUTE FUNCTION %I()',
            target_table,
            CASE WHEN target_table = ANY(rooted)
                 THEN 'agentgraph_derive_root_tenant' ELSE 'agentgraph_derive_tenant' END
        );
    END LOOP;
END
$$;

-- ------------------------------------------------------------ memberships
CREATE TABLE IF NOT EXISTS memberships (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id VARCHAR(40) NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    principal_id UUID NOT NULL REFERENCES human_users(id) ON DELETE CASCADE,
    role VARCHAR(16) NOT NULL CHECK (role IN ('owner', 'admin', 'member', 'guest')),
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, principal_id)
);

CREATE INDEX IF NOT EXISTS idx_memberships_principal ON memberships (principal_id);

-- Existing human accounts: OWNER -> owner, REVIEWER -> member, VIEWER -> guest.
INSERT INTO memberships (tenant_id, principal_id, role)
SELECT u.tenant_id, u.id,
       CASE u.role WHEN 'OWNER' THEN 'owner' WHEN 'REVIEWER' THEN 'member' ELSE 'guest' END
FROM human_users u
ON CONFLICT (tenant_id, principal_id) DO NOTHING;

-- -------------------------------------------------------- row security
-- Policies require BOTH the tenant scope and the workspace. app.tenant_scope
-- is a comma-separated list of tenant ids the request may access (one id in
-- Phase 1; shares and admin access sessions add nodes in Phase 2).
CREATE OR REPLACE FUNCTION agentgraph_tenant_scope() RETURNS TEXT[]
LANGUAGE sql STABLE AS $$
    SELECT string_to_array(NULLIF(current_setting('app.tenant_scope', true), ''), ',')
$$;

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
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', target_table);
        IF target_table = 'workspaces' THEN
            EXECUTE format(
                'CREATE POLICY tenant_isolation ON %I USING '
                '(tenant_id = ANY(agentgraph_tenant_scope()) AND '
                'id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid) '
                'WITH CHECK '
                '(tenant_id = ANY(agentgraph_tenant_scope()) AND '
                'id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)',
                target_table
            );
        ELSE
            EXECUTE format(
                'CREATE POLICY tenant_isolation ON %I USING '
                '(tenant_id = ANY(agentgraph_tenant_scope()) AND '
                'workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid) '
                'WITH CHECK '
                '(tenant_id = ANY(agentgraph_tenant_scope()) AND '
                'workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)',
                target_table
            );
        END IF;
    END LOOP;
END
$$;

ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON tenants;
CREATE POLICY tenant_isolation ON tenants
    USING (id = ANY(agentgraph_tenant_scope()))
    WITH CHECK (id = ANY(agentgraph_tenant_scope()));

ALTER TABLE memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE memberships FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON memberships;
CREATE POLICY tenant_isolation ON memberships
    USING (tenant_id = ANY(agentgraph_tenant_scope()))
    WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope()));

-- Idempotency records are owned by the tenant of the presenting credential.
ALTER TABLE idempotency_records ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(40);
DELETE FROM idempotency_records WHERE tenant_id IS NULL;
ALTER TABLE idempotency_records ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE idempotency_records
    DROP CONSTRAINT IF EXISTS idempotency_records_principal_hash_tool_idempotency_key_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_idempotency_scope
    ON idempotency_records (tenant_id, principal_hash, tool, idempotency_key);

GRANT SELECT, INSERT, UPDATE, DELETE ON tenants, memberships TO agentgraph_app;
GRANT EXECUTE ON FUNCTION agentgraph_tenant_scope() TO agentgraph_app;
