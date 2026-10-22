-- v3.30 Platform releases (2026-10-23 retest, finding 1).
--
-- One row per build version, recorded by the first replica that starts it:
-- the root primitive list at that version, and what was added or removed
-- since the previous release. Platform-only; the workspace-facing record is
-- the platform.released event and the system/changelog document.

CREATE TABLE IF NOT EXISTS platform_releases (
    version VARCHAR(32) PRIMARY KEY,
    previous_version VARCHAR(32),
    schema_version VARCHAR(32) NOT NULL,
    primitives JSONB NOT NULL DEFAULT '[]'::jsonb,
    added JSONB NOT NULL DEFAULT '[]'::jsonb,
    removed JSONB NOT NULL DEFAULT '[]'::jsonb,
    released_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
REVOKE ALL ON platform_releases FROM agentgraph_app;
