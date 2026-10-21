"use client";

import { useI18n } from "../i18n";
import type { Slot, TaskSummary, TimelineItem } from "../types";
import { formatNumber, relativeTime } from "../types";
import {
  Empty,
  IconChevron,
  IconLock,
  IconPulse,
  StatusPill,
  roleTone,
} from "./ui";

function statePreview(value: Record<string, unknown>): string {
  // Keep the preview short enough that the ellipsis remains visible inside the
  // table column at 100% and 200%; the title attribute carries the full JSON.
  const text = JSON.stringify(value);
  return text.length > 34 ? `${text.slice(0, 34)}…` : text;
}

export function Blackboard({
  slots,
  tasks,
  timeline,
  onOpenSlot,
  onOpenTask,
}: {
  slots: Slot[];
  tasks: TaskSummary[];
  timeline: TimelineItem[];
  onOpenSlot: (slot: Slot) => void;
  onOpenTask: (task: TaskSummary) => void;
}) {
  const { t } = useI18n();
  const approvals = tasks.filter(
    (task) =>
      task.status === "AWAITING_HUMAN" || task.status === "ESCALATED_TO_HUMAN",
  );
  const interrupts = timeline
    .filter((item) =>
      ["HUMAN_OVERRIDE", "TASK_ESCALATED", "BATCH_UNDONE", "TASK_REJECTED"].includes(
        item.event_type,
      ),
    )
    .slice(0, 8);

  return (
    <div className="tab-stack">
      <section className={`card approval-card ${approvals.length ? "has-approvals" : ""}`}>
        <div className="card-head">
          <div>
            <h2>{t("Human approval gate")}</h2>
            <p>{t("Agents are paused until a human resolves these tasks.")}</p>
          </div>
          <span className={`count-chip ${approvals.length ? "warn" : ""}`}>
            {t("{count} waiting", { count: formatNumber(approvals.length) })}
          </span>
        </div>
        {approvals.length === 0 ? (
          <Empty
            title={t("No approvals waiting")}
            detail={t("Escalated or AWAITING_HUMAN tasks will appear here in real time.")}
          />
        ) : (
          <div className="approval-list">
            {approvals.map((task) => (
              <button
                key={task.id}
                type="button"
                className="approval-row"
                onClick={() => onOpenTask(task)}
              >
                <div className={`event-dot ${task.status === "ESCALATED_TO_HUMAN" ? "rose" : "amber"}`} />
                <div className="approval-main">
                  <div className="approval-title">
                    <span className="mono">{task.task_key}</span>
                    <span>{task.title}</span>
                    <StatusPill status={task.status} />
                  </div>
                  <div className="approval-meta">
                    <span>{task.handoff_summary ?? t("No L1 brief supplied")}</span>
                    {task.human_feedback && <span className="muted"> · {task.human_feedback}</span>}
                  </div>
                </div>
                <div className="approval-side">
                  <span className={`role-pill tone-${roleTone(task.assigned_role)}`}>
                    {task.assigned_role ? t(task.assigned_role) : t("ANY")}
                  </span>
                  <span className="muted">{relativeTime(task.updated_at)}</span>
                  <IconChevron size={14} />
                </div>
              </button>
            ))}
          </div>
        )}
      </section>

      <section className="card">
        <div className="card-head">
          <div>
            <h2>{t("Live state grid")}</h2>
            <p>{t("Human edits take sovereignty and block agent writes until unlocked.")}</p>
          </div>
          <span className="count-chip">{t("{count} slots", { count: formatNumber(slots.length) })}</span>
        </div>
        {slots.length === 0 ? (
          <Empty
            title={t("No state slots")}
            detail={t("Agents create slots with agent_patch_shared_state or the REST slot endpoint.")}
          />
        ) : (
          <div className="table-wrap">
            <table className="data-table slot-table">
              <thead>
                <tr>
                  <th>{t("Slot key")}</th>
                  <th>{t("Current state")}</th>
                  <th>{t("Version")}</th>
                  <th>{t("Governance")}</th>
                  <th>{t("Updated")}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {slots.map((slot) => (
                  <tr key={slot.id} className="clickable" onClick={() => onOpenSlot(slot)}>
                    <td data-label={t("Slot key")}>
                      <div className="cell-title">
                        {slot.slot_key}
                        {slot.tainted && <span className="pill rose slot-taint">{t("Tainted")}</span>}
                      </div>
                      <div className="cell-sub">{t("ns:")} {slot.namespace}</div>
                    </td>
                    <td data-label={t("Current state")}>
                      <code className="state-code" title={JSON.stringify(slot.current_state)}>{statePreview(slot.current_state)}</code>
                    </td>
                    <td data-label={t("Version")}>
                      <span className="version-chip">v{slot.version}</span>
                    </td>
                    <td data-label={t("Governance")}>
                      {slot.locked_by_human ? (
                        <span className="lock-badge human">
                          <IconLock /> {t("Human")}
                        </span>
                      ) : slot.lease_active ? (
                        <span className="lock-badge agent">
                          <IconPulse size={12} /> {slot.locked_by ?? "lease"}
                        </span>
                      ) : (
                        <span className="muted">{t("Open")}</span>
                      )}
                    </td>
                    <td data-label={t("Updated")}>
                      <div>{relativeTime(slot.updated_at)}</div>
                      <div className="cell-sub">{t("by")} {slot.updated_by}</div>
                    </td>
                    <td data-label={t("Actions")}>
                      <button
                        type="button"
                        className="row-action"
                        aria-label={`${t("Override")} ${slot.slot_key}`}
                        onClick={(event) => {
                          event.stopPropagation();
                          onOpenSlot(slot);
                        }}
                      >
                        {t("Override")}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card">
        <div className="card-head">
          <div>
            <h2>{t("Interrupts & governance")}</h2>
            <p>{t("Human overrides, escalations and batch rollbacks from the event bus.")}</p>
          </div>
          <span className="count-chip">{t("{count} recent", { count: formatNumber(interrupts.length) })}</span>
        </div>
        {interrupts.length === 0 ? (
          <Empty
            title={t("Quiet board")}
            detail={t("HUMAN_OVERRIDE and escalation signals will stream here.")}
          />
        ) : (
          <ul className="event-list">
            {interrupts.map((item) => (
              <li key={item.id} className="event-row">
                <span className={`event-type tone-${item.event_type === "TASK_ESCALATED" ? "amber" : item.event_type === "TASK_REJECTED" ? "rose" : "violet"}`}>
                  {t(item.event_type)}
                </span>
                <span className="event-actor">{item.agent_signature}</span>
                <span className="event-payload">
                  {JSON.stringify(item.payload).slice(0, 160)}
                </span>
                <span className="event-time">{relativeTime(item.created_at)}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
