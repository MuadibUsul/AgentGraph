-- v3.3 agent feedback: soft archive support.
ALTER TABLE shared_documents
    ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS archived_by VARCHAR(128),
    ADD COLUMN IF NOT EXISTS archive_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_docs_archived
    ON shared_documents (workspace_id, archived_at);
