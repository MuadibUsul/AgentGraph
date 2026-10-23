-- v3.31 Per-token permission deltas (token delegation 1.0, stage A).
--
-- A token's effective permissions are its role package plus scope_add minus
-- scope_remove (permission names, e.g. doc:create, proposal:vote), narrowed
-- by the chain's coarse scopes as before. Removals accumulate down the
-- token tree: a child inherits its issuer's delta and cannot undo a removal.

ALTER TABLE agent_tokens
    ADD COLUMN IF NOT EXISTS scope_add JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS scope_remove JSONB NOT NULL DEFAULT '[]'::jsonb;
