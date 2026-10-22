-- v3.22 human members and roles (PRD AG-ID-10, single node; tree inheritance
-- of owner/admin is computed from tenants.path).
--
-- * memberships gains invited_by; a membership is what lets a person use a
--   node's workspaces (their home workspace counts as shared with them).
-- * human_users.role gains ADMIN (the account role mirrors the home membership).
-- * member_invitations: invitations for people without an account yet; the
--   token is stored hashed and expires after 7 days.

ALTER TABLE memberships ADD COLUMN IF NOT EXISTS invited_by VARCHAR(256);

DO $$
DECLARE
    constraint_name TEXT;
BEGIN
    FOR constraint_name IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'human_users'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%OWNER%'
    LOOP
        EXECUTE format('ALTER TABLE human_users DROP CONSTRAINT %I', constraint_name);
    END LOOP;
END
$$;
ALTER TABLE human_users ADD CONSTRAINT human_users_role_check
    CHECK (role IN ('OWNER', 'ADMIN', 'REVIEWER', 'VIEWER'));

CREATE TABLE IF NOT EXISTS member_invitations (
    id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('inv'),
    tenant_id VARCHAR(40) NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email VARCHAR(320) NOT NULL,
    role VARCHAR(16) NOT NULL CHECK (role IN ('owner', 'admin', 'member', 'guest')),
    membership_expires_at TIMESTAMPTZ,
    token_hash VARCHAR(128) NOT NULL UNIQUE,
    invited_by VARCHAR(256) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    accepted_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_member_invitations_tenant
    ON member_invitations (tenant_id, lower(email));

-- ------------------------------------------- rights requests (AG-TC-07)
-- A person's request to see, copy, correct or erase their data in a node.
-- subject identifiers are replaced by hashes 30 days after closing.
CREATE TABLE IF NOT EXISTS rights_requests (
    id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('rr'),
    tenant_id VARCHAR(40) NOT NULL,
    include_subtree BOOLEAN NOT NULL DEFAULT TRUE,
    scope VARCHAR(16) NOT NULL DEFAULT 'customer' CHECK (scope IN ('customer', 'platform')),
    subject JSONB NOT NULL,
    subject_hash VARCHAR(64),
    type VARCHAR(16) NOT NULL CHECK (type IN
        ('access', 'copy', 'portability', 'correction', 'deletion', 'explanation')),
    status VARCHAR(16) NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'located', 'fulfilled', 'rejected', 'closed')),
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    due_at TIMESTAMPTZ NOT NULL,
    handler VARCHAR(256) NOT NULL,
    notes TEXT,
    inventory JSONB,
    located_at TIMESTAMPTZ,
    counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    reminders_sent JSONB NOT NULL DEFAULT '[]'::jsonb,
    closed_at TIMESTAMPTZ,
    minimized_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rights_requests_tenant ON rights_requests (tenant_id, status);

CREATE TABLE IF NOT EXISTS rights_request_events (
    id BIGSERIAL PRIMARY KEY,
    request_id VARCHAR(40) NOT NULL REFERENCES rights_requests(id) ON DELETE CASCADE,
    at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kind VARCHAR(64) NOT NULL,
    actor VARCHAR(256) NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS rights_exports (
    request_id VARCHAR(40) PRIMARY KEY REFERENCES rights_requests(id) ON DELETE CASCADE,
    data BYTEA NOT NULL,
    manifest_sha256 VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

-- Append-only record of every redaction and pseudonymisation, replayed after
-- a restore so erased data never comes back. It stores the result (the
-- redacted text, the pseudonym), never the original.
CREATE TABLE IF NOT EXISTS redaction_log (
    id BIGSERIAL PRIMARY KEY,
    request_id VARCHAR(40),
    tenant_id VARCHAR(40) NOT NULL,
    kind VARCHAR(16) NOT NULL CHECK (kind IN ('pseudonymise', 'redact', 'delete')),
    target_table VARCHAR(64) NOT NULL,
    target_id TEXT NOT NULL,
    version INT,
    field VARCHAR(64),
    replacement JSONB NOT NULL DEFAULT '{}'::jsonb,
    redaction_hash VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_redaction_log_tenant ON redaction_log (tenant_id, id);

CREATE OR REPLACE FUNCTION agentgraph_redaction_log_append_only() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'the redaction log is append-only' USING ERRCODE = 'insufficient_privilege';
END;
$$;
DROP TRIGGER IF EXISTS trg_redaction_log_append_only ON redaction_log;
CREATE TRIGGER trg_redaction_log_append_only BEFORE UPDATE OR DELETE ON redaction_log
    FOR EACH ROW EXECUTE FUNCTION agentgraph_redaction_log_append_only();

ALTER TABLE document_versions
    ADD COLUMN IF NOT EXISTS redacted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS redaction_hash VARCHAR(64);
ALTER TABLE shared_documents
    ADD COLUMN IF NOT EXISTS redacted_at TIMESTAMPTZ;
