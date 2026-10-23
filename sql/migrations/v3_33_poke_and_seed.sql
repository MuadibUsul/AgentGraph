-- v3.33 Poke delivery and the memory seed (2026-10-23 specs).
--
-- * webhook_deliveries.status also takes CANCELED (an inactive rule; the code
--   already wrote it) and DEAD (every retry failed, poke delivery D-1).
-- * agent_onboarding: registered_at (step 4, register_agent) and the memory
--   seed version the agent reported storing.
-- * agent_directory: source (registered, issued at birth, or auto: filled in
--   by the platform for tokens that never registered) and reach (how to poke
--   the agent: its default subscription, a webhook).

ALTER TABLE webhook_deliveries DROP CONSTRAINT IF EXISTS webhook_deliveries_status_check;
ALTER TABLE webhook_deliveries ADD CONSTRAINT webhook_deliveries_status_check
    CHECK (status IN ('PENDING', 'SUCCEEDED', 'FAILED', 'CANCELED', 'DEAD'));

ALTER TABLE agent_onboarding
    ADD COLUMN IF NOT EXISTS registered_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS memory_seed_version INT,
    ADD COLUMN IF NOT EXISTS memory_seed_acked_at TIMESTAMPTZ;

ALTER TABLE agent_directory
    ADD COLUMN IF NOT EXISTS source VARCHAR(16) NOT NULL DEFAULT 'registered',
    ADD COLUMN IF NOT EXISTS reach JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Onboarding has five steps: step 4 is registration.
ALTER TABLE agent_onboarding DROP CONSTRAINT IF EXISTS agent_onboarding_state_check;
ALTER TABLE agent_onboarding ADD CONSTRAINT agent_onboarding_state_check
    CHECK (state IN ('new', 'oriented', 'needs_ack', 'needs_namespace_readme',
                     'needs_registration', 'needs_first_action', 'active'));
