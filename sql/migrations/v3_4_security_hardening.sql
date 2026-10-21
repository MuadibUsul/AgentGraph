-- v3.4 security hardening: proposal namespaces, Google identity binding,
-- fail-closed tenant policies (no app.bypass_rls escape hatch).

ALTER TABLE document_proposals
    ADD COLUMN IF NOT EXISTS namespace VARCHAR(64);

UPDATE document_proposals p
SET namespace = d.namespace
FROM shared_documents d
WHERE d.workspace_id = p.workspace_id
  AND d.doc_key = p.doc_key
  AND p.namespace IS NULL;

UPDATE document_proposals
SET namespace = 'default'
WHERE namespace IS NULL;

ALTER TABLE document_proposals
    ALTER COLUMN namespace SET DEFAULT 'default',
    ALTER COLUMN namespace SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_proposals_namespace
    ON document_proposals (workspace_id, namespace);

ALTER TABLE human_users
    ADD COLUMN IF NOT EXISTS google_sub VARCHAR(255),
    ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE,
    ALTER COLUMN role SET DEFAULT 'VIEWER';

CREATE UNIQUE INDEX IF NOT EXISTS uq_human_users_google_sub
    ON human_users (google_sub)
    WHERE google_sub IS NOT NULL;

ALTER TABLE state_audit_diffs
    ADD COLUMN IF NOT EXISTS actor_auth_kind VARCHAR(32),
    ADD COLUMN IF NOT EXISTS actor_token_id UUID,
    ADD COLUMN IF NOT EXISTS actor_user_id UUID;

-- New rules start at the current event head; do the same for legacy rules that
-- still point at event 0 so enabling one cannot replay the workspace history.
UPDATE webhook_rules r
SET last_event_id = COALESCE(
    (SELECT MAX(e.id) FROM workspace_events e WHERE e.workspace_id = r.workspace_id),
    0
)
WHERE r.last_event_id = 0;

DO $$
DECLARE
    target_table TEXT;
    tables TEXT[] := ARRAY[
        'workspaces', 'agent_tokens', 'shared_documents', 'context_slots',
        'collab_tasks', 'workspace_links', 'timeline_streams',
        'state_audit_diffs', 'document_versions', 'workspace_events',
        'document_chunks', 'document_proposals', 'proposal_votes',
        'path_policies', 'human_users', 'human_sessions', 'taint_edges',
        'external_actions', 'external_action_attempts', 'workspace_budgets',
        'usage_ledger', 'webhook_rules', 'webhook_deliveries'
    ];
BEGIN
    FOREACH target_table IN ARRAY tables LOOP
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', target_table);
        IF target_table = 'workspaces' THEN
            EXECUTE format(
                'CREATE POLICY tenant_isolation ON %I USING '
                '(id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid) '
                'WITH CHECK '
                '(id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)',
                target_table
            );
        ELSE
            EXECUTE format(
                'CREATE POLICY tenant_isolation ON %I USING '
                '(workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid) '
                'WITH CHECK '
                '(workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)',
                target_table
            );
        END IF;
    END LOOP;
END
$$;
