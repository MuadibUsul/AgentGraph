"""ORM models for the v3.2.1 shared blackboard workspace."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.config import get_settings
from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AgentRole(str, enum.Enum):
    SCOUT = "SCOUT"
    ANALYST = "ANALYST"
    AUDITOR = "AUDITOR"
    EXECUTOR = "EXECUTOR"


class TaskStatus(str, enum.Enum):
    TODO = "TODO"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    REVIEWING = "REVIEWING"
    HANDOFF = "HANDOFF"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    ESCALATED_TO_HUMAN = "ESCALATED_TO_HUMAN"
    DONE = "DONE"
    FAILED = "FAILED"


class LinkType(str, enum.Enum):
    TRIGGERS = "triggers"
    CONTRADICTS = "contradicts"
    SUPPORTS = "supports"
    PRECEDES = "precedes"
    RESOLVES = "resolves"
    DEPENDS_ON = "depends_on"
    REFERENCES = "references"
    BLOCKS = "blocks"
    PRODUCED_BY = "produced_by"


def _enum_values(members: type[enum.Enum]) -> list[str]:
    return [member.value for member in members]


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    api_key_hash: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    tier: Mapped[str] = mapped_column(
        String(32), nullable=False, default="developer_5usd",
        server_default=text("'developer_5usd'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )


class AgentToken(Base):
    __tablename__ = "agent_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_signature: Mapped[str] = mapped_column(String(128), nullable=False)
    api_key_hash: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    role_scope: Mapped[AgentRole] = mapped_column(
        Enum(
            AgentRole,
            name="agent_role",
            native_enum=False,
            values_callable=_enum_values,
        ),
        nullable=False,
    )
    allowed_namespaces: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=lambda: ["*"],
        server_default=text("ARRAY['*']::text[]"),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("TRUE")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    per_minute_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monthly_token_budget: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "agent_signature", name="uq_agent_tokens_ws_signature"
        ),
    )


class SharedDocument(Base):
    __tablename__ = "shared_documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    doc_key: Mapped[str] = mapped_column(String(256), nullable=False)
    title: Mapped[str] = mapped_column(
        String(256), nullable=False, default="Untitled Artifact",
        server_default=text("'Untitled Artifact'"),
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    frontmatter: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict,
        server_default=text("'{}'::jsonb"),
    )
    verification_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="DRAFT",
        server_default=text("'DRAFT'"),
    )
    trust_level: Mapped[str] = mapped_column(
        String(16), nullable=False, default="UNTRUSTED",
        server_default=text("'UNTRUSTED'"),
    )
    verified_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verified_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(get_settings().embedding_dimensions), nullable=True
    )
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    namespace: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default",
        server_default=text("'default'"),
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    locked_by_human: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("FALSE")
    )
    human_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    tainted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("FALSE")
    )
    taint_sources: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    taint_cleared_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    taint_cleared_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    policy_flags: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("workspace_id", "doc_key", name="uq_docs_workspace_key"),
        Index("idx_docs_workspace", "workspace_id"),
        Index("idx_docs_namespace", "workspace_id", "namespace"),
        Index(
            "idx_docs_frontmatter",
            "frontmatter",
            postgresql_using="gin",
            postgresql_ops={"frontmatter": "jsonb_path_ops"},
        ),
        Index(
            "idx_docs_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class ContextSlot(Base):
    __tablename__ = "context_slots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    slot_key: Mapped[str] = mapped_column(String(256), nullable=False)
    goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    schema_hint: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    blockers: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    namespace: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default",
        server_default=text("'default'"),
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    lease_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    locked_by_human: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("FALSE")
    )
    human_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    tainted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("FALSE")
    )
    taint_sources: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    updated_by: Mapped[str] = mapped_column(String(128), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("workspace_id", "slot_key", name="uq_slots_workspace_key"),
        Index("idx_slots_lookup", "workspace_id", "slot_key"),
        Index("idx_slots_namespace", "workspace_id", "namespace"),
    )


class CollabTask(Base):
    __tablename__ = "collab_tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_key: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(
            TaskStatus,
            name="task_status",
            create_type=False,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=TaskStatus.READY,
        server_default=text("'READY'"),
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    assigned_role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    namespace: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default",
        server_default=text("'default'"),
    )
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    input_context: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    output_artifact: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    handoff_brief: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    artifact_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    human_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reject_history: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    ping_pong_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    max_retries: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default=text("3")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    lease_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    tainted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("FALSE")
    )
    taint_sources: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("workspace_id", "task_key", name="uq_tasks_workspace_key"),
        Index("idx_tasks_board", "workspace_id", "status", "priority"),
        Index("idx_tasks_lease", "workspace_id", "lease_expires_at"),
        Index("idx_tasks_role", "workspace_id", "assigned_role", "status"),
    )


class WorkspaceLink(Base):
    __tablename__ = "workspace_links"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    target_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("collab_tasks.id", ondelete="CASCADE"),
        nullable=True,
    )
    source_slot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("context_slots.id", ondelete="CASCADE"),
        nullable=True,
    )
    source_doc_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("shared_documents.id", ondelete="CASCADE"),
        nullable=True,
    )
    target_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("collab_tasks.id", ondelete="CASCADE"),
        nullable=True,
    )
    target_slot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("context_slots.id", ondelete="CASCADE"),
        nullable=True,
    )
    target_doc_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("shared_documents.id", ondelete="CASCADE"),
        nullable=True,
    )
    relation_type: Mapped[LinkType] = mapped_column(
        Enum(
            LinkType,
            name="causal_relation",
            create_type=False,
            values_callable=_enum_values,
        ),
        nullable=False,
    )
    confidence: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0, server_default=text("1.0")
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "source_id", "target_id", "relation_type",
            name="uq_workspace_link",
        ),
        Index("idx_links_source", "workspace_id", "source_id"),
        Index("idx_links_target", "workspace_id", "target_id"),
    )


class TimelineStream(Base):
    __tablename__ = "timeline_streams"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_signature: Mapped[str] = mapped_column(String(128), nullable=False)
    platform: Mapped[str] = mapped_column(
        String(64), nullable=False, default="unknown", server_default=text("'unknown'")
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    namespace: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default",
        server_default=text("'default'"),
    )
    token_cost: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    latency_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("idx_timeline_workspace_time", "workspace_id", "created_at"),
        Index("idx_timeline_namespace", "workspace_id", "namespace"),
    )


class StateAuditDiff(Base):
    __tablename__ = "state_audit_diffs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    agent_signature: Mapped[str] = mapped_column(String(128), nullable=False)
    target_table: Mapped[str] = mapped_column(String(64), nullable=False)
    target_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    before_diff: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    after_diff: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("idx_audit_batch", "session_batch_id"),
        Index("idx_audit_workspace_time", "workspace_id", "created_at"),
    )


class DocumentVersion(Base):
    __tablename__ = "document_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("shared_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    frontmatter: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    verification_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="DRAFT", server_default=text("'DRAFT'")
    )
    trust_level: Mapped[str] = mapped_column(
        String(16), nullable=False, default="UNTRUSTED", server_default=text("'UNTRUSTED'")
    )
    tainted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("FALSE")
    )
    taint_sources: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    author: Mapped[str] = mapped_column(String(128), nullable=False)
    session_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "document_id", "version", name="uq_document_versions_doc_version"
        ),
        Index("idx_doc_versions_doc", "document_id", "version"),
        Index("idx_doc_versions_workspace", "workspace_id", "created_at"),
    )


class WorkspaceEvent(Base):
    __tablename__ = "workspace_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_table: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    actor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    slot_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    task_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    doc_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    namespace: Mapped[str | None] = mapped_column(String(64), nullable=True)
    assigned_role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_by_human: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("idx_workspace_events_cursor", "workspace_id", "id"),
        Index("idx_workspace_events_time", "created_at"),
    )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("shared_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    doc_key: Mapped[str] = mapped_column(String(256), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    heading: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(get_settings().embedding_dimensions), nullable=True
    )
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    search_vector: Mapped[Any | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', coalesce(heading, '') || ' ' || content)",
            persisted=True,
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "document_id", "chunk_index", name="uq_document_chunks_doc_index"
        ),
        Index("idx_document_chunks_workspace", "workspace_id"),
        Index("idx_document_chunks_doc", "document_id", "chunk_index"),
        Index(
            "idx_document_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index(
            "idx_document_chunks_fts",
            "search_vector",
            postgresql_using="gin",
        ),
    )


class DocumentProposal(Base):
    __tablename__ = "document_proposals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    doc_key: Mapped[str] = mapped_column(String(256), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    frontmatter: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    base_version: Mapped[int] = mapped_column(Integer, nullable=False)
    base_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="single", server_default=text("'single'")
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="PENDING",
        server_default=text("'PENDING'"),
    )
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    applied_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    session_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    tainted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("FALSE")
    )
    taint_sources: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    conflict_details: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("idx_document_proposals_doc", "workspace_id", "doc_key", "status"),
        Index("idx_document_proposals_status", "workspace_id", "status"),
    )


class ProposalVote(Base):
    __tablename__ = "proposal_votes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("document_proposals.id", ondelete="CASCADE"),
        nullable=False,
    )
    voter: Mapped[str] = mapped_column(String(128), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("proposal_id", "voter", name="uq_proposal_votes_voter"),
        Index("idx_proposal_votes_proposal", "proposal_id"),
    )


class PathPolicy(Base):
    __tablename__ = "path_policies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(16), nullable=False)
    pattern: Mapped[str] = mapped_column(String(256), nullable=False)
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    require_proposal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("FALSE")
    )
    allowed_roles: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list,
        server_default=text("ARRAY[]::text[]"),
    )
    json_schema: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    max_content_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    require_verified_sources: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("FALSE")
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("TRUE")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "resource_type", "pattern",
            name="uq_path_policies_pattern",
        ),
        Index("idx_path_policies_lookup", "workspace_id", "resource_type", "active"),
    )


class HumanUser(Base):
    __tablename__ = "human_users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default="REVIEWER",
        server_default=text("'REVIEWER'"),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("TRUE")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("email", name="uq_human_users_email"),
        Index("idx_human_users_workspace", "workspace_id"),
    )


class HumanSession(Base):
    __tablename__ = "human_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("human_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("idx_human_sessions_user", "user_id"),
        Index("idx_human_sessions_expiry", "expires_at"),
    )


class TaintEdge(Base):
    __tablename__ = "taint_edges"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("shared_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    reason: Mapped[str] = mapped_column(
        String(64), nullable=False, default="source",
        server_default=text("'source'"),
    )
    depth: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "source_doc_id", "target_type", "target_id",
            name="uq_taint_edges_source_target",
        ),
        Index("idx_taint_edges_target", "workspace_id", "target_type", "target_id"),
    )


class ExternalAction(Base):
    __tablename__ = "external_actions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(
        String(8), nullable=False, default="POST", server_default=text("'POST'")
    )
    headers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="PENDING_APPROVAL",
        server_default=text("'PENDING_APPROVAL'"),
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default=text("5")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    compensates_action_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("external_actions.id", ondelete="SET NULL"),
        nullable=True,
    )
    compensation_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    namespace: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default", server_default=text("'default'")
    )
    session_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "idempotency_key",
            name="uq_external_actions_idempotency",
        ),
        Index("idx_external_actions_status", "workspace_id", "status"),
        Index("idx_external_actions_batch", "workspace_id", "session_batch_id"),
    )


class ExternalActionAttempt(Base):
    __tablename__ = "external_action_attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    action_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("external_actions.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("idx_external_action_attempts_action", "action_id", "created_at"),
    )


class WorkspaceBudget(Base):
    __tablename__ = "workspace_budgets"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    monthly_token_budget: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=10_000_000,
        server_default=text("10000000"),
    )
    monthly_cost_budget_usd: Mapped[float] = mapped_column(
        Numeric(12, 4), nullable=False, default=1000,
        server_default=text("1000"),
    )
    max_requests_per_minute: Mapped[int] = mapped_column(
        Integer, nullable=False, default=600, server_default=text("600")
    )
    max_concurrent_leases: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default=text("100")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )


class UsageLedger(Base):
    __tablename__ = "usage_ledger"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_signature: Mapped[str | None] = mapped_column(String(128), nullable=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    cost_usd: Mapped[float] = mapped_column(
        Numeric(12, 6), nullable=False, default=0, server_default=text("0")
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("idx_usage_ledger_workspace_time", "workspace_id", "created_at"),
        Index("idx_usage_ledger_category", "workspace_id", "category"),
    )


class RateLimitWindow(Base):
    __tablename__ = "rate_limit_windows"

    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    bucket: Mapped[str] = mapped_column(String(160), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class WebhookRule(Base):
    __tablename__ = "webhook_rules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    secret: Mapped[str] = mapped_column(String(256), nullable=False)
    event_types: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=lambda: ["*"],
        server_default=text("ARRAY['*']::text[]"),
    )
    tables: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list,
        server_default=text("ARRAY[]::text[]"),
    )
    namespaces: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list,
        server_default=text("ARRAY[]::text[]"),
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("TRUE")
    )
    last_event_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("idx_webhook_rules_workspace", "workspace_id", "active"),
    )


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("webhook_rules.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PENDING",
        server_default=text("'PENDING'"),
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("rule_id", "event_id", name="uq_webhook_delivery_event"),
        Index("idx_webhook_deliveries_pending", "status", "next_attempt_at"),
    )
