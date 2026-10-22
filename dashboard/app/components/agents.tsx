"use client";

import { useCallback, useEffect, useState } from "react";

import { useI18n } from "../i18n";
import type { AgentToken, ApiRequest } from "../types";
import { relativeTime } from "../types";
import { AgentOnboardingBadge } from "./onboarding";
import {
  Empty,
  IconAgents,
  IconKey,
  IconLock,
  IconPlus,
  Modal,
  RolePill,
  Spinner,
} from "./ui";

interface AgentOnboardingOverview {
  items: Array<{ agent_signature: string; state: string; step: number }>;
  summary: {
    tokens: number;
    by_state: Record<string, number>;
    orientation_rate: number;
    constitution_ack_rate: number;
  };
}

const ROLES = [
  {
    role: "SCOUT",
    text: "Append-only collector. Raw namespaces only; no state reads/writes.",
  },
  {
    role: "ANALYST",
    text: "Reads everything, writes documents and proposes tasks. No state writes.",
  },
  {
    role: "AUDITOR",
    text: "Reviews tasks, reads audit, can write review notes. No state writes.",
  },
  {
    role: "EXECUTOR",
    text: "Claims tasks and owns state writes. Human locks still override.",
  },
];

export function AgentsView({
  workspaceId,
  tokens,
  request,
  onRefresh,
  onKey,
  notify,
  openCreateSignal = 0,
}: {
  workspaceId: string;
  tokens: AgentToken[];
  request: ApiRequest;
  onRefresh: () => Promise<void>;
  onKey: (key: string, name: string) => void;
  notify: (message: string) => void;
  openCreateSignal?: number;
}) {
  const { t } = useI18n();
  const [showCreate, setShowCreate] = useState(false);
  const [progress, setProgress] = useState<AgentOnboardingOverview | null>(null);

  const loadProgress = useCallback(async () => {
    try {
      setProgress(
        await request<AgentOnboardingOverview>(
          `/v1/admin/workspaces/${workspaceId}/agent-onboarding`,
        ),
      );
    } catch {
      setProgress(null);
    }
  }, [request, workspaceId]);

  useEffect(() => {
    void loadProgress();
  }, [loadProgress, tokens.length]);

  useEffect(() => {
    if (openCreateSignal > 0) setShowCreate(true);
  }, [openCreateSignal]);

  const byAgent = new Map(
    (progress?.items ?? []).map((item) => [item.agent_signature, item] as const),
  );
  const [signature, setSignature] = useState("");
  const [role, setRole] = useState("EXECUTOR");
  const [namespaces, setNamespaces] = useState("*");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function create(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await request<{ token: AgentToken; api_key: string }>(
        `/v1/admin/workspaces/${workspaceId}/agent-tokens`,
        {
          method: "POST",
          body: JSON.stringify({
            agent_signature: signature.trim(),
            role_scope: role,
            allowed_namespaces: namespaces
              .split(",")
              .map((item) => item.trim())
              .filter(Boolean),
          }),
        },
      );
      setShowCreate(false);
      setSignature("");
      onKey(result.api_key, result.token.agent_signature);
      await onRefresh();
      notify(t("Agent token for {name} minted.", { name: result.token.agent_signature }));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Unable to create agent token"));
    } finally {
      setBusy(false);
    }
  }

  async function toggle(token: AgentToken) {
    setBusy(true);
    setError(null);
    try {
      await request(
        `/v1/admin/workspaces/${workspaceId}/agent-tokens/${token.id}`,
        {
          method: "PATCH",
          body: JSON.stringify({ is_active: !token.is_active }),
        },
      );
      await onRefresh();
      notify(t("{name} {state}.", { name: token.agent_signature, state: token.is_active ? t("disabled") : t("enabled") }));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Unable to update token"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="tab-stack">
      <div className="role-grid">
        {ROLES.map((item) => (
          <div className="role-card" key={item.role}>
            <div className="role-card-head">
              <RolePill role={item.role} />
              {item.role === "SCOUT" && <IconLock size={13} />}
            </div>
            <p>{t(item.text)}</p>
          </div>
        ))}
      </div>

      <section className="card">
        <div className="card-head">
          <div>
            <h2>{t("Scoped agent tokens")}</h2>
            <p>{t("Each token pins one agent signature, a role and its namespaces.")}</p>
          </div>
          <button type="button" className="button primary" onClick={() => setShowCreate(true)}>
            <IconPlus /> {t("Mint token")}
          </button>
        </div>
        {error && <div className="inline-error">{error}</div>}
        {progress && tokens.length > 0 && (
          <p className="onboarding-summary">
            {t("Onboarded {done} of {total} agents · orientation {orient}% · constitution acknowledged {ack}%", {
              done: progress.summary.by_state.active ?? 0,
              total: progress.summary.tokens,
              orient: Math.round(progress.summary.orientation_rate * 100),
              ack: Math.round(progress.summary.constitution_ack_rate * 100),
            })}{" "}
            <a href="/docs/agent-quickstart/" target="_blank" rel="noreferrer">{t("What are the steps?")}</a>
          </p>
        )}
        {tokens.length === 0 ? (
          <Empty
            title={t("No agent tokens")}
            detail={t("Mint a token for each agent runtime before connecting it to the API or MCP.")}
            action={
              <div className="empty-actions">
                <button type="button" className="button primary small" onClick={() => setShowCreate(true)}>
                  <IconPlus /> {t("Mint token")}
                </button>
                <a className="button ghost small" href="/docs/mcp/" target="_blank" rel="noreferrer">
                  {t("MCP setup guide")}
                </a>
              </div>
            }
          />
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t("Agent")}</th>
                  <th>{t("Role")}</th>
                  <th>{t("Namespaces")}</th>
                  <th>{t("Status")}</th>
                  <th>{t("Onboarding")}</th>
                  <th>{t("Created")}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {tokens.map((token) => (
                  <tr key={token.id}>
                    <td>
                      <div className="agent-cell">
                        <span className="agent-avatar">
                          <IconAgents size={14} />
                        </span>
                        <span title={token.agent_signature}>{token.agent_signature}</span>
                      </div>
                    </td>
                    <td><RolePill role={token.role_scope} /></td>
                    <td>
                      <div className="namespace-chips">
                        {token.allowed_namespaces.map((namespace) => (
                          <span key={namespace} className="namespace-chip" title={namespace}>{namespace}</span>
                        ))}
                      </div>
                    </td>
                    <td>
                      <span className={`status-dot-inline ${token.is_active ? "live" : "dead"}`} />
                      {token.is_active ? t("Active") : t("Disabled")}
                    </td>
                    <td>
                      <AgentOnboardingBadge
                        state={byAgent.get(token.agent_signature)?.state}
                        step={byAgent.get(token.agent_signature)?.step}
                      />
                    </td>
                    <td>{relativeTime(token.created_at)}</td>
                    <td>
                      <button
                        type="button"
                        className="button ghost small"
                        disabled={busy}
                        onClick={() => void toggle(token)}
                      >
                        {token.is_active ? t("Disable") : t("Enable")}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {showCreate && (
        <Modal
          title={t("Mint an agent token")}
          subtitle={t("The raw key is shown exactly once. Store it in the agent runtime.")}
          onClose={() => setShowCreate(false)}
          footer={
            <>
              <button type="button" className="button ghost" onClick={() => setShowCreate(false)}>
                {t("Cancel")}
              </button>
              <button
                type="submit"
                form="agent-token-form"
                className="button primary"
                disabled={busy || !signature.trim()}
              >
                {busy ? <Spinner /> : <IconKey />} {t("Mint token")}
              </button>
            </>
          }
        >
          <form id="agent-token-form" onSubmit={create}>
            <label className="field">
              <span>{t("Agent signature")}</span>
              <input
                value={signature}
                onChange={(e) => setSignature(e.target.value)}
                placeholder="grid_executor_01"
                autoFocus
                required
              />
            </label>
            <label className="field">
              <span>{t("Role scope")}</span>
              <select value={role} onChange={(e) => setRole(e.target.value)}>
                {ROLES.map((item) => (
                  <option key={item.role} value={item.role}>{t(item.role)}</option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>{t("Allowed namespaces (comma separated, * for all)")}</span>
              <input value={namespaces} onChange={(e) => setNamespaces(e.target.value)} />
            </label>
            <div className="inline-note">
              {t("After minting you can copy a ready MCP config and test the connection.")}{" "}
              <a href="/docs/auth/" target="_blank" rel="noreferrer">{t("Tokens and roles")}</a>
            </div>
            {role === "SCOUT" && (
              <div className="inline-note">
                {t("Scout tokens should use a restricted namespace such as")} <code>raw_inbox</code>.
              </div>
            )}
            {error && <div className="inline-error">{error}</div>}
          </form>
        </Modal>
      )}
    </div>
  );
}
