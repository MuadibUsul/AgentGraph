"""Pydantic contracts for the v3.2.1 shared blackboard API."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import get_settings
from app.models import LinkType, TaskStatus

settings = get_settings()

Namespace = str
RoleName = Literal["SCOUT", "ANALYST", "AUDITOR", "EXECUTOR"]
_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$"
_NAMESPACE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=False,
    )


def _clean_agent(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > 128:
        raise ValueError("agent signature must be at most 128 characters")
    return value


def _serialized_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be JSON serializable") from exc


def _clean_namespaces(values: list[str] | None) -> list[str]:
    if not values:
        raise ValueError("at least one namespace is required; use '*' for all")
    cleaned: list[str] = []
    for raw in values:
        value = raw.strip()
        if not value:
            continue
        if value != "*" and len(value) > 64:
            raise ValueError("namespace must be at most 64 characters")
        if value not in cleaned:
            cleaned.append(value)
    if not cleaned:
        raise ValueError("at least one namespace is required")
    return cleaned


# --------------------------------------------------------------------- tasks


class HandoffBrief(StrictModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    summary: str = Field(min_length=1, max_length=4_000)
    key_metrics: dict[str, Any]
    unverified_hypotheses: list[str]
    risk_flag: bool

    @model_validator(mode="after")
    def _size(self) -> HandoffBrief:
        if _serialized_size(self.model_dump()) > settings.max_brief_bytes:
            raise ValueError("handoff brief is too large")
        return self


class NextTaskSpec(StrictModel):
    task_key: str = Field(pattern=_KEY_PATTERN + "$", min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    assigned_role: RoleName | None = None
    priority: int = Field(default=0, ge=-100, le=100)
    namespace: Namespace = Field(default="default", pattern=_NAMESPACE_PATTERN + "$")
    input_context: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _claimable_role(self) -> NextTaskSpec:
        if self.assigned_role == "SCOUT":
            raise ValueError(
                "SCOUT tokens cannot claim tasks; assign ANALYST, AUDITOR or EXECUTOR"
            )
        return self


class TaskCreateRequest(StrictModel):
    task_key: str | None = Field(
        default=None, pattern=_KEY_PATTERN + "$", max_length=128
    )
    title: str = Field(min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=20_000)
    status: Literal["TODO", "READY"] = "READY"
    priority: int = Field(default=0, ge=-100, le=100)
    assigned_role: RoleName | None = None
    namespace: Namespace = Field(default="default", pattern=_NAMESPACE_PATTERN + "$")
    input_context: dict[str, Any] = Field(default_factory=dict)
    max_retries: int = Field(default=settings.default_max_retries, ge=1, le=20)
    depends_on: list[str] = Field(default_factory=list, max_length=50)
    agent: str | None = None
    session_batch_id: uuid.UUID | None = None

    @field_validator("agent")
    @classmethod
    def _agent(cls, value: str | None) -> str | None:
        return _clean_agent(value)

    @field_validator("depends_on")
    @classmethod
    def _dep_keys(cls, value: list[str]) -> list[str]:
        for item in value:
            if not item or len(item) > 128:
                raise ValueError("invalid dependency task key")
        return list(dict.fromkeys(value))

    @field_validator("input_context")
    @classmethod
    def _context_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        if _serialized_size(value) > settings.max_state_bytes:
            raise ValueError("input_context is too large")
        return value

    @model_validator(mode="after")
    def _claimable_role(self) -> TaskCreateRequest:
        if self.assigned_role == "SCOUT":
            raise ValueError(
                "SCOUT tokens cannot claim tasks; assign ANALYST, AUDITOR or EXECUTOR"
            )
        return self


class TaskClaimRequest(StrictModel):
    lease_seconds: int | None = Field(
        default=None, ge=5, le=settings.max_task_lease_seconds
    )
    agent: str | None = None
    session_batch_id: uuid.UUID | None = None

    @field_validator("agent")
    @classmethod
    def _agent(cls, value: str | None) -> str | None:
        return _clean_agent(value)


class TaskRenewRequest(StrictModel):
    lease_seconds: int | None = Field(
        default=None, ge=5, le=settings.max_task_lease_seconds
    )
    lease_epoch: int | None = Field(default=None, ge=0)
    agent: str | None = None

    @field_validator("agent")
    @classmethod
    def _agent(cls, value: str | None) -> str | None:
        return _clean_agent(value)


class TaskReleaseRequest(StrictModel):
    lease_epoch: int | None = Field(default=None, ge=0)
    agent: str | None = None
    reason: str | None = Field(default=None, max_length=2_000)
    session_batch_id: uuid.UUID | None = None
    force: bool = False

    @field_validator("agent")
    @classmethod
    def _agent(cls, value: str | None) -> str | None:
        return _clean_agent(value)


class TaskHandoffRequest(StrictModel):
    next_status: Literal["REVIEWING", "HANDOFF", "DONE", "AWAITING_HUMAN", "FAILED"]
    brief: HandoffBrief | None = None
    artifact_text: str | None = Field(
        default=None, max_length=settings.max_artifact_chars
    )
    artifact_uri: str | None = Field(default=None, max_length=2_048)
    output_artifact: dict[str, Any] = Field(default_factory=dict)
    human_feedback: str | None = Field(default=None, max_length=8_000)
    expected_version: int | None = Field(default=None, ge=1)
    lease_epoch: int | None = Field(default=None, ge=0)
    create_next: NextTaskSpec | None = None
    agent: str | None = None
    session_batch_id: uuid.UUID | None = None

    @field_validator("agent")
    @classmethod
    def _agent(cls, value: str | None) -> str | None:
        return _clean_agent(value)

    @model_validator(mode="after")
    def _handoff_contract(self) -> TaskHandoffRequest:
        if self.next_status in ("REVIEWING", "HANDOFF", "DONE") and self.brief is None:
            raise ValueError(
                "a structured L1 handoff brief is required for REVIEWING/HANDOFF/DONE"
            )
        if self.artifact_uri and not self.artifact_uri.startswith("doc://"):
            raise ValueError("artifact_uri must use doc://<doc_key>")
        if _serialized_size(self.output_artifact) > settings.max_state_bytes:
            raise ValueError("output_artifact is too large")
        return self


class TaskReviewRequest(StrictModel):
    decision: Literal["approve", "reject"]
    feedback: str | None = Field(default=None, max_length=8_000)
    expected_version: int | None = Field(default=None, ge=1)
    next_status: Literal["DONE", "HANDOFF", "READY"] | None = None
    agent: str | None = None
    session_batch_id: uuid.UUID | None = None

    @field_validator("agent")
    @classmethod
    def _agent(cls, value: str | None) -> str | None:
        return _clean_agent(value)

    @model_validator(mode="after")
    def _decision_status(self) -> TaskReviewRequest:
        if self.next_status is not None:
            if self.decision == "approve" and self.next_status not in (
                "DONE",
                "HANDOFF",
                "READY",
            ):
                raise ValueError("approve next_status must be DONE, HANDOFF or READY")
            if self.decision == "reject" and self.next_status not in ("READY",):
                raise ValueError("reject next_status must be READY")
        return self


class TaskEscalateRequest(StrictModel):
    reason: str = Field(min_length=1, max_length=8_000)
    expected_version: int | None = Field(default=None, ge=1)
    agent: str | None = None
    session_batch_id: uuid.UUID | None = None

    @field_validator("agent")
    @classmethod
    def _agent(cls, value: str | None) -> str | None:
        return _clean_agent(value)


class AdminTaskReassignRequest(StrictModel):
    assigned_role: Literal["ANALYST", "AUDITOR", "EXECUTOR"]
    reason: str | None = Field(default=None, max_length=4_000)
    expected_version: int | None = Field(default=None, ge=1)
    reset_to_ready: bool = True


class AdminTaskCancelRequest(StrictModel):
    reason: str = Field(min_length=1, max_length=4_000)
    expected_version: int | None = Field(default=None, ge=1)


# --------------------------------------------------------------------- slots


class SlotPatchRequest(StrictModel):
    ops: list[dict[str, Any]] = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=1)
    lease_epoch: int | None = Field(default=None, ge=0)
    lease_seconds: int | None = Field(
        default=None, ge=5, le=settings.max_task_lease_seconds
    )
    agent: str | None = None
    session_batch_id: uuid.UUID | None = None
    rebase: bool = False

    @field_validator("agent")
    @classmethod
    def _agent(cls, value: str | None) -> str | None:
        return _clean_agent(value)


class SlotReplaceRequest(StrictModel):
    state: dict[str, Any]
    expected_version: int = Field(ge=0)
    goal: str | None = Field(default=None, max_length=8_000)
    blockers: list[Any] | None = None
    schema_hint: dict[str, Any] | None = None
    namespace: Namespace | None = Field(
        default=None, pattern=_NAMESPACE_PATTERN + "$"
    )
    lease_seconds: int | None = Field(
        default=None, ge=5, le=settings.max_task_lease_seconds
    )
    lease_epoch: int | None = Field(default=None, ge=0)
    agent: str | None = None
    session_batch_id: uuid.UUID | None = None

    @field_validator("agent")
    @classmethod
    def _agent(cls, value: str | None) -> str | None:
        return _clean_agent(value)

    @model_validator(mode="after")
    def _size(self) -> SlotReplaceRequest:
        if (
            _serialized_size(self.state) + _serialized_size(self.blockers or [])
            > settings.max_state_bytes
        ):
            raise ValueError("state is too large")
        return self


class HumanSlotUpdateRequest(StrictModel):
    state: dict[str, Any] | None = None
    expected_version: int = Field(ge=1)
    goal: str | None = Field(default=None, max_length=8_000)
    blockers: list[Any] | None = None
    schema_hint: dict[str, Any] | None = None
    human_note: str | None = Field(default=None, max_length=4_000)
    lock: bool | None = None
    unlock: bool = False
    session_batch_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _human_contract(self) -> HumanSlotUpdateRequest:
        if self.lock is True and self.unlock:
            raise ValueError("lock and unlock cannot both be true")
        if (
            self.state is None
            and not self.unlock
            and self.goal is None
            and self.blockers is None
        ):
            raise ValueError("provide state/goal/blockers or unlock=true")
        if (
            self.state is not None
            and _serialized_size(self.state) > settings.max_state_bytes
        ):
            raise ValueError("state is too large")
        return self


# ----------------------------------------------------------------- documents


class DocumentUpsertRequest(StrictModel):
    title: str | None = Field(default=None, max_length=256)
    content: str = Field(min_length=1, max_length=settings.max_document_content_chars)
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    expected_version: int = Field(ge=0)
    namespace: Namespace | None = Field(
        default=None, pattern=_NAMESPACE_PATTERN + "$"
    )
    agent: str | None = None
    session_batch_id: uuid.UUID | None = None
    rebase: bool = False

    @field_validator("agent")
    @classmethod
    def _agent(cls, value: str | None) -> str | None:
        return _clean_agent(value)

    @model_validator(mode="after")
    def _size(self) -> DocumentUpsertRequest:
        if _serialized_size(self.frontmatter) > settings.max_state_bytes:
            raise ValueError("frontmatter is too large")
        return self


class AdminDocumentCreateRequest(DocumentUpsertRequest):
    doc_key: str = Field(
        pattern=_KEY_PATTERN + "$", min_length=1, max_length=256
    )


class DocumentSectionPatchRequest(StrictModel):
    section_heading: str = Field(min_length=1, max_length=256)
    new_markdown: str = Field(
        max_length=settings.max_document_content_chars,
        description=(
            "Replacement section body only. Do not include the heading; a "
            "leading matching heading is stripped for compatibility."
        ),
    )
    expected_version: int = Field(ge=1)
    agent: str | None = None
    session_batch_id: uuid.UUID | None = None
    rebase: bool = False

    @field_validator("agent")
    @classmethod
    def _agent(cls, value: str | None) -> str | None:
        return _clean_agent(value)


class DocumentQueryRequest(StrictModel):
    query: str | None = Field(default=None, max_length=10_000)
    filters: dict[str, Any] = Field(default_factory=dict)
    namespace: Namespace | None = Field(default=None, pattern=_NAMESPACE_PATTERN + "$")
    limit: int = Field(default=10, ge=1, le=50)
    include_unverified: bool = False
    include_tainted: bool = False
    mode: Literal["auto", "semantic", "keyword", "hybrid"] = "hybrid"

    @model_validator(mode="after")
    def _size(self) -> DocumentQueryRequest:
        if _serialized_size(self.filters) > 16_384:
            raise ValueError("filters are too large")
        return self


class DocumentHumanUpdateRequest(StrictModel):
    title: str | None = Field(default=None, max_length=256)
    content: str | None = Field(
        default=None, max_length=settings.max_document_content_chars
    )
    frontmatter: dict[str, Any] | None = None
    expected_version: int = Field(ge=1)
    human_note: str | None = Field(default=None, max_length=4_000)
    lock: bool | None = None
    unlock: bool = False
    session_batch_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _human_contract(self) -> DocumentHumanUpdateRequest:
        if self.lock is True and self.unlock:
            raise ValueError("lock and unlock cannot both be true")
        if (
            self.content is None
            and self.title is None
            and self.frontmatter is None
            and not self.unlock
        ):
            raise ValueError("provide content/title/frontmatter or unlock=true")
        if (
            self.frontmatter is not None
            and _serialized_size(self.frontmatter) > settings.max_state_bytes
        ):
            raise ValueError("frontmatter is too large")
        return self


class DereferenceRequest(StrictModel):
    uri: str = Field(min_length=1, max_length=2_048)


# --------------------------------------------------------------------- links


class LinkCreateRequest(StrictModel):
    source_id: uuid.UUID
    target_id: uuid.UUID
    relation_type: LinkType = LinkType.DEPENDS_ON
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _no_loop(self) -> LinkCreateRequest:
        if self.source_id == self.target_id:
            raise ValueError("workspace links cannot be self-loops")
        if _serialized_size(self.metadata) > 16_384:
            raise ValueError("metadata is too large")
        return self


# ------------------------------------------------------------------- timeline


class TimelineEventRequest(StrictModel):
    event_type: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    payload: dict[str, Any] = Field(default_factory=dict)
    namespace: Namespace = Field(default="default", pattern=_NAMESPACE_PATTERN + "$")
    agent: str | None = None
    platform: str = Field(default="api", max_length=64)
    token_cost: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)

    @field_validator("agent")
    @classmethod
    def _agent(cls, value: str | None) -> str | None:
        return _clean_agent(value)

    @field_validator("payload")
    @classmethod
    def _payload_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        if _serialized_size(value) > 65_536:
            raise ValueError("payload exceeds 64 KiB")
        return value


# ------------------------------------------------------------- agent tokens


class AgentTokenCreateRequest(StrictModel):
    agent_signature: str = Field(min_length=1, max_length=128)
    role_scope: RoleName
    allowed_namespaces: list[str] = Field(default_factory=lambda: ["*"])
    is_active: bool = True
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
    per_minute_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    monthly_token_budget: int | None = Field(default=None, ge=0)

    @field_validator("allowed_namespaces")
    @classmethod
    def _namespaces(cls, value: list[str]) -> list[str]:
        return _clean_namespaces(value)


class AgentTokenUpdateRequest(StrictModel):
    role_scope: RoleName | None = None
    allowed_namespaces: list[str] | None = None
    is_active: bool | None = None
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
    per_minute_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    monthly_token_budget: int | None = Field(default=None, ge=0)

    @field_validator("allowed_namespaces")
    @classmethod
    def _namespaces(cls, value: list[str] | None) -> list[str] | None:
        return _clean_namespaces(value) if value is not None else None

    @model_validator(mode="after")
    def _at_least_one(self) -> AgentTokenUpdateRequest:
        if not any(
            value is not None
            for value in (
                self.role_scope,
                self.allowed_namespaces,
                self.is_active,
                self.expires_in_days,
                self.per_minute_limit,
                self.monthly_token_budget,
            )
        ):
            raise ValueError("at least one token field is required")
        return self


# --------------------------------------------------------------- workspaces


class CreateWorkspaceRequest(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    tier: Literal["free", "developer_5usd", "pro"] = "developer_5usd"


class UpdateWorkspaceRequest(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    tier: Literal["free", "developer_5usd", "pro"] | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> UpdateWorkspaceRequest:
        if self.name is None and self.tier is None:
            raise ValueError("name or tier is required")
        return self


# -------------------------------------------------------------------- output


class WorkspaceInfo(BaseModel):
    id: uuid.UUID
    name: str
    tier: str
    created_at: datetime


class WhoAmIResponse(BaseModel):
    workspace: WorkspaceInfo
    role: str
    agent_signature: str
    auth_kind: str
    allowed_namespaces: list[str]
    permissions: list[str] = Field(default_factory=list)
    default_lease_seconds: int
    embedding_model: str
    embedding_dimensions: int
    version: str
    human_role: str | None = None


class TaskSummary(BaseModel):
    id: uuid.UUID
    task_key: str
    title: str
    status: TaskStatus
    priority: int
    assigned_role: str | None
    namespace: str
    claimed_by: str | None
    lease_expires_at: datetime | None
    lease_active: bool
    handoff_summary: str | None = None
    artifact_uri: str | None = None
    human_feedback: str | None = None
    review_feedback: str | None = None
    reviewed_by: str | None = None
    ping_pong_count: int
    max_retries: int
    version: int
    lease_epoch: int
    tainted: bool = False
    taint_sources: list[Any] = Field(default_factory=list)
    updated_at: datetime


class TaskOut(TaskSummary):
    description: str | None = None
    input_context: dict[str, Any] = Field(default_factory=dict)
    output_artifact: dict[str, Any] = Field(default_factory=dict)
    handoff_brief: dict[str, Any] | None = None
    human_feedback: str | None = None
    reject_history: list[Any] = Field(default_factory=list)
    created_by: str
    created_at: datetime


class TaskListResponse(BaseModel):
    items: list[TaskSummary]


class TaskActionResponse(BaseModel):
    task: TaskOut
    escalated: bool = False
    artifact_uri: str | None = None
    session_batch_id: uuid.UUID
    status: TaskStatus
    version: int
    task_key: str


class TaskContextResponse(BaseModel):
    task: TaskOut
    upstream_briefs: list[dict[str, Any]] = Field(default_factory=list)
    slots: list[SlotOut] = Field(default_factory=list)
    documents: list[DocumentSummary] = Field(default_factory=list)
    session_batch_id: uuid.UUID | None = None


class SlotOut(BaseModel):
    id: uuid.UUID
    slot_key: str
    goal: str | None
    current_state: dict[str, Any]
    schema_hint: dict[str, Any]
    blockers: list[Any]
    namespace: str
    version: int
    lease_epoch: int
    locked_by: str | None
    lock_expires_at: datetime | None
    lease_active: bool
    locked_by_human: bool
    human_note: str | None
    tainted: bool = False
    taint_sources: list[Any] = Field(default_factory=list)
    updated_by: str
    updated_at: datetime


class SlotSummary(BaseModel):
    id: uuid.UUID
    slot_key: str
    namespace: str
    version: int
    lease_epoch: int
    locked_by: str | None
    lock_expires_at: datetime | None
    locked_by_human: bool
    tainted: bool = False
    taint_sources: list[Any] = Field(default_factory=list)
    updated_by: str
    updated_at: datetime


class SlotListResponse(BaseModel):
    items: list[SlotOut]


class SlotMutationResponse(BaseModel):
    slot: SlotOut
    applied_ops: list[dict[str, Any]] = Field(default_factory=list)
    session_batch_id: uuid.UUID


class DocumentSummary(BaseModel):
    id: uuid.UUID
    doc_key: str
    title: str
    namespace: str
    version: int
    frontmatter: dict[str, Any]
    verification_status: str
    trust_level: str
    verified_version: int | None
    verified_hash: str | None
    content_hash: str | None
    embedding_model: str | None
    locked_by_human: bool
    human_note: str | None
    tainted: bool = False
    taint_sources: list[Any] = Field(default_factory=list)
    score: float | None = None
    updated_by: str
    updated_at: datetime


class DocumentOut(DocumentSummary):
    content: str
    created_by: str
    created_at: datetime
    agent_view: str


class DocumentListResponse(BaseModel):
    items: list[DocumentSummary]


class DocumentMutationResponse(BaseModel):
    document: DocumentOut
    session_batch_id: uuid.UUID


class DocumentNormalizeResponse(BaseModel):
    converted: bool
    document: DocumentOut


class DocumentQueryResponse(BaseModel):
    items: list[DocumentSummary]
    total: int


class DereferenceResponse(BaseModel):
    uri: str
    document: DocumentOut


class LinkOut(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID
    target_id: uuid.UUID
    source_type: str | None = None
    target_type: str | None = None
    relation_type: LinkType
    confidence: float
    metadata: dict[str, Any]
    created_at: datetime


class LinkListResponse(BaseModel):
    items: list[LinkOut]


class TimelineItemOut(BaseModel):
    id: uuid.UUID
    agent_signature: str
    platform: str
    event_type: str
    payload: dict[str, Any]
    namespace: str
    token_cost: int
    latency_ms: int
    created_at: datetime


class TimelineListResponse(BaseModel):
    items: list[TimelineItemOut]


class WorkspaceOutput(BaseModel):
    task_key: str
    title: str
    status: str
    assigned_role: str | None = None
    artifact_uri: str | None = None
    handoff_summary: str | None = None
    reviewed_by: str | None = None


class WorkspaceMapResponse(BaseModel):
    workspace: WorkspaceInfo
    role: str
    counts: dict[str, int]
    task_statuses: list[str]
    roles: list[str]
    tasks: list[TaskSummary]
    slots: list[SlotSummary]
    documents: list[DocumentSummary]
    outputs: list[WorkspaceOutput] = Field(default_factory=list)
    artifact_uri_scheme: str = "doc://<doc_key>"


class QuotaUsage(BaseModel):
    used: int
    limit: int
    percentage: float


class WorkspaceStatsResponse(BaseModel):
    workspace: WorkspaceInfo
    documents: int
    slots: int
    tasks: int
    agent_tokens: int
    timeline_events: int
    audit_entries: int
    escalated_tasks: int
    tokens_logged: int
    avg_latency_ms: float
    last_activity_at: datetime | None
    document_quota: QuotaUsage
    slot_quota: QuotaUsage
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int
    pending_proposals: int = 0
    pending_external_actions: int = 0
    webhook_rules: int = 0
    tainted_documents: int = 0
    tokens_used_30d: int = 0


class AdminWorkspaceOut(WorkspaceInfo):
    documents: int
    slots: int
    tasks: int
    timeline_events: int
    last_activity_at: datetime | None


class WorkspaceCreatedResponse(BaseModel):
    workspace: WorkspaceInfo
    api_key: str = Field(description="Shown exactly once; store it securely.")


class ApiKeyRotatedResponse(BaseModel):
    workspace: WorkspaceInfo
    api_key: str = Field(description="Shown exactly once; store it securely.")


class AgentTokenOut(BaseModel):
    id: uuid.UUID
    agent_signature: str
    role_scope: str
    allowed_namespaces: list[str]
    is_active: bool
    created_at: datetime
    last_seen_at: datetime | None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    per_minute_limit: int | None = None
    monthly_token_budget: int | None = None


class AgentTokenCreatedResponse(BaseModel):
    token: AgentTokenOut
    api_key: str = Field(description="Shown exactly once; store it securely.")


class AgentTokenListResponse(BaseModel):
    items: list[AgentTokenOut]


class AuditDiffOut(BaseModel):
    id: uuid.UUID
    session_batch_id: uuid.UUID
    agent_signature: str
    target_table: str
    target_record_id: uuid.UUID
    operation: str
    before_diff: dict[str, Any]
    after_diff: dict[str, Any]
    created_at: datetime


class AuditBatchOut(BaseModel):
    session_batch_id: uuid.UUID
    agent_signature: str
    entry_count: int
    targets: list[str]
    first_at: datetime
    last_at: datetime


class AuditListResponse(BaseModel):
    batches: list[AuditBatchOut]
    diffs: list[AuditDiffOut]


class BatchUndoResponse(BaseModel):
    batch_id: uuid.UUID
    undo_batch_id: uuid.UUID
    restored: int
    warnings: list[str] = Field(default_factory=list)


class DocumentVerifyRequest(StrictModel):
    expected_version: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=4_000)
    session_batch_id: uuid.UUID | None = None
    force: bool = False


class DocumentStatusRequest(StrictModel):
    expected_version: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=4_000)
    session_batch_id: uuid.UUID | None = None
    force: bool = False


class DocumentVersionSummary(BaseModel):
    version: int
    parent_version: int | None
    content_hash: str
    author: str
    session_batch_id: uuid.UUID | None
    change_summary: str | None
    verification_status: str
    trust_level: str
    tainted: bool = False
    taint_sources: list[Any] = Field(default_factory=list)
    created_at: datetime


class DocumentVersionOut(DocumentVersionSummary):
    id: uuid.UUID
    document_id: uuid.UUID
    title: str
    content: str
    frontmatter: dict[str, Any]
    namespace: str


class DocumentVersionListResponse(BaseModel):
    versions: list[DocumentVersionSummary]


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


TaskContextResponse.model_rebuild()


# ------------------------------------------------------- v3.2 proposals / arena


class ProposalCreateRequest(StrictModel):
    content: str = Field(min_length=1, max_length=settings.max_proposal_content_chars)
    title: str | None = Field(default=None, max_length=256)
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    base_version: int = Field(ge=1)
    mode: Literal["single", "arena"] = "single"
    note: str | None = Field(default=None, max_length=4_000)
    agent: str | None = None
    session_batch_id: uuid.UUID | None = None

    @field_validator("agent")
    @classmethod
    def _agent(cls, value: str | None) -> str | None:
        return _clean_agent(value)

    @model_validator(mode="after")
    def _size(self) -> ProposalCreateRequest:
        if _serialized_size(self.frontmatter) > settings.max_state_bytes:
            raise ValueError("frontmatter is too large")
        return self


class ProposalVoteRequest(StrictModel):
    score: int = Field(ge=0, le=5)
    comment: str | None = Field(default=None, max_length=4_000)


class ProposalApplyRequest(StrictModel):
    expected_document_version: int | None = Field(default=None, ge=1)
    note: str | None = Field(default=None, max_length=4_000)
    session_batch_id: uuid.UUID | None = None
    rebase: bool = True


class ProposalRejectRequest(StrictModel):
    note: str | None = Field(default=None, max_length=4_000)


class ProposalVoteOut(BaseModel):
    voter: str
    score: int
    comment: str | None
    created_at: datetime


class ProposalOut(BaseModel):
    id: uuid.UUID
    doc_key: str
    title: str
    content: str
    frontmatter: dict[str, Any]
    base_version: int
    base_content_hash: str
    mode: str
    status: str
    created_by: str
    reviewed_by: str | None
    review_note: str | None
    applied_version: int | None
    applied_by: str | None
    tainted: bool
    taint_sources: list[Any]
    conflict_details: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    votes: list[ProposalVoteOut] = Field(default_factory=list)
    vote_score: float = 0
    vote_count: int = 0


class ProposalListResponse(BaseModel):
    items: list[ProposalOut]


class ProposalApplyResponse(BaseModel):
    proposal: ProposalOut
    document: DocumentOut
    rebased: bool = False


# ------------------------------------------------------------- v3.2 policies


class PolicyCreateRequest(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    resource_type: Literal["document", "slot", "task"] = "document"
    pattern: str = Field(min_length=1, max_length=256)
    priority: int = Field(default=0, ge=-100, le=100)
    require_proposal: bool = False
    allowed_roles: list[RoleName] = Field(default_factory=list)
    json_schema: dict[str, Any] = Field(default_factory=dict)
    max_content_bytes: int | None = Field(default=None, ge=1, le=10_000_000)
    require_verified_sources: bool = False
    active: bool = True

    @model_validator(mode="after")
    def _schema_size(self) -> PolicyCreateRequest:
        if _serialized_size(self.json_schema) > 65_536:
            raise ValueError("json_schema is too large")
        return self


class PolicyUpdateRequest(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    priority: int | None = Field(default=None, ge=-100, le=100)
    require_proposal: bool | None = None
    allowed_roles: list[RoleName] | None = None
    json_schema: dict[str, Any] | None = None
    max_content_bytes: int | None = Field(default=None, ge=1, le=10_000_000)
    require_verified_sources: bool | None = None
    active: bool | None = None


class PolicyOut(BaseModel):
    id: uuid.UUID
    name: str
    resource_type: str
    pattern: str
    priority: int
    require_proposal: bool
    allowed_roles: list[str]
    json_schema: dict[str, Any]
    max_content_bytes: int | None
    require_verified_sources: bool
    active: bool
    created_at: datetime
    updated_at: datetime


class PolicyListResponse(BaseModel):
    items: list[PolicyOut]


# ----------------------------------------------------------- v3.2 human auth


class HumanUserCreateRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(min_length=1, max_length=128)
    role: Literal["OWNER", "REVIEWER", "VIEWER"] = "REVIEWER"
    password: str | None = Field(default=None, min_length=8, max_length=256)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError("a valid email is required")
        return value


class HumanUserUpdateRequest(StrictModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    role: Literal["OWNER", "REVIEWER", "VIEWER"] | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=256)

    @model_validator(mode="after")
    def _at_least_one(self) -> HumanUserUpdateRequest:
        if (
            self.display_name is None
            and self.role is None
            and self.is_active is None
            and self.password is None
        ):
            raise ValueError("at least one user field is required")
        return self


class HumanUserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    role: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


class HumanUserCreatedResponse(BaseModel):
    user: HumanUserOut
    temporary_password: str


class HumanUserListResponse(BaseModel):
    items: list[HumanUserOut]


class HumanLoginRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return value.strip().lower()


class HumanRegisterRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=256)
    display_name: str = Field(min_length=1, max_length=128)
    workspace_name: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError("a valid email is required")
        return value


class GoogleLoginRequest(StrictModel):
    credential: str = Field(min_length=20, max_length=16_384)
    display_name: str | None = Field(default=None, max_length=128)
    workspace_name: str | None = Field(default=None, max_length=128)


class AuthConfigResponse(BaseModel):
    signup_enabled: bool
    google_enabled: bool
    google_client_id: str | None = None


class HumanSessionOut(BaseModel):
    token: str = Field(description="Shown exactly once; store it securely.")
    expires_at: datetime
    user: HumanUserOut
    workspace: WorkspaceInfo


class HumanMeResponse(BaseModel):
    user: HumanUserOut
    workspace: WorkspaceInfo
    role: str


# --------------------------------------------------------- v3.2 budget/usage


class BudgetUpdateRequest(StrictModel):
    monthly_token_budget: int = Field(ge=0)
    monthly_cost_budget_usd: float = Field(ge=0)
    max_requests_per_minute: int = Field(ge=1, le=1_000_000)
    max_concurrent_leases: int = Field(ge=1, le=100_000)


class BudgetOut(BaseModel):
    workspace_id: uuid.UUID
    monthly_token_budget: int
    monthly_cost_budget_usd: float
    max_requests_per_minute: int
    max_concurrent_leases: int
    updated_at: datetime


class UsageOut(BaseModel):
    tokens_used: int
    cost_usd_used: float
    entries: int
    tokens_limit: int
    cost_usd_limit: float
    requests_limit_per_minute: int
    max_concurrent_leases: int
    period_days: int


# ----------------------------------------------------------- v3.2 webhooks


class WebhookRuleCreateRequest(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=8, max_length=2_048)
    secret: str | None = Field(default=None, min_length=16, max_length=256)
    event_types: list[str] = Field(default_factory=lambda: ["*"], max_length=100)
    tables: list[str] = Field(default_factory=list, max_length=20)
    namespaces: list[str] = Field(default_factory=list, max_length=20)
    active: bool = True

    @field_validator("event_types")
    @classmethod
    def _event_types(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip().upper() for item in value if item.strip()]
        if not cleaned:
            raise ValueError("at least one event type is required")
        return list(dict.fromkeys(cleaned))

class WebhookRuleUpdateRequest(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    url: str | None = Field(default=None, min_length=8, max_length=2_048)
    secret: str | None = Field(default=None, min_length=16, max_length=256)
    event_types: list[str] | None = None
    tables: list[str] | None = None
    namespaces: list[str] | None = None
    active: bool | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> WebhookRuleUpdateRequest:
        if not any(
            value is not None
            for value in (
                self.name,
                self.url,
                self.secret,
                self.event_types,
                self.tables,
                self.namespaces,
                self.active,
            )
        ):
            raise ValueError("at least one rule field is required")
        return self


class WebhookRuleOut(BaseModel):
    id: uuid.UUID
    name: str
    url: str
    secret_set: bool
    event_types: list[str]
    tables: list[str]
    namespaces: list[str]
    active: bool
    last_event_id: int
    consecutive_failures: int
    created_at: datetime
    updated_at: datetime


class WebhookRuleListResponse(BaseModel):
    items: list[WebhookRuleOut]


class WebhookDeliveryOut(BaseModel):
    id: uuid.UUID
    rule_id: uuid.UUID
    event_id: int
    status: str
    attempts: int
    response_status: int | None
    last_error: str | None
    next_attempt_at: datetime | None
    created_at: datetime
    delivered_at: datetime | None


class WebhookDeliveryListResponse(BaseModel):
    items: list[WebhookDeliveryOut]


# ----------------------------------------------------- v3.2 external actions


class ExternalActionCreateRequest(StrictModel):
    action_type: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    target_url: str = Field(min_length=8, max_length=2_048)
    method: Literal["POST", "PUT", "PATCH", "DELETE"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=128)
    compensation_payload: dict[str, Any] | None = None
    namespace: Namespace = Field(
        default="default", pattern=_NAMESPACE_PATTERN + "$"
    )
    session_batch_id: uuid.UUID | None = None
    agent: str | None = None

    @field_validator("agent")
    @classmethod
    def _agent(cls, value: str | None) -> str | None:
        return _clean_agent(value)

    @model_validator(mode="after")
    def _size(self) -> ExternalActionCreateRequest:
        if _serialized_size(self.payload) > settings.max_state_bytes:
            raise ValueError("payload is too large")
        if _serialized_size(self.compensation_payload or {}) > settings.max_state_bytes:
            raise ValueError("compensation_payload is too large")
        return self


class ExternalActionRejectRequest(StrictModel):
    note: str | None = Field(default=None, max_length=4_000)


class ExternalActionCompensateRequest(StrictModel):
    payload: dict[str, Any] | None = None


class ExternalActionOut(BaseModel):
    id: uuid.UUID
    action_type: str
    target_url: str
    method: str
    idempotency_key: str
    status: str
    attempts: int
    max_attempts: int
    last_error: str | None
    response_status: int | None
    approved_by: str | None
    approved_at: datetime | None
    next_attempt_at: datetime | None
    completed_at: datetime | None
    compensates_action_id: uuid.UUID | None
    compensation_payload: dict[str, Any] | None
    namespace: str
    session_batch_id: uuid.UUID | None
    created_by: str
    created_at: datetime
    updated_at: datetime


class ExternalActionListResponse(BaseModel):
    items: list[ExternalActionOut]


# ------------------------------------------------------------- v3.2 taint


class DocumentTaintRequest(StrictModel):
    tainted: bool
    sources: list[Any] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=4_000)
    expected_version: int | None = Field(default=None, ge=1)
