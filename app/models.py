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
    FetchedValue,
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

    # Canonical address (PRD AG-ST-17): <prefix>_<ULID>, assigned by the database.
    canonical_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, server_default=FetchedValue()
    )

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
    # Token id of the workspace key: a branch with commander rights (AG-ID-01).
    key_token_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, server_default=FetchedValue()
    )
    # The root token only manages tokens; its secret is held by a human.
    root_token_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, server_default=FetchedValue()
    )
    root_key_hash: Mapped[str | None] = mapped_column(String(256), nullable=True)
    root_key_rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    max_token_depth: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default=text("2")
    )
    branch_token_quota: Mapped[int] = mapped_column(
        Integer, nullable=False, default=20, server_default=text("20")
    )
    workspace_token_quota: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default=text("100")
    )
    # Server-derived: a trigger creates a root tenant when none is supplied.
    tenant_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("tenants.id"), nullable=False,
        server_default=FetchedValue(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    __mapper_args__ = {"eager_defaults": True}


class Tenant(Base):
    """A node of the tenant tree (PRD AG-ID-07); path/root/depth are trigger-maintained."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    alias: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    tenant_type: Mapped[str] = mapped_column(String(16), nullable=False)
    parent_tenant_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("tenants.id"), nullable=True
    )
    root_tenant_id: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default=FetchedValue()
    )
    path: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)), nullable=False, server_default=FetchedValue()
    )
    depth: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=FetchedValue()
    )
    billing_owner_tenant_id: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default=FetchedValue()
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default=text("'active'")
    )
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deletion_admin_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deletion_owner_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deletion_owner_confirmed_by: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    purge_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    final_export_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signing_key_id: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=FetchedValue()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    __mapper_args__ = {"eager_defaults": True}


class Membership(Base):
    """A human's role on a tenant node (PRD AG-ID-10)."""

    __tablename__ = "memberships"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("human_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )


class AgentToken(Base):
    __tablename__ = "agent_tokens"

    # Canonical address (PRD AG-ST-17): <prefix>_<ULID>, assigned by the database.
    canonical_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, server_default=FetchedValue()
    )
    parent_token_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    root_token_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    depth: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    issued_by_agent_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    # AG-ID-02: explicit grant (None = legacy token, limited by its role only).
    scopes: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    read_namespaces: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    write_namespaces: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    can_issue: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("FALSE")
    )
    max_descendants: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    lineage: Mapped[list[str] | None] = mapped_column(ARRAY(String(40)), nullable=True)
    issued_by_token_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    issued_by_human: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    agent_profile: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_ancestor: Mapped[str | None] = mapped_column(String(40), nullable=True)
    expiring_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    __mapper_args__ = {"eager_defaults": True}

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

    # Canonical address (PRD AG-ST-17): <prefix>_<ULID>, assigned by the database.
    canonical_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, server_default=FetchedValue()
    )
    alias_legacy: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("FALSE")
    )
    # Lineage stamps are set by database triggers from app.lineage_stamp.
    created_stamp: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, server_default=FetchedValue(), server_onupdate=FetchedValue()
    )
    updated_stamp: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, server_default=FetchedValue(), server_onupdate=FetchedValue()
    )
    __mapper_args__ = {"eager_defaults": True}

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
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archived_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    archive_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
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

    # Canonical address (PRD AG-ST-17): <prefix>_<ULID>, assigned by the database.
    canonical_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, server_default=FetchedValue()
    )
    # Lineage stamps are set by database triggers from app.lineage_stamp.
    created_stamp: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, server_default=FetchedValue(), server_onupdate=FetchedValue()
    )
    updated_stamp: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, server_default=FetchedValue(), server_onupdate=FetchedValue()
    )
    __mapper_args__ = {"eager_defaults": True}

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

    # Canonical address (PRD AG-ST-17): <prefix>_<ULID>, assigned by the database.
    canonical_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, server_default=FetchedValue()
    )
    # Lineage stamps are set by database triggers from app.lineage_stamp.
    created_stamp: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, server_default=FetchedValue(), server_onupdate=FetchedValue()
    )
    updated_stamp: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, server_default=FetchedValue(), server_onupdate=FetchedValue()
    )
    __mapper_args__ = {"eager_defaults": True}

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
    artifact_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    artifact_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    handed_off_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_round: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    review_history: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    handoff_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
    orphaned_from: Mapped[str | None] = mapped_column(String(128), nullable=True)
    orphan_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    assignee: Mapped[str | None] = mapped_column(String(160), nullable=True)
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
    actor_auth_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_token_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
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

    stamp: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, server_default=FetchedValue()
    )
    __mapper_args__ = {"eager_defaults": True}

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
    # AG-EV-01: per-workspace gap-free seq, assigned at commit (commit order).
    seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    event_id: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default=FetchedValue()
    )
    type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    targets: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    refs: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    summary: Mapped[str | None] = mapped_column(String(200), nullable=True)
    shard_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    stamp: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, server_default=FetchedValue()
    )

    __table_args__ = (
        Index("idx_workspace_events_cursor", "workspace_id", "id"),
        Index("idx_workspace_events_time", "created_at"),
    )


class EventSubscription(Base):
    """Server-side prefix subscription (PRD AG-EV-02)."""

    __tablename__ = "event_subscriptions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, server_default=FetchedValue())
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default=FetchedValue()
    )
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_token_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    prefixes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    types: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("FALSE")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    last_polled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


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
    namespace: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default", server_default=text("'default'")
    )
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

    # Canonical address (PRD AG-ST-17): <prefix>_<ULID>, assigned by the database.
    canonical_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, server_default=FetchedValue()
    )
    __mapper_args__ = {"eager_defaults": True}

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
    google_sub: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("FALSE")
    )
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default="VIEWER",
        server_default=text("'VIEWER'"),
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


class AliasRedirect(Base):
    """A renamed alias that keeps resolving to the same object forever."""

    __tablename__ = "alias_redirects"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    object_type: Mapped[str] = mapped_column(String(16), nullable=False)
    old_alias: Mapped[str] = mapped_column(String(512), nullable=False)
    target_canonical_id: Mapped[str] = mapped_column(String(40), nullable=False)
    renamed_by: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class Tombstone(Base):
    """Content-free record of a purged object; id and alias are never reused."""

    __tablename__ = "tombstones"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    object_type: Mapped[str] = mapped_column(String(16), nullable=False)
    canonical_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    alias: Mapped[str] = mapped_column(String(512), nullable=False)
    namespace: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    purged_by: Mapped[str] = mapped_column(String(256), nullable=False)
    purged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class NamespaceRecord(Base):
    """Register-before-use namespace registry (PRD AG-ST-17 point 7)."""

    __tablename__ = "namespaces"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner: Mapped[str | None] = mapped_column(String(256), nullable=True)
    doc_type_default: Mapped[str] = mapped_column(
        String(16), nullable=False, default="collaborative",
        server_default=text("'collaborative'"),
    )
    write_policy: Mapped[str] = mapped_column(
        String(32), nullable=False, default="open", server_default=text("'open'")
    )
    auto_registered: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("FALSE")
    )
    created_by: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class DocumentRef(Base):
    """A declared {"$ref"} edge from a document version's frontmatter."""

    __tablename__ = "document_refs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    source_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shared_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    field_path: Mapped[str] = mapped_column(String(512), nullable=False)
    target_canonical_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    target_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="declared", server_default=text("'declared'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class GrantSnapshot(Base):
    """Immutable record of what a token was granted when issued (PRD AG-ID-03)."""

    __tablename__ = "grant_snapshots"

    id: Mapped[str] = mapped_column(
        String(40), primary_key=True, server_default=FetchedValue()
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    token_id: Mapped[str] = mapped_column(String(40), nullable=False)
    token_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    parent_token_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    principal_id: Mapped[str] = mapped_column(String(256), nullable=False)
    role_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    namespaces: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False)
    can_issue: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_descendants: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    budget_limits: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    ttl_seconds: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    issuer_stamp: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    parent_effective_grant_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    grant_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    backfilled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __mapper_args__ = {"eager_defaults": True}


class ConstitutionAck(Base):
    """The constitution version a principal has confirmed reading (AG-OB-01)."""

    __tablename__ = "constitution_acks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    principal_key: Mapped[str] = mapped_column(String(256), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    acked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class AccessAudit(Base):
    """Append-only audit of reads such as agent.oriented."""

    __tablename__ = "access_audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class Lease(Base):
    """One grant of exclusive use (PRD AG-CO-03); tasks are the first user."""

    __tablename__ = "leases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    lease_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, server_default=FetchedValue()
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default=FetchedValue()
    )
    resource_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    resource_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False)
    holder: Mapped[str] = mapped_column(String(128), nullable=False)
    holder_token_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    heartbeat_interval_s: Mapped[int] = mapped_column(Integer, nullable=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    heartbeats: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    heartbeat_missed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default=text("'active'")
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DocumentIndex(Base):
    """Index status and embedding job of one document (PRD AG-ST-13)."""

    __tablename__ = "document_index"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shared_documents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    pending_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    indexed_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    superseded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_lag_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

