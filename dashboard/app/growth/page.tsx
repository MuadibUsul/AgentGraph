"use client";

import { useCallback, useEffect, useState } from "react";

import { LanguageToggle, useI18n } from "../i18n";
import { BrandMark } from "../components/ui";
import { normalizeBase } from "../types";

const TOKEN_KEY = "agentgraph.admin_token";
const BASE_KEY = "agentgraph.api_base";

type Kpi = {
  value: number | null;
  target: number;
  numerator?: number;
  denominator?: number;
  median?: number;
  active_workspaces?: number;
  at_target?: number;
  viewed?: number;
};

type Growth = {
  generated_at: string;
  window_days: number;
  kpis: {
    activation_rate: Kpi;
    week2_retention: Kpi;
    decisions_per_active_workspace: Kpi;
    upgrade_intent_workspaces: Kpi;
  };
  signups: number;
  funnel: { step: string; workspaces: number }[];
  cli: { started: number; completed: number };
  cohorts: {
    week: string;
    signups: number;
    activated: number;
    activation_rate: number | null;
    retention_eligible: number;
    retained: number;
    week2_retention: number | null;
  }[];
  recent: {
    workspace_id: string;
    workspace_name: string;
    email: string;
    tier: string;
    signup_at: string;
    first_oriented_at: string | null;
    activated: boolean;
    retained_week2: boolean;
    agents: number;
    last_agent_activity: string | null;
    decisions_7d: number;
    upgrade_intent: boolean;
  }[];
};

const STEP_LABELS: Record<string, string> = {
  signed_up: "Signed up",
  token_created: "Created an agent key",
  agent_oriented: "Agent called get_orientation",
  human_decision: "Made a human decision",
  retained_week2: "Agents back in week 2",
};

function pct(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`;
}

function when(value: string | null, locale: string): string {
  if (!value) return "—";
  return new Date(value).toLocaleString(locale, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export default function GrowthPage() {
  const { t, lang } = useI18n();
  const locale = lang === "zh" ? "zh-CN" : "en-US";
  const [days, setDays] = useState(28);
  const [data, setData] = useState<Growth | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (span: number) => {
    const token = sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem(TOKEN_KEY);
    if (!token) {
      window.location.assign("/dashboard/connect/");
      return;
    }
    const base =
      localStorage.getItem(BASE_KEY) ||
      (window.location.port === "3000" ? "http://localhost:8000" : window.location.origin);
    setLoading(true);
    setError(null);
    try {
      const response = await fetch(`${normalizeBase(base)}/v1/admin/growth?days=${span}`, {
        headers: { Authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(30000),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body?.error?.message ?? `HTTP ${response.status}`);
      setData(body as Growth);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(days);
  }, [days, load]);

  const tiles = data
    ? [
        {
          id: "activation",
          label: t("Activation"),
          hint: t("Signups whose agent called get_orientation within 24 h"),
          value: pct(data.kpis.activation_rate.value),
          target: `≥ ${pct(data.kpis.activation_rate.target)}`,
          detail: `${data.kpis.activation_rate.numerator ?? 0} / ${data.kpis.activation_rate.denominator ?? 0}`,
          ok: data.kpis.activation_rate.value,
          goal: data.kpis.activation_rate.target,
        },
        {
          id: "retention",
          label: t("Week-2 retention"),
          hint: t("Activated workspaces whose agents came back 7–14 days later"),
          value: pct(data.kpis.week2_retention.value),
          target: `≥ ${pct(data.kpis.week2_retention.target)}`,
          detail: `${data.kpis.week2_retention.numerator ?? 0} / ${data.kpis.week2_retention.denominator ?? 0}`,
          ok: data.kpis.week2_retention.value,
          goal: data.kpis.week2_retention.target,
        },
        {
          id: "decisions",
          label: t("Human decisions"),
          hint: t("Per active workspace, last 7 days (approve, reject, verify, undo)"),
          value:
            data.kpis.decisions_per_active_workspace.value === null
              ? "—"
              : String(data.kpis.decisions_per_active_workspace.value),
          target: `≥ ${data.kpis.decisions_per_active_workspace.target}`,
          detail: t("{active} active · median {median}", {
            active: data.kpis.decisions_per_active_workspace.active_workspaces ?? 0,
            median: data.kpis.decisions_per_active_workspace.median ?? 0,
          }),
          ok: data.kpis.decisions_per_active_workspace.value,
          goal: data.kpis.decisions_per_active_workspace.target,
        },
        {
          id: "upgrade",
          label: t("Upgrade intent"),
          hint: t("Workspaces that clicked Upgrade in this window"),
          value: String(data.kpis.upgrade_intent_workspaces.value ?? 0),
          target: `≥ ${data.kpis.upgrade_intent_workspaces.target}`,
          detail: t("{viewed} opened the dialog", { viewed: data.kpis.upgrade_intent_workspaces.viewed ?? 0 }),
          ok: data.kpis.upgrade_intent_workspaces.value,
          goal: data.kpis.upgrade_intent_workspaces.target,
        },
      ]
    : [];
  const top = data?.funnel[0]?.workspaces || 0;

  return (
    <div className="growth-page">
      <header className="growth-header">
        <a className="auth-brand" href="/dashboard/">
          <span className="auth-brand-mark"><BrandMark size={34} /></span>
          <span>
            <strong>Veynod<span className="accent">.</span></strong>
            <small>{t("Growth")}</small>
          </span>
        </a>
        <div className="growth-actions">
          <div className="segmented" role="group" aria-label={t("Window")}>
            {[7, 28, 90].map((value) => (
              <button
                key={value}
                type="button"
                className={value === days ? "active" : ""}
                onClick={() => setDays(value)}
              >
                {t("{days} days", { days: value })}
              </button>
            ))}
          </div>
          <LanguageToggle />
          <a className="auth-operator-link" href="/dashboard/">{t("Back to console")} →</a>
        </div>
      </header>

      <main className="growth-main">
        <div className="growth-title">
          <h1>{t("Is anyone getting value?")}</h1>
          <p>
            {t("Four numbers from the six-week plan. Self-serve signups only; accounts in METRICS_INTERNAL_EMAILS are left out.")}
          </p>
        </div>
        {error && <div className="inline-error" role="alert">{error}</div>}
        {!data && loading && <p className="growth-muted">{t("Loading…")}</p>}

        {data && (
          <>
            <section className="growth-kpis">
              {tiles.map((tile) => {
                const state =
                  tile.ok === null || tile.ok === undefined
                    ? "none"
                    : tile.ok >= tile.goal
                      ? "good"
                      : "below";
                return (
                  <article key={tile.id} className={`growth-kpi state-${state}`}>
                    <div className="growth-kpi-top">
                      <span>{tile.label}</span>
                      <span className="growth-pill">
                        {state === "none" ? t("Not enough data") : state === "good" ? t("On target") : t("Below target")}
                      </span>
                    </div>
                    <div className="growth-kpi-value">{tile.value}</div>
                    <div className="growth-kpi-foot">
                      <span>{t("Target")} {tile.target}</span>
                      <span>{tile.detail}</span>
                    </div>
                    <p>{tile.hint}</p>
                  </article>
                );
              })}
            </section>

            <section className="growth-grid">
              <article className="growth-panel">
                <h2>{t("Funnel")}</h2>
                <p className="growth-muted">
                  {t("Signups in the last {days} days: {count}", { days: data.window_days, count: data.signups })}
                </p>
                <ol className="growth-funnel">
                  {data.funnel.map((step) => {
                    const share = top ? step.workspaces / top : 0;
                    return (
                      <li key={step.step}>
                        <div className="growth-funnel-label">
                          <span>{t(STEP_LABELS[step.step] ?? step.step)}</span>
                          <span>{step.workspaces} · {top ? pct(share) : "—"}</span>
                        </div>
                        <div className="growth-bar"><i style={{ width: `${Math.max(share * 100, step.workspaces ? 2 : 0)}%` }} /></div>
                      </li>
                    );
                  })}
                </ol>
                <p className="growth-muted">
                  {t("npx veynod init: {started} started, {completed} finished", data.cli)}
                </p>
              </article>

              <article className="growth-panel">
                <h2>{t("Weekly cohorts")}</h2>
                <table className="growth-table">
                  <thead>
                    <tr>
                      <th>{t("Week of")}</th>
                      <th>{t("Signups")}</th>
                      <th>{t("Activated")}</th>
                      <th>{t("Week 2")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.cohorts.length === 0 && (
                      <tr><td colSpan={4} className="growth-muted">{t("No signups yet.")}</td></tr>
                    )}
                    {data.cohorts.map((row) => (
                      <tr key={row.week}>
                        <td>{row.week}</td>
                        <td>{row.signups}</td>
                        <td>{pct(row.activation_rate)} <small>({row.activated})</small></td>
                        <td>
                          {row.retention_eligible ? pct(row.week2_retention) : t("too early")}
                          {row.retention_eligible > 0 && <small> ({row.retained}/{row.retention_eligible})</small>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </article>
            </section>

            <section className="growth-panel">
              <h2>{t("Recent signups")}</h2>
              <p className="growth-muted">{t("Who to talk to this week: activated but quiet, or quiet from the start.")}</p>
              <div className="growth-scroll">
                <table className="growth-table">
                  <thead>
                    <tr>
                      <th>{t("Workspace")}</th>
                      <th>{t("Owner")}</th>
                      <th>{t("Signed up")}</th>
                      <th>{t("Agents")}</th>
                      <th>{t("Activated")}</th>
                      <th>{t("Last agent call")}</th>
                      <th>{t("Decisions 7d")}</th>
                      <th>{t("Week 2")}</th>
                      <th>{t("Upgrade")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.recent.length === 0 && (
                      <tr><td colSpan={9} className="growth-muted">{t("No signups yet.")}</td></tr>
                    )}
                    {data.recent.map((row) => (
                      <tr key={row.workspace_id}>
                        <td>{row.workspace_name}</td>
                        <td className="mono">{row.email}</td>
                        <td>{when(row.signup_at, locale)}</td>
                        <td>{row.agents}</td>
                        <td>{row.activated ? "✓" : row.first_oriented_at ? t("late") : "—"}</td>
                        <td>{when(row.last_agent_activity, locale)}</td>
                        <td>{row.decisions_7d}</td>
                        <td>{row.retained_week2 ? "✓" : "—"}</td>
                        <td>{row.upgrade_intent ? "★" : ""}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
            <p className="growth-muted">
              {t("Updated {time}", { time: when(data.generated_at, locale) })}
            </p>
          </>
        )}
      </main>
    </div>
  );
}
