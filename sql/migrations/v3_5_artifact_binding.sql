-- v3.5 artifact binding: pin the reviewed artifact version/hash at handoff
-- and record who handed the task off, so review approval cannot verify a
-- different document version and an agent cannot approve its own handoff.

ALTER TABLE collab_tasks
    ADD COLUMN IF NOT EXISTS artifact_version INT,
    ADD COLUMN IF NOT EXISTS artifact_hash VARCHAR(64),
    ADD COLUMN IF NOT EXISTS handed_off_by VARCHAR(128);
