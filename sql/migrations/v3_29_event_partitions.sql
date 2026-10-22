-- v3.29 Partitioned event log (PRD §2.3, AG-EV-02 point 6: the shard layer is
-- partitioned by shard_key on one Postgres, so a burst in one shard does not
-- bloat the others' indexes and a later move to several nodes needs no API
-- change).
--
-- workspace_events becomes a HASH(shard_key) partitioned table with 16
-- partitions. Unique keys of a partitioned table must contain the partition
-- key, so the primary key is (id, shard_key) and the unique event_id / seq
-- indexes carry shard_key too; ids still come from one sequence and seq from
-- the per-workspace counter, so both stay unique in practice. shard_key is
-- NOT NULL (the writer always set it; 'default' otherwise).
--
-- The conversion copies the rows before the triggers exist (so seq, tenant
-- and stamps are kept as they were) and is skipped when the table is
-- already partitioned. Documents stay unpartitioned: a dozen tables hold
-- foreign keys to shared_documents(id), which a partitioned table could only
-- accept with shard_key in every one of them.

CREATE OR REPLACE FUNCTION agentgraph_assign_event_seq() RETURNS TRIGGER AS $$
DECLARE
    next_seq BIGINT;
BEGIN
    IF NEW.workspace_id IS NULL THEN
        RETURN NULL;
    END IF;
    INSERT INTO workspace_event_seq (workspace_id, seq) VALUES (NEW.workspace_id, 1)
    ON CONFLICT (workspace_id) DO UPDATE SET seq = workspace_event_seq.seq + 1
    RETURNING seq INTO next_seq;
    -- shard_key prunes the update to the row's own partition.
    UPDATE workspace_events SET seq = next_seq
    WHERE id = NEW.id AND shard_key = NEW.shard_key;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;

DO $$
DECLARE
    part INT;
BEGIN
    IF (SELECT relkind FROM pg_class
        WHERE oid = to_regclass('public.workspace_events')) = 'p' THEN
        RETURN;
    END IF;

    UPDATE workspace_events SET shard_key = COALESCE(namespace, 'default')
    WHERE shard_key IS NULL;
    ALTER TABLE workspace_events RENAME TO workspace_events_unpartitioned;
    CREATE TABLE workspace_events (
        LIKE workspace_events_unpartitioned INCLUDING DEFAULTS
    ) PARTITION BY HASH (shard_key);
    ALTER TABLE workspace_events ALTER COLUMN shard_key SET DEFAULT 'default';
    ALTER TABLE workspace_events ALTER COLUMN shard_key SET NOT NULL;
    FOR part IN 0..15 LOOP
        EXECUTE format(
            'CREATE TABLE workspace_events_p%s PARTITION OF workspace_events '
            'FOR VALUES WITH (MODULUS 16, REMAINDER %s)', lpad(part::text, 2, '0'), part);
    END LOOP;

    INSERT INTO workspace_events SELECT * FROM workspace_events_unpartitioned;
    ALTER SEQUENCE workspace_events_id_seq OWNED BY workspace_events.id;
    DROP TABLE workspace_events_unpartitioned;

    ALTER TABLE workspace_events ADD PRIMARY KEY (id, shard_key);
    ALTER TABLE workspace_events ADD CONSTRAINT workspace_events_workspace_id_fkey
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE;
    CREATE INDEX idx_workspace_events_cursor ON workspace_events (workspace_id, id);
    CREATE INDEX idx_workspace_events_shard ON workspace_events (workspace_id, shard_key, seq);
    CREATE INDEX idx_workspace_events_tenant ON workspace_events (tenant_id, workspace_id);
    CREATE INDEX idx_workspace_events_time ON workspace_events (created_at DESC);
    CREATE INDEX idx_workspace_events_seq ON workspace_events (workspace_id, seq);
    CREATE UNIQUE INDEX uq_workspace_events_event_id ON workspace_events (event_id, shard_key);
    CREATE UNIQUE INDEX uq_workspace_events_seq ON workspace_events (workspace_id, seq, shard_key);

    CREATE CONSTRAINT TRIGGER trg_assign_event_seq AFTER INSERT ON workspace_events
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION agentgraph_assign_event_seq();
    CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
        ON workspace_events FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();
    CREATE TRIGGER trg_lineage_stamp BEFORE INSERT OR UPDATE ON workspace_events
        FOR EACH ROW EXECUTE FUNCTION agentgraph_stamp_entry();

    ALTER TABLE workspace_events ENABLE ROW LEVEL SECURITY;
    ALTER TABLE workspace_events FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON workspace_events
        USING (tenant_id = ANY(agentgraph_tenant_scope())
               AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
        WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope())
               AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
    GRANT SELECT, INSERT, UPDATE, DELETE ON workspace_events TO agentgraph_app;
    -- The application never addresses a partition directly.
    FOR part IN 0..15 LOOP
        EXECUTE format('REVOKE ALL ON workspace_events_p%s FROM agentgraph_app',
                       lpad(part::text, 2, '0'));
    END LOOP;
END
$$;
