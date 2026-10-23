"use client";

import { useEffect, useState } from "react";

import { useI18n } from "../i18n";
import type { TaskDetail, TaskSummary } from "../types";
import { formatNumber, pretty, relativeTime } from "../types";
import {
  Empty,
  IconCheck,
  IconChevron,
  IconLock,
  IconPlus,
  IconX,
  Modal,
  RolePill,
  Spinner,
  StatusPill,
} from "./ui";

const COLUMNS: Array<{ title: string; detail: string; statuses: string[]; tone: string }> = [
  { title: "Backlog / Ready", detail: "TODO · READY", statuses: ["TODO", "READY"], tone: "slate" },
  { title: "In progress", detail: "IN_PROGRESS · REVIEWING · HANDOFF", statuses: ["IN_PROGRESS", "REVIEWING", "HANDOFF"], tone: "sky" },
  { title: "Human gate", detail: "AWAITING_HUMAN · ESCALATED", statuses: ["AWAITING_HUMAN", "ESCALATED_TO_HUMAN"], tone: "amber" },
  { title: "Closed", detail: "DONE · FAILED", statuses: ["DONE", "FAILED"], tone: "mint" },
];

export function TaskBoard({
  tasks,
  onOpen,
  onCreate,
}: {
  tasks: TaskSummary[];
  onOpen: (task: TaskSummary) => void;
  onCreate: () => void;
}) {
  const { t } = useI18n();
  return (
    <div className="tab-stack">
      <div className="board-toolbar">
        <div>
          <h2 className="section-title">{t("Task board")}</h2>
          <p className="section-sub">
            {t("Claims are lease-based. Rejections increment the ping-pong counter and escalate automatically at the threshold.")}
          </p>
        </div>
        <button
          type="button"
          className="button primary"
          onClick={onCreate}
          data-modal-trigger="new-task"
        >
          <IconPlus /> {t("New task")}
        </button>
      </div>
      {tasks.length === 0 && (
        <Empty
          title={t("No tasks yet")}
          detail={t("Create a task for your agents, or let an ANALYST agent plan one. Agents claim READY tasks under a lease and hand them back for your review.")}
          action={
            <div className="empty-actions">
              <button type="button" className="button primary small" onClick={onCreate}>
                <IconPlus /> {t("New task")}
              </button>
              <a className="button ghost small" href="/docs/workflows/" target="_blank" rel="noreferrer">
                {t("How tasks work")}
              </a>
            </div>
          }
        />
      )}
      <div className="kanban">
        {COLUMNS.map((column) => {
          const items = tasks.filter((task) => column.statuses.includes(task.status));
          return (
            <div className="kanban-column" key={column.title}>
              <div className="kanban-head">
                <span className={`kanban-dot tone-${column.tone}`} />
                <span>{t(column.title)}</span>
                <span className="kanban-count">{items.length}</span>
              </div>
              <div className="kanban-sub">
                {column.statuses.map((status) => t(status)).join(" · ")}
              </div>
              <div className="kanban-list">
                {items.length === 0 ? (
                  <div className="kanban-empty">{t("No tasks")}</div>
                ) : (
                  items.map((task) => (
                    <button
                      key={task.id}
                      type="button"
                      className={`kanban-card task-status-${task.status}`}
                      onClick={() => onOpen(task)}
                    >
                      <div className="kanban-card-top">
                        <span className="mono muted" title={task.task_key}>{task.task_key}</span>
                        <span className="priority-chip">
                          {task.priority > 0 ? `P${task.priority}` : t("Normal")}
                        </span>
                      </div>
                      <div className="kanban-title" title={task.title}>{task.title}</div>
                      {task.handoff_summary && (
                        <div className="kanban-brief">{task.handoff_summary}</div>
                      )}
                      <div className="kanban-meta">
                        <RolePill role={task.assigned_role} />
                        <StatusPill status={task.status} />
                        {task.tainted && <span className="pill rose" title={t("Task input references tainted content")}>{t("Tainted")}</span>}
                        {task.ping_pong_count > 0 && (
                          <span className="pp-chip" title={t("Review ping-pong count")}>
                            {task.ping_pong_count}/{task.max_retries}
                          </span>
                        )}
                      </div>
                      <div className="kanban-foot">
                        <span>
                          {task.claimed_by
                            ? `${task.claimed_by} · ${task.lease_active ? t("lease live") : t("lease expired")}`
                            : task.status === "READY"
                              ? t("unclaimed")
                              : `v${task.version}`}
                        </span>
                        <span>{relativeTime(task.updated_at)}</span>
                      </div>
                    </button>
                  ))
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function TaskDetailModal({
  task,
  busy,
  onClose,
  onReview,
  onOpenArtifact,
}: {
  task: TaskDetail;
  busy: boolean;
  onClose: () => void;
  onReview: (decision: "approve" | "reject", feedback: string) => Promise<void>;
  onOpenArtifact: (artifactUri: string) => void;
}) {
  const { t } = useI18n();
  const [feedback, setFeedback] = useState(task.human_feedback ?? "");
  useEffect(() => {
    setFeedback(task.human_feedback ?? "");
  }, [task.id, task.human_feedback]);

  const reviewable = ["REVIEWING", "AWAITING_HUMAN", "ESCALATED_TO_HUMAN"].includes(
    task.status,
  );

  return (
    <Modal
      title={
        <span className="modal-title-row">
          <span className="mono">{task.task_key}</span>
          <StatusPill status={task.status} />
        </span>
      }
      subtitle={task.title}
      onClose={onClose}
      wide
      footer={
        <>
          <button type="button" className="button ghost" onClick={onClose}>
            {t("Close")}
          </button>
          {reviewable && (
            <>
              <button
                type="button"
                className="button danger"
                disabled={busy || !feedback.trim()}
                title={feedback.trim() ? undefined : t("A rejection needs a reason: write feedback first.")}
                onClick={() => void onReview("reject", feedback)}
              >
                {busy ? <Spinner /> : <IconX />} {t("Reject")}
              </button>
              <button
                type="button"
                className="button primary"
                disabled={busy}
                onClick={() => void onReview("approve", feedback)}
              >
                {busy ? <Spinner /> : <IconCheck />} {t("Approve")}
              </button>
            </>
          )}
        </>
      }
    >
      <div className="detail-grid">
        <div className="detail-cell">
          <span>{t("Assigned role")}</span>
          <strong>{task.assigned_role ? t(task.assigned_role) : t("Any role")}</strong>
        </div>
        <div className="detail-cell">
          <span>{t("Priority")}</span>
          <strong>{task.priority > 0 ? `P${task.priority}` : t("Normal")}</strong>
        </div>
        <div className="detail-cell">
          <span>{t("Version")}</span>
          <strong>v{task.version}</strong>
        </div>
        <div className="detail-cell">
          <span>{t("Namespace")}</span>
          <strong>{task.namespace}</strong>
        </div>
        <div className="detail-cell">
          <span>{t("Claim / lease")}</span>
          <strong>
            {task.claimed_by ?? t("unclaimed")}
            {task.lease_active ? ` · ${t("live")}` : ""}
          </strong>
        </div>
        <div className="detail-cell">
          <span>{t("Ping-pong")}</span>
          <strong>
            {task.ping_pong_count} / {task.max_retries}
          </strong>
        </div>
      </div>

      {task.description && <p className="detail-description">{task.description}</p>}

      {task.review_feedback && (
        <div className="detail-section">
          <h3>{t("Review feedback")}</h3>
          <div className="brief-box">
            <div className="brief-summary">{task.review_feedback}</div>
            {task.reviewed_by && (
              <div className="brief-metrics">
                <span className="metric-chip">{t("Reviewer")}: <strong>{task.reviewed_by}</strong></span>
              </div>
            )}
          </div>
        </div>
      )}

      <div className="detail-section">
        <div className="detail-section-head">
          <h3>{t("L1 handoff brief")}</h3>
          {task.artifact_uri && (
            <button
              type="button"
              className="button ghost small"
              onClick={() => onOpenArtifact(task.artifact_uri!)}
            >
              {t("Open L2 artifact")} <IconChevron size={13} />
            </button>
          )}
        </div>
        {task.handoff_brief ? (
          <div className="brief-box">
            <div className="brief-summary">{String(task.handoff_brief.summary ?? "—")}</div>
            <div className="brief-metrics">
              {Object.entries(task.handoff_brief.key_metrics ?? {}).map(([key, value]) => (
                <span key={key} className="metric-chip">
                  {key}: <strong>{String(value)}</strong>
                </span>
              ))}
              {task.handoff_brief.risk_flag && (
                <span className="metric-chip risk">
                  <IconLock size={11} /> {t("risk flagged")}
                </span>
              )}
            </div>
            {(task.handoff_brief.unverified_hypotheses ?? []).length > 0 && (
              <ul className="hypothesis-list">
                {task.handoff_brief.unverified_hypotheses!.map((item, index) => (
                  <li key={index}>{item}</li>
                ))}
              </ul>
            )}
          </div>
        ) : (
          <p className="muted">{t("No L1 brief yet.")}</p>
        )}
      </div>

      <div className="detail-columns">
        <div className="detail-section">
          <h3>{t("Input context")}</h3>
          <pre className="json-block">{pretty(task.input_context)}</pre>
        </div>
        <div className="detail-section">
          <h3>{t("Output artifact")}</h3>
          <pre className="json-block">{pretty(task.output_artifact)}</pre>
        </div>
      </div>

      {(task.reject_history ?? []).length > 0 && (
        <div className="detail-section">
          <h3>{t("Reject history")}</h3>
          <pre className="json-block">{pretty(task.reject_history)}</pre>
        </div>
      )}

      {reviewable && (
        <label className="field">
          <span>{t("Human feedback")}</span>
          <textarea
            rows={3}
            value={feedback}
            onChange={(event) => setFeedback(event.target.value)}
            placeholder={t("Approval note or rejection reason…")}
          />
        </label>
      )}
      <div className="detail-footer-meta">
        {t("Created by")} {task.created_by} · {relativeTime(task.created_at)} ·{" "}
        {t("Updated")} {relativeTime(task.updated_at)}
      </div>
    </Modal>
  );
}

export function CreateTaskModal({
  busy,
  onClose,
  onCreate,
}: {
  busy: boolean;
  onClose: () => void;
  onCreate: (payload: Record<string, unknown>) => Promise<void>;
}) {
  const { t } = useI18n();
  const [key, setKey] = useState("");
  const [title, setTitle] = useState("");
  const [role, setRole] = useState("EXECUTOR");
  const [priority, setPriority] = useState(0);
  const [namespace, setNamespace] = useState("default");
  const [description, setDescription] = useState("");
  const [context, setContext] = useState("{}");
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      const inputContext = JSON.parse(context || "{}");
      if (!inputContext || typeof inputContext !== "object" || Array.isArray(inputContext)) {
        throw new Error(t("Input context must be a JSON object"));
      }
      await onCreate({
        task_key: key.trim() || undefined,
        title: title.trim(),
        assigned_role: role,
        priority,
        namespace,
        description: description.trim() || undefined,
        input_context: inputContext,
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Invalid task"));
    }
  }

  return (
    <Modal
      title={t("New task")}
      subtitle={t("Create a task for an agent role. Agents can also create next tasks during handoff.")}
      onClose={onClose}
      returnFocusSelector='[data-modal-trigger="new-task"]'
    >
      <form onSubmit={submit}>
        <div className="form-grid">
          <label className="field">
            <span>{t("Task key (optional)")}</span>
            <input value={key} onChange={(e) => setKey(e.target.value)} placeholder="TASK-104" />
          </label>
          <label className="field">
            <span>{t("Priority")}</span>
            <input
              type="number"
              min={-100}
              max={100}
              value={priority}
              onChange={(e) => setPriority(Number(e.target.value))}
            />
          </label>
        </div>
        <label className="field">
          <span>{t("Title")}</span>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder={t("Validate BTC order-block liquidity")}
            required
            autoFocus
          />
        </label>
        <div className="form-grid">
          <label className="field">
            <span>{t("Assigned role")}</span>
            <select value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="EXECUTOR">{t("Executor")}</option>
              <option value="ANALYST">{t("Analyst")}</option>
              <option value="AUDITOR">{t("Auditor")}</option>
              <option value="SCOUT">{t("Scout")}</option>
            </select>
          </label>
          <label className="field">
            <span>{t("Namespace")}</span>
            <input value={namespace} onChange={(e) => setNamespace(e.target.value)} />
          </label>
        </div>
        <label className="field">
          <span>{t("Description")}</span>
          <textarea
            rows={3}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder={t("What should the agent do? Point to slot/doc keys instead of pasting raw logs.")}
          />
        </label>
        <label className="field">
          <span>{t("Input context (JSON)")}</span>
          <textarea
            rows={4}
            className="mono-input"
            value={context}
            onChange={(e) => setContext(e.target.value)}
          />
        </label>
        {error && <div className="inline-error">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="button ghost" onClick={onClose}>
            {t("Cancel")}
          </button>
          <button type="submit" className="button primary" disabled={busy || !title.trim()}>
            {busy ? <Spinner /> : <IconPlus />} {t("Create task")}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function TaskCount({ tasks }: { tasks: TaskSummary[] }) {
  const approvals = tasks.filter((t) => ["AWAITING_HUMAN", "ESCALATED_TO_HUMAN"].includes(t.status));
  return <>{formatNumber(approvals.length)}</>;
}
