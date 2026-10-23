-- v3.28 Phase 3 R3.2 (AG-ST-12, AG-ID-08).
--
-- * retrieval_eval_runs: one row per evaluation run (recall@k, MRR, index
--   size), kept for comparison with earlier runs. Platform-only.
-- * retrieval_probes: the daily self_recall@50 probe per workspace.
--   Platform-only.
-- * tenant_signing_keys: each tenant's Ed25519 keys for lineage attestations
--   and the secret behind its pairwise pseudonyms. Platform-only; private keys
--   never leave the platform session and are never exported.

CREATE TABLE IF NOT EXISTS retrieval_eval_runs (
    run_id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('evr'),
    dataset VARCHAR(64) NOT NULL,
    corpus_hash VARCHAR(32) NOT NULL,
    ks INT[] NOT NULL,
    metrics JSONB NOT NULL,
    n_queries INT NOT NULL,
    index_size INT NOT NULL,
    embedding_provider VARCHAR(32) NOT NULL,
    embedding_dimensions INT NOT NULL,
    duration_ms INT NOT NULL,
    misses JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_retrieval_eval_runs_dataset
    ON retrieval_eval_runs (dataset, created_at DESC);

CREATE TABLE IF NOT EXISTS retrieval_probes (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    sampled INT NOT NULL,
    hits INT NOT NULL,
    self_recall NUMERIC(5, 4) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_retrieval_probes_workspace
    ON retrieval_probes (workspace_id, created_at DESC);
DROP TRIGGER IF EXISTS trg_derive_tenant ON retrieval_probes;
CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
    ON retrieval_probes FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();

CREATE TABLE IF NOT EXISTS tenant_signing_keys (
    kid VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(40) NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    private_key TEXT NOT NULL,
    public_x TEXT NOT NULL,
    pseudonym_secret TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retired_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_tenant_signing_keys_tenant
    ON tenant_signing_keys (tenant_id, created_at DESC);

REVOKE ALL ON retrieval_eval_runs FROM agentgraph_app;
REVOKE ALL ON retrieval_probes FROM agentgraph_app;
REVOKE ALL ON tenant_signing_keys FROM agentgraph_app;
