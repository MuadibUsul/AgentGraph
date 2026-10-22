-- v3.6 failure-semantics protocol (PRD AG-ST-18): idempotency records.
-- Records are scoped by a hash of the presenting credential, so a key can only
-- be replayed by the same principal; rows hold no tenant-readable index.

CREATE TABLE IF NOT EXISTS idempotency_records (
    id BIGSERIAL PRIMARY KEY,
    principal_hash CHAR(64) NOT NULL,
    tool VARCHAR(512) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    request_hash CHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'in_progress'
        CHECK (status IN ('in_progress', 'completed')),
    http_status INT,
    response JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    UNIQUE (principal_hash, tool, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_idempotency_created
    ON idempotency_records (created_at);
