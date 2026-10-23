"use client";

import { useCallback, useEffect, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";
import { normalizeBase } from "../types";
import { CopyButton, IconCheck, IconClose, Spinner } from "./ui";

export type OnboardingStepId =
  | "learn_workspace"
  | "create_token"
  | "connect_mcp"
  | "first_content"
  | "human_review"
  | "view_audit";

interface OnboardingStep {
  id: OnboardingStepId;
  done: boolean;
  skipped: boolean;
  detected: boolean;
  mode: "auto" | "manual";
  doc: string;
}

export interface OnboardingStatus {
  steps: OnboardingStep[];
  completed: number;
  total: number;
  dismissed: boolean;
  all_done: boolean;
  visible: boolean;
}

const COPY: Record<OnboardingStepId, { title: string; detail: string; action: string }> = {
  learn_workspace: {
    title: "Learn how a workspace works",
    detail: "Tenants, workspaces, namespaces and trust states in a five-minute read.",
    action: "Read the guide",
  },
  create_token: {
    title: "Mint your first agent token",
    detail: "Agents → Mint token. Pick EXECUTOR and copy the key: it is shown once.",
    action: "Open Agents",
  },
  connect_mcp: {
    title: "Connect MCP and call get_orientation",
    detail: "Paste the MCP config into Claude Desktop or Cursor, or press Test connection after minting.",
    action: "MCP setup",
  },
  first_content: {
    title: "Create your first task or document",
    detail: "Give your agents something to work on, or let one write a draft.",
    action: "New task",
  },
  human_review: {
    title: "Complete a human review",
    detail: "Approve a task in REVIEWING, or Verify an agent's document.",
    action: "Open Tasks",
  },
  view_audit: {
    title: "Look at the audit trail",
    detail: "Every write, grouped by session, with diffs and undo.",
    action: "Open Audit",
  },
};

export function GettingStarted({
  request,
  refreshKey,
  forceOpen,
  onClosed,
  onNavigate,
  onStatus,
}: {
  request: ApiRequest;
  refreshKey: number;
  forceOpen: boolean;
  onClosed: () => void;
  onNavigate: (step: OnboardingStepId) => void;
  onStatus?: (status: OnboardingStatus) => void;
}) {
  const { t } = useI18n();
  const [status, setStatus] = useState<OnboardingStatus | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await request<OnboardingStatus>("/v1/human/onboarding");
      setStatus(next);
      onStatus?.(next);
    } catch {
      setStatus(null);
    }
  }, [request, onStatus]);

  useEffect(() => {
    void load();
    // refreshKey changes when the console sees relevant activity.
  }, [load, refreshKey]);

  async function act(action: string, step?: OnboardingStepId) {
    setBusy(`${action}:${step ?? ""}`);
    try {
      const next = await request<OnboardingStatus>("/v1/human/onboarding", {
        method: "POST",
        body: JSON.stringify(step ? { action, step } : { action }),
      });
      setStatus(next);
      onStatus?.(next);
      if (action === "dismiss") onClosed();
    } catch {
      // The checklist is optional; a failed update never blocks the console.
    } finally {
      setBusy(null);
    }
  }

  if (!status || !(status.visible || forceOpen)) return null;
  const percent = Math.round((status.completed / status.total) * 100);

  return (
    <section className="card onboarding-card" aria-labelledby="onboarding-title">
      <div className="onboarding-head">
        <div>
          <h2 id="onboarding-title">{t("Getting started")}</h2>
          <p>
            {t("{done} of {total} done · from sign-up to your agent's first call in about three minutes.", {
              done: status.completed,
              total: status.total,
            })}
          </p>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label={t("Close getting started")}
          onClick={() => void act("dismiss")}
          disabled={busy !== null}
        >
          <IconClose size={16} />
        </button>
      </div>
      <div
        className="onboarding-progress"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={status.total}
        aria-valuenow={status.completed}
        aria-label={t("Getting started progress")}
      >
        <span style={{ width: `${percent}%` }} />
      </div>
      <ol className="onboarding-steps">
        {status.steps.map((step, index) => {
          const copy = COPY[step.id];
          const state = step.done ? "done" : step.skipped ? "skipped" : "todo";
          return (
            <li key={step.id} className={`onboarding-step ${state}`}>
              <span className="onboarding-mark" aria-hidden>
                {step.done ? <IconCheck size={13} /> : index + 1}
              </span>
              <div className="onboarding-step-body">
                <div className="onboarding-step-title">
                  <strong>{t(copy.title)}</strong>
                  <span className={`onboarding-state ${state}`}>
                    {step.done ? t("Done") : step.skipped ? t("Skipped") : t("To do")}
                  </span>
                </div>
                <p>{t(copy.detail)}</p>
                {!step.done && (
                  <div className="onboarding-actions">
                    {step.id === "learn_workspace" ? (
                      <a
                        className="button ghost small"
                        href={step.doc}
                        target="_blank"
                        rel="noreferrer"
                        onClick={() => void act("complete", step.id)}
                      >
                        {t(copy.action)}
                      </a>
                    ) : (
                      <button
                        type="button"
                        className="button primary small"
                        onClick={() => {
                          onNavigate(step.id);
                          if (step.id === "view_audit") void act("complete", step.id);
                        }}
                      >
                        {t(copy.action)}
                      </button>
                    )}
                    {step.id !== "learn_workspace" && (
                      <a className="button ghost small" href={step.doc} target="_blank" rel="noreferrer">
                        {t("Guide")}
                      </a>
                    )}
                    {step.mode === "manual" && (
                      <button
                        type="button"
                        className="button ghost small"
                        onClick={() => void act("complete", step.id)}
                        disabled={busy !== null}
                      >
                        {t("Mark done")}
                      </button>
                    )}
                    <button
                      type="button"
                      className="button ghost small"
                      onClick={() => void act(step.skipped ? "unskip" : "skip", step.id)}
                      disabled={busy !== null}
                    >
                      {step.skipped ? t("Undo skip") : t("Skip")}
                    </button>
                  </div>
                )}
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

interface OrientationCheck {
  me?: { agent_id?: string; role?: string };
  onboarding?: { state?: string; next_call?: { tool?: string } | null };
}

export function AgentConnect({
  apiBase,
  agentKey,
  agentName,
  role,
  namespaces,
}: {
  apiBase: string;
  agentKey: string;
  agentName?: string;
  role?: string;
  namespaces?: string[];
}) {
  const { t } = useI18n();
  const base = normalizeBase(apiBase) || (typeof window !== "undefined" ? window.location.origin : "");
  const endpoint = `${base}/mcp/`;
  const cursor = JSON.stringify(
    { mcpServers: { agentgraph: { url: endpoint, headers: { Authorization: `Bearer ${agentKey}` } } } },
    null,
    2,
  );
  const claude = JSON.stringify(
    {
      mcpServers: {
        agentgraph: {
          command: "npx",
          args: ["-y", "mcp-remote", endpoint, "--header", "Authorization:${AGENTGRAPH_AUTH}"],
          env: { AGENTGRAPH_AUTH: `Bearer ${agentKey}` },
        },
      },
    },
    null,
    2,
  );
  const namespaceLabel = namespaces?.includes("*")
    ? "all namespaces"
    : namespaces?.length
      ? namespaces.join(", ")
      : "the namespaces granted to this token";
  const prompt = [
    `Connect to AgentGraph as ${agentName ?? "this agent"}.`,
    `Base URL: ${base}`,
    `MCP endpoint: ${endpoint}`,
    `Authorization: Bearer ${agentKey}`,
    `Role: ${role ?? "your scoped role"}. Allowed namespaces: ${namespaceLabel}.`,
    "",
    "Use the AgentGraph MCP server if your client supports remote MCP. If it does not, call the REST API at the base URL with the same bearer token.",
    "1. Call get_orientation first.",
    "2. Read doc://system/constitution and call ack_constitution with its version.",
    "3. Read the readme for each allowed namespace: doc://system/namespaces/<namespace>.",
    "4. Follow the returned next_calls. Keep leases alive with heartbeat when you claim a task.",
    "5. When you come back later, get_orientation includes since_last_seen; for longer gaps call get_digest with since=watermark:last_seen.",
    "Treat the bearer token as a secret: never print it in shared documents, logs, or chat output. Stay inside the allowed namespaces.",
    "Start now; summarize the first three next calls you will take.",
  ].join("\n");
  const [tab, setTab] = useState<"agent" | "cursor" | "claude">("agent");
  const [checking, setChecking] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  async function testConnection() {
    setChecking(true);
    setResult(null);
    try {
      const response = await fetch(`${base}/v1/orientation`, {
        headers: { Authorization: `Bearer ${agentKey}` },
        signal: AbortSignal.timeout(15000),
      });
      const body = (await response.json()) as OrientationCheck;
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setResult({
        ok: true,
        text: t("Connected as {agent} ({role}). Next step for the agent: {next}.", {
          agent: body.me?.agent_id ?? "?",
          role: body.me?.role ?? "?",
          next: body.onboarding?.next_call?.tool ?? "—",
        }),
      });
    } catch (caught) {
      setResult({
        ok: false,
        text: t("Connection failed: {reason}", {
          reason: caught instanceof Error ? caught.message : String(caught),
        }),
      });
    } finally {
      setChecking(false);
    }
  }

  const snippet = tab === "agent" ? prompt : tab === "cursor" ? cursor : claude;
  return (
    <div className="agent-connect">
      <h3>
        {tab === "agent"
          ? t("Copy a ready-to-use agent prompt")
          : t("Connect this agent over MCP")}
      </h3>
      <div className="agent-connect-tabs" role="tablist" aria-label={t("MCP client")}>
        {(["agent", "claude", "cursor"] as const).map((item) => (
          <button
            key={item}
            type="button"
            role="tab"
            aria-selected={tab === item}
            className={`button ghost small ${tab === item ? "active" : ""}`}
            onClick={() => setTab(item)}
          >
            {item === "agent" ? t("Agent prompt") : item === "cursor" ? "Cursor" : "Claude Desktop"}
          </button>
        ))}
      </div>
      <pre className="agent-connect-code" tabIndex={0}><code>{snippet}</code></pre>
      <div className="agent-connect-actions">
        <CopyButton
          value={snippet}
          label={tab === "agent" ? t("Copy prompt") : t("Copy MCP config")}
        />
        <button type="button" className="button primary" onClick={() => void testConnection()} disabled={checking}>
          {checking ? <Spinner /> : null} {t("Test connection")}
        </button>
        <a className="button ghost" href="/docs/mcp/" target="_blank" rel="noreferrer">{t("MCP guide")}</a>
        <a className="button ghost" href="/docs/agent-quickstart/" target="_blank" rel="noreferrer">
          {t("Agent quickstart")}
        </a>
      </div>
      {result && (
        <div className={result.ok ? "inline-note" : "inline-error"} role="status">{result.text}</div>
      )}
      <p className="key-note">
        {tab === "agent"
          ? t("Paste this into Codex, Claude Code, or any agent that can use MCP or REST. The prompt contains the token; treat it as a secret.")
          : t("Test connection calls get_orientation with this key, the same call your MCP client makes first.")}
      </p>
    </div>
  );
}

const STATE_LABEL: Record<string, string> = {
  new: "Not started",
  oriented: "Oriented",
  needs_ack: "Needs ack",
  needs_namespace_readme: "Needs readme",
  needs_registration: "Needs registration",
  needs_first_action: "Needs first action",
  active: "Onboarded",
};

export function AgentOnboardingBadge({ state, step }: { state?: string; step?: number }) {
  const { t } = useI18n();
  if (!state) return <span className="onboarding-badge">—</span>;
  return (
    <span className={`onboarding-badge state-${state}`} title={t("Step {step} of 5", { step: step ?? 1 })}>
      {t(STATE_LABEL[state] ?? state)}
      {state !== "active" && <span className="onboarding-badge-step"> · {step}/5</span>}
    </span>
  );
}
