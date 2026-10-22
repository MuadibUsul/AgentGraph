-- v3.16 tenant export and import (PRD AG-ST-02).
-- * exports: one row per export job. row_hashes (gzip JSON) is the per-row
--   hash index an incremental export diffs against.
-- * export_archives: the archive bytes (format agentgraph-export/1, zip).
-- * imports: one row per import, with what it created.
-- These rows outlive a purged tenant (the final export must stay
-- downloadable), so they carry no foreign keys to tenant data and are
-- reached only through the platform session, never through row security.

CREATE TABLE IF NOT EXISTS exports (
    id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('exp'),
    tenant_id VARCHAR(40) NOT NULL,
    workspace_id UUID,
    requested_by VARCHAR(256) NOT NULL,
    purpose VARCHAR(16) NOT NULL DEFAULT 'manual'
        CHECK (purpose IN ('manual', 'final', 'scheduled')),
    scope VARCHAR(16) NOT NULL CHECK (scope IN ('tenant', 'workspace', 'namespace')),
    scope_ref VARCHAR(256) NOT NULL,
    include_subtree BOOLEAN NOT NULL DEFAULT TRUE,
    include_history BOOLEAN NOT NULL DEFAULT TRUE,
    include_groups JSONB,
    kind VARCHAR(16) NOT NULL CHECK (kind IN ('full', 'incremental')),
    base_export_id VARCHAR(40),
    prev_export_id VARCHAR(40),
    seq_from BIGINT,
    seq_to BIGINT,
    as_of TIMESTAMPTZ,
    status VARCHAR(16) NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'done', 'failed')),
    progress DOUBLE PRECISION NOT NULL DEFAULT 0,
    error TEXT,
    manifest_sha256 VARCHAR(64),
    counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    row_hashes BYTEA,
    size_bytes BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_exports_tenant ON exports (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_exports_running ON exports (tenant_id, status);

CREATE TABLE IF NOT EXISTS export_archives (
    export_id VARCHAR(40) PRIMARY KEY REFERENCES exports(id) ON DELETE CASCADE,
    data BYTEA NOT NULL
);

CREATE TABLE IF NOT EXISTS imports (
    id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('imp'),
    tenant_id VARCHAR(40) NOT NULL,
    requested_by VARCHAR(256) NOT NULL,
    target VARCHAR(16) NOT NULL CHECK (target IN ('new_workspace', 'new_tenant', 'increment')),
    verify BOOLEAN NOT NULL DEFAULT TRUE,
    source_export_id VARCHAR(40),
    source_kind VARCHAR(16),
    status VARCHAR(16) NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'done', 'failed')),
    error JSONB,
    counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    manifest_sha256 VARCHAR(64),
    workspace_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_imports_source ON imports (source_export_id);

REVOKE ALL ON exports, export_archives, imports FROM agentgraph_app;
