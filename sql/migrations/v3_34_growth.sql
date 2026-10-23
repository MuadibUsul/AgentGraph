-- v3.34 Growth instrumentation and one-command setup (2026-10-23).
--
-- * product_events: an append-only operator log of product moments the
--   platform cannot derive from workspace data (a CLI setup started or
--   finished, a human decision, a click on "Upgrade"). Activation and
--   retention are derived from agent_onboarding and token_access_log; signups
--   from human_users. Rows carry ids and short labels only: never document
--   text, tokens or e-mail addresses.
-- * cli_sessions: the device flow behind `npx veynod init`. The CLI holds the
--   device code (stored here only as a hash) and an X25519 key pair; a signed-in
--   owner approves the short user code in the console; the agent key is minted
--   then and stored only sealed to the CLI's public key until the CLI collects
--   it (the row's sealed copy is cleared on pickup).
--
-- Both tables are operator-only: RLS is forced with no policy for the
-- application role, so tenant-scoped sessions read and write nothing; the
-- service layer uses unscoped sessions for them.

CREATE TABLE IF NOT EXISTS product_events (
    id BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    name VARCHAR(48) NOT NULL,
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id UUID,
    props JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_product_events_name_time ON product_events (name, occurred_at);
CREATE INDEX IF NOT EXISTS idx_product_events_workspace ON product_events (workspace_id, occurred_at);
ALTER TABLE product_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_events FORCE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS cli_sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    device_code_hash VARCHAR(128) NOT NULL UNIQUE,
    user_code VARCHAR(16) NOT NULL UNIQUE,
    client_public_key VARCHAR(64) NOT NULL,
    client_label VARCHAR(64) NOT NULL DEFAULT '',
    client_kinds JSONB NOT NULL DEFAULT '[]'::jsonb,
    status VARCHAR(16) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'denied', 'consumed')),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id UUID,
    agent_signature VARCHAR(128),
    token_id UUID,
    sealed_key TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    decided_at TIMESTAMPTZ,
    consumed_at TIMESTAMPTZ,
    last_polled_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_cli_sessions_expires ON cli_sessions (expires_at);
ALTER TABLE cli_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE cli_sessions FORCE ROW LEVEL SECURITY;
