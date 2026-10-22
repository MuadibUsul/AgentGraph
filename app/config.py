"""Application configuration for AgentGraph v3.2.1."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------ app
    app_name: str = "AgentGraph"
    app_version: str = "3.2.1"
    environment: Literal["development", "staging", "production", "test"] = "development"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    docs_enabled: bool = True
    docs_require_auth: bool = False
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    # ------------------------------------------------------------- database
    database_url: str = (
        "postgresql+asyncpg://agentgraph:agentgraph@localhost:5432/agentgraph"
    )
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: float = 30.0
    auto_init_db: bool = False

    # ----------------------------------------------------------------- auth
    admin_token: str | None = None
    api_key_prefix: str = "ag_"
    agent_key_prefix: str = "agt_"
    human_key_prefix: str = "agh_"

    # ------------------------------------------------------------ embeddings
    embedding_provider: Literal["openai", "ollama", "hash"] = "hash"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(default=1024, ge=1, le=2000)
    embedding_batch_size: int = 64
    embedding_batch_interval_seconds: float = Field(default=0.0, ge=0, le=60)
    embedding_timeout_seconds: float = 30.0
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    ollama_base_url: str = "http://localhost:11434"
    ollama_embedding_model: str = "nomic-embed-text"

    # ------------------------------------------------------- blackboard rules
    default_task_lease_seconds: int = Field(default=60, ge=5, le=86_400)
    max_task_lease_seconds: int = Field(default=3_600, ge=5, le=86_400)
    lease_reaper_interval_seconds: int = Field(default=15, ge=5, le=3_600)
    # AG-CO-03: lease TTL when a 2026-11 client sets none (heartbeat every
    # ttl/3), and how many orphanings escalate a task to a human.
    lease_default_ttl_seconds: int = Field(default=900, ge=60, le=3_600)
    orphan_escalation_threshold: int = Field(default=3, ge=1, le=100)
    default_max_retries: int = Field(default=3, ge=1, le=100)
    max_reject_history: int = Field(default=20, ge=1, le=500)
    human_override_default_lock: bool = True

    # --------------------------------------------------------------- retrieval
    search_top_k: int = Field(default=5, ge=1, le=100)
    max_search_top_k: int = Field(default=50, ge=1, le=200)
    semantic_threshold: float = Field(default=0.15, ge=0.0, le=1.0)
    default_timeline_hours: int = 24
    max_context_timeline: int = 50
    max_context_slots: int = 100
    preview_chars: int = 280
    timeline_payload_chars: int = 180
    token_chars_per_token: float = 4.0
    max_workspace_map_items: int = 300
    max_page_size: int = 500
    # HMAC key for list cursors (AG-ST-01); derived from the database URL
    # when unset, so cursors survive restarts of the same deployment.
    cursor_signing_secret: str | None = None
    list_cursor_ttl_hours: int = Field(default=24, ge=1, le=720)
    # Exports (AG-ST-02): manual exports per tenant per hour, how long an
    # archive stays downloadable, and the largest archive an import accepts.
    exports_per_hour: int = Field(default=4, ge=1, le=1_000)
    export_retention_days: int = Field(default=30, ge=1, le=3_650)
    max_import_bytes: int = Field(default=512 * 1024 * 1024, ge=1_024)

    # Token tree (AG-ID-02): issue_child_token stays off until an operator
    # enables it (PRD gate: root/branch split, lineage and revocation first).
    child_token_issuance: bool = False

    # ------------------------------------------- backups (AG-ST-10, R1.3)
    # Archives go to BACKUP_DIR, which must live on other storage than the
    # database (another disk, an NFS/object-storage mount). Unset = no backups.
    backup_dir: str | None = None
    backup_scheduler_enabled: bool = True
    # Incrementals every 15 minutes keep the logical RPO at 15 minutes.
    backup_interval_minutes: int = Field(default=15, ge=1, le=1_440)
    backup_full_interval_hours: int = Field(default=24, ge=1, le=720)
    backup_retention_days: int = Field(default=30, ge=1, le=3_650)
    # Optional command run after each backup, e.g. "rclone copy {path} remote:ag".
    backup_upload_command: str | None = None
    # Restores and drills target this database, never production.
    staging_database_url: str | None = None
    restore_drill_interval_days: int = Field(default=30, ge=1, le=365)
    restore_drill_sample: int = Field(default=100, ge=1, le=100_000)

    # ------------------------------------------ incidents (AG-TC-06, R1.3)
    incident_check_interval_seconds: int = Field(default=60, ge=5, le=3_600)
    # Shown in every notice as "our contact"; required to send notices.
    security_contact: str | None = None
    platform_admin_email: str | None = None
    # Outgoing mail; without SMTP_HOST messages stay in email_outbox as no_transport.
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65_535)
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "AgentGraph <no-reply@localhost>"
    smtp_starttls: bool = True
    # Per-token access log (scope resolution for reads).
    access_log_flush_seconds: float = Field(default=5.0, ge=0.1, le=300)
    access_log_max_objects: int = Field(default=500, ge=10, le=10_000)

    # ----------------------------------------------------- proposals / policy
    proposals_enabled: bool = True
    default_proposal_quorum: int = 1
    max_proposal_content_chars: int = 100_000
    proposal_arena_max_entries: int = 20

    # ------------------------------------------------------------------ taint
    taint_propagation_enabled: bool = True
    taint_max_depth: int = 20
    taint_untrusted_source_default: bool = True

    # --------------------------------------------------------------- retrieval
    chunk_size_chars: int = Field(default=1_800, ge=200, le=20_000)
    chunk_overlap_chars: int = Field(default=200, ge=0, le=2_000)
    hybrid_candidate_multiplier: int = Field(default=4, ge=1, le=20)
    hybrid_rrf_k: int = Field(default=60, ge=1, le=1_000)
    hnsw_ef_search: int = Field(default=100, ge=10, le=1_000)

    # ------------------------------------------------------------- human auth
    human_session_hours: int = Field(default=12, ge=1, le=720)
    # PRD AG-ID-07 point 8 / D21: deletion grace period before physical purge.
    tenant_deletion_grace_days: int = Field(default=30, ge=1, le=365)
    password_hash_n: int = Field(default=2**14, ge=2**12, le=2**18)
    password_hash_r: int = 8
    password_hash_p: int = 1
    human_login_max_attempts_per_minute: int = Field(default=10, ge=1, le=1_000)
    auth_ip_max_attempts_per_minute: int = Field(default=60, ge=1, le=10_000)
    auth_global_max_attempts_per_minute: int = Field(default=600, ge=1, le=100_000)
    signup_global_max_per_minute: int = Field(default=60, ge=1, le=10_000)
    human_signup_enabled: bool = True
    signup_default_tier: str = "free"
    google_client_id: str | None = None

    # ---------------------------------------------------------- row security
    rls_enabled: bool = True
    rls_role: str = "agentgraph_app"
    rls_require_context: bool = False

    # ------------------------------------------------------ rate / budgets
    rate_limit_enabled: bool = True
    rate_limit_default_per_minute: int = Field(default=600, ge=1, le=1_000_000)
    rate_limit_window_seconds: int = Field(default=60, ge=1, le=3_600)
    default_monthly_token_budget: int = Field(default=10_000_000, ge=0)
    default_monthly_cost_budget_usd: float = Field(default=1000.0, ge=0)
    default_max_concurrent_leases: int = Field(default=100, ge=1)

    # ---------------------------------------------------- outbound actions
    outbound_dispatch_interval_seconds: int = Field(default=5, ge=1, le=300)
    outbound_max_attempts: int = Field(default=5, ge=1, le=20)
    outbound_timeout_seconds: float = 10.0
    outbound_max_response_bytes: int = Field(default=65_536, ge=1_024, le=1_048_576)
    outbound_allow_private_networks: bool = True
    outbound_allowed_hosts: list[str] = Field(default_factory=list)
    outbound_signing_secret: str | None = None
    webhook_max_delivery_attempts: int = Field(default=6, ge=1, le=20)
    webhook_batch_size: int = Field(default=100, ge=1, le=1000)

    # ---------------------------------------------------------------- quota
    free_max_documents: int = 5_000
    free_max_slots: int = 20
    developer_max_documents: int = 50_000
    developer_max_slots: int = 100
    pro_max_documents: int = 500_000
    pro_max_slots: int = 1_000
    # PRD AG-ST-03: documents up to 1 MiB of UTF-8; larger content uses parts.
    max_document_content_bytes: int = 1_048_576
    # Character ceiling for request validation only; the byte limit decides.
    max_document_content_chars: int = 4_194_304
    max_state_bytes: int = 100_000
    max_brief_bytes: int = 32_768
    max_artifact_chars: int = 4_194_304

    # ------------------------------------------------ async index (AG-ST-13)
    index_worker_enabled: bool = True
    index_worker_interval_seconds: float = Field(default=1.0, ge=0.1, le=60)
    index_worker_batch: int = Field(default=20, ge=1, le=500)
    index_max_attempts: int = Field(default=5, ge=1, le=50)

    # ------------------------------------------------ cost control (AG-ST-15)
    # "shadow" records would-be limits without blocking (run ~14 days to
    # calibrate); "enforce" returns 429 rate_limited / circuit_open.
    budget_mode: str = Field(default="shadow", pattern="^(shadow|enforce)$")
    write_rate_per_minute: int = Field(default=60, ge=1, le=100_000)
    write_burst: int = Field(default=20, ge=1, le=100_000)
    query_rate_per_minute: int = Field(default=120, ge=1, le=100_000)
    hot_key_writes_per_minute: int = Field(default=20, ge=2, le=10_000)
    circuit_open_minutes: int = Field(default=10, ge=1, le=1_440)
    embedding_tokens_per_day: int = Field(default=2_000_000, ge=1)
    # Server-enforced backoff: a request before Retry-After is refused without
    # taking a token and doubles the next wait; repeated violations trip a
    # short circuit (reason ignored_backoff).
    backoff_max_seconds: int = Field(default=60, ge=1, le=3_600)
    backoff_violations_to_trip: int = Field(default=3, ge=1, le=100)
    backoff_circuit_minutes: int = Field(default=1, ge=1, le=1_440)
    # A token's write rate above this multiple of its own 7-day p99 for the
    # same hour trips the circuit (reason write_spike); needs enough history.
    write_spike_multiplier: float = Field(default=10.0, ge=1.0, le=1_000.0)
    write_spike_min_history_minutes: int = Field(default=30, ge=1, le=10_080)
    # In-flight requests per credential; long-polls (SSE) have their own pool.
    max_in_flight_per_token: int = Field(default=4, ge=1, le=10_000)
    max_streams_per_token: int = Field(default=1, ge=1, le=100)

    # ------------------------------------------- failure-semantics protocol
    # PRD AG-ST-18: 24 h idempotency window; an in-progress record older than
    # the stale limit is treated as abandoned (crashed worker) and reclaimed.
    idempotency_window_hours: int = Field(default=24, ge=1, le=168)
    idempotency_stale_seconds: int = Field(default=300, ge=10, le=3_600)

    # -------------------------------------------------------------- realtime
    sse_heartbeat_seconds: int = Field(default=15, ge=5, le=120)
    sse_max_connections: int = Field(default=200, ge=1, le=10_000)
    sse_max_connections_per_principal: int = Field(default=5, ge=1, le=100)
    sse_revalidate_seconds: int = Field(default=30, ge=5, le=600)
    sse_max_lifetime_seconds: int = Field(default=3_600, ge=60, le=86_400)
    embedding_bytes_per_minute: int = Field(default=5_000_000, ge=100_000)
    sse_replay_limit: int = Field(default=500, ge=1, le=5_000)
    event_retention_days: int = Field(default=7, ge=1, le=365)
    auto_timeline_events: bool = True

    # ------------------------------------------------------------------ mcp
    request_timeout_seconds: float = 30.0
    mcp_enabled: bool = True
    mcp_stateless_http: bool = True
    mcp_json_response: bool = True
    mcp_api_key: str | None = None
    mcp_agent_name: str = "mcp_agent"
    mcp_dns_rebinding_protection: bool = True
    mcp_allowed_hosts: list[str] = Field(default_factory=list)
    mcp_allowed_origins: list[str] = Field(default_factory=list)

    @field_validator("cors_origins", "outbound_allowed_hosts", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str) and not value.startswith("["):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _openai_key_required(self) -> Settings:
        if self.embedding_provider == "openai" and not self.openai_api_key:
            raise ValueError(
                "OPENAI_API_KEY is required when EMBEDDING_PROVIDER=openai"
            )
        return self

    @field_validator("max_search_top_k")
    @classmethod
    def _search_cap(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_search_top_k must be positive")
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def tier_limits(self, tier: str) -> tuple[int, int]:
        if tier == "free":
            return self.free_max_documents, self.free_max_slots
        if tier == "pro":
            return self.pro_max_documents, self.pro_max_slots
        return self.developer_max_documents, self.developer_max_slots


@lru_cache
def get_settings() -> Settings:
    return Settings()
