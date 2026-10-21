-- v3.10 review rounds (PRD AG-CO-01): review_feedback means "feedback not yet
-- addressed"; earlier rounds move to review_history. The executor's lease
-- expiry at handoff is kept so a rework (next_status=IN_PROGRESS) can return
-- the task to the same executor while that lease would still be valid.

ALTER TABLE collab_tasks
    ADD COLUMN IF NOT EXISTS review_round INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS review_history JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS handoff_lease_expires_at TIMESTAMPTZ;
