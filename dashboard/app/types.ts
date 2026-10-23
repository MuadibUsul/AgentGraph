export type ApiRequest = <T>(path: string, init?: RequestInit) => Promise<T>;

export type TaskStatus =
  | "TODO"
  | "READY"
  | "IN_PROGRESS"
  | "REVIEWING"
  | "HANDOFF"
  | "AWAITING_HUMAN"
  | "ESCALATED_TO_HUMAN"
  | "DONE"
  | "FAILED";

export interface WorkspaceSummary {
  id: string;
  name: string;
  tier: string;
  created_at: string;
  documents: number;
  slots: number;
  tasks: number;
  timeline_events: number;
  last_activity_at: string | null;
}

export interface Quota {
  used: number;
  limit: number;
  percentage: number;
}

export interface Stats {
  workspace: { id: string; name: string; tier: string; created_at: string };
  documents: number;
  slots: number;
  tasks: number;
  agent_tokens: number;
  timeline_events: number;
  audit_entries: number;
  escalated_tasks: number;
  tokens_logged: number;
  avg_latency_ms: number;
  last_activity_at: string | null;
  document_quota: Quota;
  slot_quota: Quota;
  embedding_provider: string;
  embedding_model: string;
  embedding_dimensions: number;
  pending_proposals: number;
  pending_external_actions: number;
  webhook_rules: number;
  tainted_documents: number;
  tokens_used_30d: number;
  document_namespaces?: Record<string, number>;
}

export interface TaskSummary {
  id: string;
  task_key: string;
  title: string;
  status: TaskStatus;
  priority: number;
  assigned_role: string | null;
  namespace: string;
  claimed_by: string | null;
  lease_expires_at: string | null;
  lease_active: boolean;
  handoff_summary: string | null;
  artifact_uri: string | null;
  human_feedback: string | null;
  review_feedback: string | null;
  reviewed_by: string | null;
  ping_pong_count: number;
  max_retries: number;
  version: number;
  lease_epoch: number;
  tainted: boolean;
  taint_sources: TaintSource[];
  updated_at: string;
}

export interface TaskDetail extends TaskSummary {
  description: string | null;
  input_context: Record<string, unknown>;
  output_artifact: Record<string, unknown>;
  handoff_brief: HandoffBrief | null;
  human_feedback: string | null;
  reject_history: unknown[];
  created_by: string;
  created_at: string;
}

export interface HandoffBrief {
  summary?: string;
  key_metrics?: Record<string, unknown>;
  unverified_hypotheses?: string[];
  risk_flag?: boolean;
  [key: string]: unknown;
}

export interface Slot {
  id: string;
  slot_key: string;
  goal: string | null;
  current_state: Record<string, unknown>;
  schema_hint: Record<string, unknown>;
  blockers: unknown[];
  namespace: string;
  version: number;
  locked_by: string | null;
  lock_expires_at: string | null;
  lease_active: boolean;
  lease_epoch: number;
  locked_by_human: boolean;
  human_note: string | null;
  tainted: boolean;
  taint_sources: TaintSource[];
  updated_by: string;
  updated_at: string;
}

export interface DocumentPage {
  items: DocumentSummary[];
  next_cursor: string | null;
  total: number;
  count: number;
}

export interface DocumentSummary {
  id: string;
  doc_key: string;
  title: string;
  namespace: string;
  version: number;
  frontmatter: Record<string, unknown>;
  verification_status: string;
  trust_level: string;
  verified_version: number | null;
  verified_hash: string | null;
  content_hash: string | null;
  embedding_model: string | null;
  locked_by_human: boolean;
  human_note: string | null;
  tainted: boolean;
  taint_sources: TaintSource[];
  score?: number | null;
  updated_by: string;
  updated_at: string;
}

export interface DocumentDetail extends DocumentSummary {
  content: string;
  created_by: string;
  created_at: string;
  agent_view: string;
}

export interface AgentToken {
  id: string;
  agent_signature: string;
  role_scope: string;
  allowed_namespaces: string[];
  is_active: boolean;
  created_at: string;
  last_seen_at: string | null;
  scope_add?: string[];
  scope_remove?: string[];
  canonical_id?: string | null;
  expires_at?: string | null;
  revoked_at?: string | null;
  issued_by?: "human" | "agent";
  issued_by_agent?: string | null;
  depth?: number;
  parent_token_id?: string | null;
  delegated_active?: number;
  delegated_total?: number;
}

export interface DocumentVersionSummary {
  version: number;
  parent_version: number | null;
  content_hash: string;
  author: string;
  session_batch_id: string | null;
  change_summary: string | null;
  verification_status: string;
  trust_level: string;
  tainted: boolean;
  taint_sources: TaintSource[];
  created_at: string;
}

export interface DocumentVersion extends DocumentVersionSummary {
  id: string;
  document_id: string;
  title: string;
  content: string;
  frontmatter: Record<string, unknown>;
  namespace: string;
}

export interface TimelineItem {
  id: string;
  agent_signature: string;
  platform: string;
  event_type: string;
  payload: Record<string, unknown>;
  namespace: string;
  token_cost: number;
  latency_ms: number;
  created_at: string;
}

export interface AuditDiff {
  id: string;
  session_batch_id: string;
  agent_signature: string;
  target_table: string;
  target_record_id: string;
  operation: string;
  before_diff: Record<string, unknown>;
  after_diff: Record<string, unknown>;
  created_at: string;
}

export interface AuditBatch {
  session_batch_id: string;
  agent_signature: string;
  entry_count: number;
  targets: string[];
  first_at: string;
  last_at: string;
}

export interface AuditPayload {
  batches: AuditBatch[];
  diffs: AuditDiff[];
  total_batches?: number;
  next_cursor?: string | null;
}

export interface StreamEvent {
  table: string;
  action: string;
  workspace_id: string;
  record_id: string;
  actor: string;
  version: string;
  status: string;
  slot_key?: string | null;
  task_key?: string | null;
  doc_key?: string | null;
  namespace?: string;
  assigned_role?: string;
  locked_by_human?: string;
  timestamp: string;
}

export interface ReadyInfo {
  status: string;
  database: string;
  embedding_provider: string;
  embedding_model: string;
  version: string;
}

export interface KeyReveal {
  key: string;
  name: string;
  kind: "workspace" | "rotated" | "agent";
  role?: string;
  namespaces?: string[];
}

export interface TaintSource {
  doc_key?: string;
  uri?: string;
  reason?: string;
  depth?: number;
  [key: string]: unknown;
}

export interface ProposalVote {
  voter: string;
  score: number;
  comment: string | null;
  created_at: string;
}

export interface Proposal {
  id: string;
  doc_key: string;
  title: string;
  content: string;
  frontmatter: Record<string, unknown>;
  base_version: number;
  base_content_hash: string;
  mode: string;
  status: string;
  created_by: string;
  reviewed_by: string | null;
  review_note: string | null;
  applied_version: number | null;
  applied_by: string | null;
  tainted: boolean;
  taint_sources: TaintSource[];
  conflict_details: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
  votes: ProposalVote[];
  vote_score: number;
  vote_count: number;
}

export interface PathPolicy {
  id: string;
  name: string;
  resource_type: string;
  pattern: string;
  priority: number;
  require_proposal: boolean;
  allowed_roles: string[];
  json_schema: Record<string, unknown>;
  max_content_bytes: number | null;
  require_verified_sources: boolean;
  active: boolean;
  created_at: string;
  updated_at: string;
}

export interface Usage {
  tokens_used: number;
  cost_usd_used: number;
  entries: number;
  tokens_limit: number;
  cost_usd_limit: number;
  requests_limit_per_minute: number;
  max_concurrent_leases: number;
  period_days: number;
}

export interface Budget extends Usage {
  workspace_id: string;
  monthly_token_budget: number;
  monthly_cost_budget_usd: number;
  max_requests_per_minute: number;
  max_concurrent_leases: number;
  updated_at: string;
}

export interface WebhookRule {
  id: string;
  name: string;
  url: string;
  secret_set: boolean;
  event_types: string[];
  tables: string[];
  namespaces: string[];
  active: boolean;
  last_event_id: number;
  consecutive_failures: number;
  created_at: string;
  updated_at: string;
}

export interface WebhookDelivery {
  id: string;
  rule_id: string;
  event_id: number;
  status: string;
  attempts: number;
  response_status: number | null;
  last_error: string | null;
  next_attempt_at: string | null;
  created_at: string;
  delivered_at: string | null;
}

export interface ExternalAction {
  id: string;
  action_type: string;
  target_url: string;
  method: string;
  payload: Record<string, unknown> | null;
  headers: Record<string, string> | null;
  idempotency_key: string;
  status: string;
  attempts: number;
  max_attempts: number;
  last_error: string | null;
  response_status: number | null;
  approved_by: string | null;
  approved_at: string | null;
  next_attempt_at: string | null;
  completed_at: string | null;
  compensates_action_id: string | null;
  compensation_payload: Record<string, unknown> | null;
  namespace: string;
  session_batch_id: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface HumanUser {
  id: string;
  email: string;
  display_name: string;
  role: string;
  is_active: boolean;
  created_at: string;
  last_login_at: string | null;
}

export interface HumanUserCreated {
  user: HumanUser;
  temporary_password: string;
}

export interface AuthConfig {
  signup_enabled: boolean;
  google_enabled: boolean;
  google_client_id: string | null;
}

export interface AuthSession {
  token: string;
  expires_at: string;
  user: HumanUser;
  workspace: { id: string; name: string; tier: string; created_at: string };
}

export type FormatLanguage = "en" | "zh";

let formatLanguage: FormatLanguage = "en";

export function setFormatLanguage(language: FormatLanguage): void {
  formatLanguage = language;
}

const ZH_STATUS: Record<string, string> = {
  TODO: "待办",
  READY: "就绪",
  IN_PROGRESS: "进行中",
  REVIEWING: "审核中",
  HANDOFF: "交接中",
  AWAITING_HUMAN: "等待人工",
  ESCALATED_TO_HUMAN: "已升级人工",
  DONE: "完成",
  FAILED: "失败",
  DRAFT: "草稿",
  PENDING_REVIEW: "待审核",
  VERIFIED: "已验证",
  DEPRECATED: "已废弃",
  PENDING: "待处理",
  PENDING_APPROVAL: "待批准",
  APPROVED: "已批准",
  EXECUTING: "执行中",
  SUCCEEDED: "成功",
  REJECTED: "已拒绝",
  APPLIED: "已应用",
  SUPERSEDED: "已被取代",
  COMPENSATION_PENDING: "补偿待处理",
  COMPENSATED: "已补偿",
  ACTIVE: "启用",
};

export function normalizeBase(value: string): string {
  return value.trim().replace(/\/+$/, "");
}

export function formatNumber(value: number | null | undefined): string {
  return new Intl.NumberFormat(formatLanguage === "zh" ? "zh-CN" : "en-US").format(
    value ?? 0,
  );
}

export function relativeTime(value: string | null | undefined): string {
  if (!value) return formatLanguage === "zh" ? "从未" : "never";
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return formatLanguage === "zh" ? "未知" : "unknown";
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 45) return formatLanguage === "zh" ? "刚刚" : "just now";
  if (seconds < 3600) {
    const minutes = Math.floor(seconds / 60);
    return formatLanguage === "zh" ? `${minutes} 分钟前` : `${minutes}m ago`;
  }
  if (seconds < 86400) {
    const hours = Math.floor(seconds / 3600);
    return formatLanguage === "zh" ? `${hours} 小时前` : `${hours}h ago`;
  }
  if (seconds < 86400 * 30) {
    const days = Math.floor(seconds / 86400);
    return formatLanguage === "zh" ? `${days} 天前` : `${days}d ago`;
  }
  return new Date(value).toLocaleDateString(
    formatLanguage === "zh" ? "zh-CN" : "en-US",
  );
}

export function shortDate(value: string | null | undefined): string {
  if (!value) return "—";
  return new Date(value).toLocaleDateString(
    formatLanguage === "zh" ? "zh-CN" : "en-US",
    {
      year: "numeric",
      month: "short",
      day: "numeric",
    },
  );
}

export function shortId(value: string, length = 8): string {
  return `${value.slice(0, length)}…`;
}

export function tierLabel(tier: string): string {
  if (tier === "free") return formatLanguage === "zh" ? "免费" : "Free";
  if (tier === "pro") return formatLanguage === "zh" ? "专业版" : "Pro";
  return formatLanguage === "zh" ? "开发者 · $5" : "Developer · $5";
}

export function statusLabel(status: string): string {
  if (formatLanguage === "zh") {
    return ZH_STATUS[status] ?? status.replaceAll("_", " ").toLowerCase();
  }
  return status.replaceAll("_", " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
}

export function statusTone(status: string): string {
  if (status === "DONE") return "mint";
  if (status === "FAILED") return "rose";
  if (status === "ESCALATED_TO_HUMAN" || status === "AWAITING_HUMAN") return "amber";
  if (status === "REVIEWING") return "violet";
  if (status === "IN_PROGRESS") return "sky";
  return "slate";
}

export function roleTone(role: string | null): string {
  if (role === "EXECUTOR") return "mint";
  if (role === "ANALYST") return "sky";
  if (role === "AUDITOR") return "violet";
  if (role === "SCOUT") return "amber";
  return "slate";
}

export function safeJson(value: string): Record<string, unknown> {
  const parsed = JSON.parse(value);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Expected a JSON object");
  }
  return parsed as Record<string, unknown>;
}

export function pretty(value: unknown): string {
  return JSON.stringify(value ?? {}, null, 2);
}

export interface DocumentParts {
  part_of: string;
  count: number;
  truncated: boolean;
  parts: {
    doc_key: string;
    namespace: string;
    title: string | null;
    version: number;
    part_index: number | string | null;
    verification_status: string;
    tainted: boolean;
    content: string;
  }[];
}
