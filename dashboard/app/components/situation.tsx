"use client";

import { useCallback, useEffect, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";
import { relativeTime } from "../types";
import { Empty, IconPulse, IconRefresh, Spinner } from "./ui";

type SituationAlert = {
  alert_id: string;
  rule: string;
  severity: "high" | "medium" | "low";
  subject: { kind: string; ref: string };
  state: "open" | "acked" | "resolved";
  details: Record<string, unknown>;
  first_seen: string;
  last_seen: string;
  notify_count: number;
  acked_by: string | null;
  note: string | null;
};

const RULE_LABELS: Record<string, string> = {
  retry_storm: "Retry storm",
  idle_spin: "Idle loop",
  unclaimed_task: "Unclaimed task",
  repeated_orphan: "Repeatedly orphaned task",
  conflict_anomaly: "Conflict hotspot",
  cost_anomaly: "Cost anomaly",
  index_backlog: "Index backlog",
  taint_spread: "Taint spreading",
};

export function SituationView({
  workspaceId,
  request,
  notify,
}: {
  workspaceId: string;
  request: ApiRequest;
  notify: (message: string) => void;
}) {
  const { t } = useI18n();
  const [state, setState] = useState<"open" | "acked" | "resolved">("open");
  const [alerts, setAlerts] = useState<SituationAlert[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [drill, setDrill] = useState<{ alert: SituationAlert; data: unknown } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await request<{ items: SituationAlert[] }>(
        `/v1/admin/workspaces/${workspaceId}/alerts?state=${state}`,
      );
      setAlerts(result.items);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Unable to load alerts."));
    } finally {
      setLoading(false);
    }
  }, [request, workspaceId, state, t]);

  useEffect(() => {
    void load();
  }, [load]);

  async function open(alert: SituationAlert) {
    const query =
      alert.subject.kind === "task"
        ? `task=${encodeURIComponent(alert.subject.ref)}`
        : alert.subject.kind === "document"
          ? `doc=${encodeURIComponent(alert.subject.ref)}`
          : alert.subject.kind === "principal"
            ? `principal=${encodeURIComponent(alert.subject.ref)}`
            : null;
    if (!query) {
      setDrill({ alert, data: alert.details });
      return;
    }
    setBusy(alert.alert_id);
    try {
      const data = await request<unknown>(
        `/v1/admin/workspaces/${workspaceId}/metrics?${query}&window=24h`,
      );
      setDrill({ alert, data });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Unable to load metrics."));
    } finally {
      setBusy(null);
    }
  }

  async function ack(alert: SituationAlert) {
    setBusy(alert.alert_id);
    try {
      await request(`/v1/admin/workspaces/${workspaceId}/alerts/${alert.alert_id}/ack`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      notify(t("Alert acknowledged."));
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Unable to acknowledge the alert."));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="tab-stack">
      <section className="card">
        <div className="card-head">
          <div>
            <h2>{t("Situation")}</h2>
            <p>
              {t(
                "Anomalies across agents, tasks and documents, checked every minute. High severity alerts also notify the owners.",
              )}
            </p>
          </div>
          <div className="segmented" role="group" aria-label={t("Alert state")}>
            {(["open", "acked", "resolved"] as const).map((value) => (
              <button
                key={value}
                type="button"
                className={`button ghost small ${state === value ? "active" : ""}`}
                aria-pressed={state === value}
                onClick={() => setState(value)}
              >
                {t(value === "open" ? "Open" : value === "acked" ? "Acknowledged" : "Resolved")}
              </button>
            ))}
            <button type="button" className="button ghost small" onClick={() => void load()}>
              {loading ? <Spinner /> : <IconRefresh />} {t("Refresh")}
            </button>
          </div>
        </div>
        {error && <div className="inline-error">{error}</div>}
        {alerts.length === 0 ? (
          <Empty
            title={t("Nothing needs attention")}
            detail={t("Retry storms, idle loops, unclaimed tasks and cost spikes will show here.")}
          />
        ) : (
          <div className="diff-list">
            {alerts.map((alert) => (
              <div key={alert.alert_id} className="diff-row">
                <div className="batch-top">
                  <span className={`status-pill tone-${alert.severity === "high" ? "rose" : alert.severity === "medium" ? "amber" : "slate"}`}>
                    {t(alert.severity === "high" ? "High" : alert.severity === "medium" ? "Medium" : "Low")}
                  </span>
                  <strong>{t(RULE_LABELS[alert.rule] ?? alert.rule)}</strong>
                  <span className="mono muted">
                    {alert.subject.kind}: {alert.subject.ref}
                  </span>
                  <span className="diff-time">{relativeTime(alert.last_seen)}</span>
                </div>
                <div className="batch-foot">
                  <button
                    type="button"
                    className="button ghost small"
                    onClick={() => void open(alert)}
                    disabled={busy === alert.alert_id}
                  >
                    {busy === alert.alert_id ? <Spinner /> : <IconPulse size={13} />} {t("Details")}
                  </button>
                  {alert.state === "open" && (
                    <button
                      type="button"
                      className="button ghost small"
                      onClick={() => void ack(alert)}
                      disabled={busy === alert.alert_id}
                    >
                      {t("Acknowledge")}
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
      {drill && (
        <section className="card">
          <div className="card-head">
            <div>
              <h2>{t(RULE_LABELS[drill.alert.rule] ?? drill.alert.rule)}</h2>
              <p className="mono">{drill.alert.subject.ref}</p>
            </div>
            <button type="button" className="button ghost small" onClick={() => setDrill(null)}>
              {t("Close")}
            </button>
          </div>
          <pre className="json-block">{JSON.stringify({ details: drill.alert.details, metrics: drill.data }, null, 2)}</pre>
        </section>
      )}
    </div>
  );
}
