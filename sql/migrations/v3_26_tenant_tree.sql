-- v3.26 the tenant tree in use (PRD Phase 2 R2.4: AG-ID-09, ID-10, ID-11,
-- ID-12, ID-13).
--
-- * Four levels (company → organization → organization → personal): the
--   root counts as depth 1, so "depth ≤ 3 from the root" is depth ≤ 4 here.
-- * Moving or attaching a subtree re-derives path, root and depth for every
--   descendant in the same statement (ids and aliases never change).
-- * tenant_policies: inherited settings, only ever tightened down the tree.
-- * tenant_confirmations: two-owner confirmations for move/attach/detach.
-- * tenant_shares: read/write grants inside one tree; admin_access_sessions:
--   audited, time-boxed content access for ancestor owners and admins.

ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_depth_check;
ALTER TABLE tenants ADD CONSTRAINT tenants_depth_check CHECK (depth BETWEEN 1 AND 4);
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS billing_account VARCHAR(128),
    ADD COLUMN IF NOT EXISTS detached_from VARCHAR(40);

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
           AND NEW.tenant_type = OLD.tenant_type
           AND NEW.path IS NOT DISTINCT FROM OLD.path THEN
            NEW.updated_at := NOW();
            RETURN NEW;
        END IF;
        IF NEW.tenant_type = 'personal' AND OLD.tenant_type <> 'personal'
           AND EXISTS (SELECT 1 FROM tenants WHERE parent_tenant_id = NEW.id) THEN
            RAISE EXCEPTION 'tenant_type_invalid_parent: personal tenants have no children'
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
        IF NEW.tenant_type = 'company' THEN
            RAISE EXCEPTION 'tenant_type_invalid_parent: a company is always a root'
                USING ERRCODE = 'check_violation';
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
    IF NEW.billing_owner_tenant_id IS NULL
       OR (TG_OP = 'UPDATE' AND NEW.root_tenant_id IS DISTINCT FROM OLD.root_tenant_id
           AND NEW.billing_account IS NULL) THEN
        NEW.billing_owner_tenant_id := NEW.root_tenant_id;
    END IF;
    IF NEW.depth > 4 THEN
        RAISE EXCEPTION 'tenant_depth_exceeded: at most 3 levels below the root'
            USING ERRCODE = 'check_violation';
    END IF;
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;

-- After a node's path changes, re-derive its children (each update cascades).
CREATE OR REPLACE FUNCTION agentgraph_tenant_tree_cascade() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.path IS DISTINCT FROM OLD.path THEN
        UPDATE tenants SET path = NULL WHERE parent_tenant_id = NEW.id;
    END IF;
    RETURN NULL;
END;
$$;
-- AFTER UPDATE (not UPDATE OF path): the BEFORE trigger rewrites path when
-- only parent_tenant_id was set, and column lists only see the SET clause.
DROP TRIGGER IF EXISTS trg_tenant_tree_cascade ON tenants;
CREATE TRIGGER trg_tenant_tree_cascade
    AFTER UPDATE ON tenants
    FOR EACH ROW EXECUTE FUNCTION agentgraph_tenant_tree_cascade();

CREATE TABLE IF NOT EXISTS tenant_policies (
    tenant_id VARCHAR(40) NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    key VARCHAR(64) NOT NULL,
    value JSONB NOT NULL,
    locked BOOLEAN NOT NULL DEFAULT FALSE,
    updated_by VARCHAR(256),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, key)
);

CREATE TABLE IF NOT EXISTS tenant_confirmations (
    id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('cnf'),
    action VARCHAR(16) NOT NULL CHECK (action IN ('move', 'attach', 'detach')),
    node_id VARCHAR(40) NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    target_id VARCHAR(40) REFERENCES tenants(id) ON DELETE CASCADE,
    required TEXT[] NOT NULL,
    confirmed TEXT[] NOT NULL DEFAULT '{}',
    requested_by VARCHAR(256) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '7 days',
    done_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS tenant_shares (
    id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('shr'),
    root_tenant_id VARCHAR(40) NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- The owning node (the shared resource's tenant), derived from the workspace.
    tenant_id VARCHAR(40) NOT NULL,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    resource_kind VARCHAR(16) NOT NULL
        CHECK (resource_kind IN ('document', 'namespace', 'workspace', 'task')),
    resource_ref VARCHAR(256) NOT NULL,
    grantee_kind VARCHAR(16) NOT NULL CHECK (grantee_kind IN ('node', 'principal')),
    grantee_ref VARCHAR(256) NOT NULL,
    access VARCHAR(8) NOT NULL CHECK (access IN ('read', 'write')),
    expires_at TIMESTAMPTZ,
    created_by VARCHAR(256) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ
);
DROP TRIGGER IF EXISTS trg_derive_tenant ON tenant_shares;
CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
    ON tenant_shares FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();
CREATE INDEX IF NOT EXISTS idx_tenant_shares_grantee
    ON tenant_shares (grantee_kind, grantee_ref) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS admin_access_sessions (
    id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('acs'),
    tenant_id VARCHAR(40) NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    opened_by UUID NOT NULL,
    opened_by_label VARCHAR(256) NOT NULL,
    reason TEXT NOT NULL,
    opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ,
    reads INT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_admin_access_open
    ON admin_access_sessions (opened_by, expires_at) WHERE closed_at IS NULL;

-- Platform tables: read and written by the platform session only.
REVOKE ALL ON tenant_policies, tenant_confirmations, tenant_shares, admin_access_sessions
    FROM agentgraph_app;

-- An export of a node may be delivered to an ancestor (the requester) or to
-- the tree a node detached from (AG-ID-13).
ALTER TABLE exports ADD COLUMN IF NOT EXISTS deliver_to TEXT[] NOT NULL DEFAULT '{}';
