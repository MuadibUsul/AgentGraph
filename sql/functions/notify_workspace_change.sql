-- Current outbox trigger function (PRD AG-EV-01). Applied on every start,
-- after init.sql and all migrations: several older migrations redefine this
-- function, so the last definition applied must be this one.
CREATE OR REPLACE FUNCTION notify_workspace_change() RETURNS trigger AS $$
-- The outbox (PRD AG-EV-01): one event per meaningful change, written in the
-- same transaction. Its seq is assigned at commit by the deferred trigger
-- agentgraph_assign_event_seq (v3_17), so the counter row is locked only for
-- the instant of commit: seq is gap-free and follows commit order without
-- serializing a workspace's writers for the length of their transactions.
DECLARE
    row_data JSONB;
    old_data JSONB;
    event_payload JSONB;
    new_event_id BIGINT;
    event_type TEXT;
    event_targets JSONB := '[]'::jsonb;
    event_refs JSONB := '{}'::jsonb;
    event_label TEXT;
    ws UUID;
BEGIN
    row_data := to_jsonb(NEW);
    IF TG_OP = 'UPDATE' THEN
        old_data := to_jsonb(OLD);
    END IF;
    ws := (row_data->>'workspace_id')::uuid;

    IF TG_TABLE_NAME = 'shared_documents' THEN
        IF TG_OP = 'INSERT' THEN
            event_type := 'doc.created';
        ELSIF old_data->>'doc_key' IS DISTINCT FROM row_data->>'doc_key' THEN
            event_type := 'doc.renamed';
        ELSIF old_data->>'archived_at' IS NULL AND row_data->>'archived_at' IS NOT NULL THEN
            event_type := 'doc.archived';
        ELSIF old_data->>'archived_at' IS NOT NULL AND row_data->>'archived_at' IS NULL THEN
            event_type := 'doc.restored';
        ELSIF old_data->'locked_by_human' IS DISTINCT FROM row_data->'locked_by_human' THEN
            event_type := CASE WHEN (row_data->>'locked_by_human')::boolean
                               THEN 'doc.locked' ELSE 'doc.unlocked' END;
        ELSIF old_data->'version' IS DISTINCT FROM row_data->'version' THEN
            event_type := 'doc.updated';
        ELSIF old_data->'verification_status' IS DISTINCT FROM row_data->'verification_status'
              OR old_data->'tainted' IS DISTINCT FROM row_data->'tainted' THEN
            event_type := 'doc.trust_changed';
        ELSE
            RETURN NEW;  -- embedding or index bookkeeping: not a change anyone acts on
        END IF;
        event_refs := jsonb_build_object(
            'doc_id', row_data->>'canonical_id', 'alias', row_data->>'doc_key',
            'version', (row_data->>'version')::int);
        IF row_data->'frontmatter'->>'maintainer' IS NOT NULL THEN
            event_targets := jsonb_build_array(row_data->'frontmatter'->>'maintainer');
        END IF;
        event_label := row_data->>'doc_key';
    ELSIF TG_TABLE_NAME = 'collab_tasks' THEN
        IF TG_OP = 'INSERT' THEN
            event_type := CASE WHEN row_data->>'assignee' IS NOT NULL
                               THEN 'task.assigned' ELSE 'task.created' END;
        ELSIF old_data->>'assignee' IS DISTINCT FROM row_data->>'assignee'
              AND row_data->>'assignee' IS NOT NULL THEN
            event_type := 'task.assigned';
        ELSIF row_data->>'lease_owner' IS NOT NULL
              AND old_data->'lease_epoch' IS DISTINCT FROM row_data->'lease_epoch' THEN
            event_type := 'task.claimed';
        ELSIF old_data->>'status' IS DISTINCT FROM row_data->>'status' THEN
            event_type := 'task.status_changed';
        ELSIF old_data->'version' IS DISTINCT FROM row_data->'version' THEN
            event_type := 'task.updated';
        ELSE
            RETURN NEW;
        END IF;
        IF row_data->>'assignee' IS NOT NULL THEN
            event_targets := jsonb_build_array(row_data->>'assignee');
        END IF;
        event_refs := jsonb_build_object(
            'task_id', row_data->>'canonical_id', 'alias', row_data->>'task_key',
            'version', (row_data->>'version')::int, 'status', row_data->>'status');
        event_label := row_data->>'task_key';
    ELSIF TG_TABLE_NAME = 'context_slots' THEN
        event_type := CASE WHEN TG_OP = 'INSERT' THEN 'slot.created' ELSE 'slot.updated' END;
        event_refs := jsonb_build_object(
            'slot_id', row_data->>'canonical_id', 'alias', row_data->>'slot_key',
            'version', (row_data->>'version')::int);
        event_label := row_data->>'slot_key';
    ELSIF TG_TABLE_NAME = 'document_proposals' THEN
        event_type := CASE WHEN TG_OP = 'INSERT' THEN 'proposal.created'
                           WHEN old_data->>'status' IS DISTINCT FROM row_data->>'status'
                                AND row_data->>'status' NOT IN ('OPEN', 'PENDING')
                           THEN 'proposal.resolved'
                           ELSE 'proposal.updated' END;
        event_refs := jsonb_build_object('proposal_id', row_data->>'id',
                                         'alias', row_data->>'doc_key');
        event_label := row_data->>'doc_key';
    ELSIF TG_TABLE_NAME = 'external_actions' THEN
        event_type := CASE WHEN TG_OP = 'INSERT' THEN 'action.created' ELSE 'action.updated' END;
        event_refs := jsonb_build_object('action_id', row_data->>'id',
                                         'status', row_data->>'status');
        event_label := row_data->>'action_type';
    ELSIF TG_TABLE_NAME = 'timeline_streams' THEN
        -- Application events. Those that repeat a row change above stay under
        -- timeline.* so a doc.* or task.* subscriber gets each change once.
        event_type := CASE upper(COALESCE(row_data->>'event_type', ''))
            WHEN 'DOC_INDEXED' THEN 'doc.indexed'
            WHEN 'DOC_INDEX_FAILED' THEN 'doc.index_failed'
            WHEN 'DOC_PURGED' THEN 'doc.purged'
            WHEN 'LEASE_EXPIRED' THEN 'task.lease_expired'
            WHEN 'TASK_HEARTBEAT_MISSED' THEN 'task.heartbeat_missed'
            WHEN 'TASK_HANDOFF' THEN 'task.handoff'
            WHEN 'TASK_APPROVED' THEN 'task.reviewed'
            WHEN 'TASK_REJECTED' THEN 'task.reviewed'
            WHEN 'HUMAN_APPROVED' THEN 'task.reviewed'
            WHEN 'TASK_ESCALATED' THEN 'escalation.created'
            WHEN 'HUMAN_REVIEW_REQUESTED' THEN 'human_review.requested'
            WHEN 'PROPOSAL_VOTED' THEN 'proposal.voted'
            WHEN 'BUDGET_CIRCUIT_OPEN' THEN 'budget.circuit_open'
            WHEN 'RESPONSIBILITIES_TRANSFERRED' THEN 'responsibilities.transferred'
            WHEN 'SECURITY_INCIDENT_NOTICE' THEN 'security.incident_notice'
            WHEN 'TOKEN_ROOT_USED' THEN 'token.root_used'
            WHEN 'TOKEN_ISSUED' THEN 'token.issued'
            WHEN 'TOKEN_REVOKED' THEN 'token.revoked'
            WHEN 'TOKEN_EXPIRING' THEN 'token.expiring'
            WHEN 'AGENT_REGISTERED' THEN 'agent.registered'
            WHEN 'AGENT_STATUS_CHANGED' THEN 'agent.status_changed'
            ELSE 'timeline.' || lower(COALESCE(NULLIF(row_data->>'event_type', ''), 'event'))
        END;
        IF jsonb_typeof(row_data->'payload'->'targets') = 'array' THEN
            event_targets := row_data->'payload'->'targets';
        END IF;
        event_refs := jsonb_strip_nulls(jsonb_build_object(
            'alias', COALESCE(row_data->'payload'->>'doc_key', row_data->'payload'->>'task_key'),
            'id', row_data->'payload'->>'id',
            'version', COALESCE(row_data->'payload'->'refs'->'version',
                                row_data->'payload'->'version')));
        event_label := COALESCE(row_data->'payload'->>'doc_key',
                                row_data->'payload'->>'task_key', '');
    ELSE
        event_type := TG_TABLE_NAME || '.' || lower(TG_OP);
    END IF;

    event_payload := jsonb_build_object(
        'table', TG_TABLE_NAME,
        'action', TG_OP,
        'workspace_id', row_data->>'workspace_id',
        'record_id', row_data->>'id',
        'actor', COALESCE(
            row_data->>'updated_by',
            row_data->>'claimed_by',
            row_data->>'created_by',
            row_data->>'agent_signature',
            'system'
        ),
        'actor_stamp', COALESCE(
            NULLIF(current_setting('app.lineage_stamp', true), '')::jsonb,
            '{"kind": "system", "principal_id": "system", "token_id": null, "chain": ["system"]}'::jsonb
        ),
        'version', NULLIF(row_data->>'version', '')::int,
        'status', COALESCE(
            row_data->>'status',
            row_data->'frontmatter'->>'status',
            ''
        ),
        'slot_key', row_data->>'slot_key',
        'task_key', row_data->>'task_key',
        'doc_key', row_data->>'doc_key',
        'namespace', COALESCE(row_data->>'namespace', 'default'),
        'assigned_role', COALESCE(row_data->>'assigned_role', ''),
        'locked_by_human', COALESCE(row_data->>'locked_by_human', 'false')::boolean,
        'event_type', COALESCE(row_data->>'event_type', row_data->'payload'->>'event_type', ''),
        'timestamp', NOW()
    );

    INSERT INTO workspace_events (
        workspace_id, source_table, action, record_id, actor, version, status,
        slot_key, task_key, doc_key, namespace, assigned_role, locked_by_human,
        payload, created_at, type, targets, refs, summary, shard_key
    ) VALUES (
        ws,
        TG_TABLE_NAME,
        TG_OP,
        (row_data->>'id')::uuid,
        event_payload->>'actor',
        NULLIF(event_payload->>'version', '')::int,
        NULLIF(event_payload->>'status', ''),
        event_payload->>'slot_key',
        event_payload->>'task_key',
        event_payload->>'doc_key',
        event_payload->>'namespace',
        NULLIF(event_payload->>'assigned_role', ''),
        COALESCE(event_payload->>'locked_by_human', 'false')::boolean,
        event_payload,
        NOW(),
        event_type,
        event_targets,
        event_refs,
        left(event_type || ' ' || COALESCE(event_label, '') || ' by '
             || COALESCE(event_payload->>'actor', 'system'), 200),
        COALESCE(row_data->>'namespace', 'default')
    ) RETURNING id INTO new_event_id;

    event_payload := event_payload || jsonb_build_object('id', new_event_id, 'type', event_type);
    PERFORM pg_notify('workspace_events', event_payload::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
