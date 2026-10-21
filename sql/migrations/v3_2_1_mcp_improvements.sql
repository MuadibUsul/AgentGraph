-- AgentGraph v3.2.1: MCP parity and review semantics.
ALTER TABLE collab_tasks
    ADD COLUMN IF NOT EXISTS review_feedback TEXT,
    ADD COLUMN IF NOT EXISTS reviewed_by VARCHAR(128);
