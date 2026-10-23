"use client";

import { useEffect, useState } from "react";

import { LanguageToggle, useI18n } from "../i18n";
import { BrandMark } from "../components/ui";
import { normalizeBase } from "../types";

const SESSION_KEY = "agentgraph.session_token";
const BASE_KEY = "agentgraph.api_base";
const RETURN_KEY = "agentgraph.return_to";

type CliView = {
  user_code: string;
  status: "pending" | "approved" | "denied" | "consumed" | "expired";
  client_label: string;
  clients: string[];
  created_at: string;
  expires_at: string;
  agent_signature: string | null;
  workspace: { id: string; name: string };
  can_approve: boolean;
};

type Outcome = { kind: "approved"; agent: string; workspace: string } | { kind: "denied" };

const ROLES = ["EXECUTOR", "ANALYST", "SCOUT", "AUDITOR"] as const;

function formatCode(value: string): string {
  const clean = value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 8);
  return clean.length > 4 ? `${clean.slice(0, 4)}-${clean.slice(4)}` : clean;
}

export default function CliApprovePage() {
  const { t } = useI18n();
  const [apiBase, setApiBase] = useState("");
  const [session, setSession] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [view, setView] = useState<CliView | null>(null);
  const [agent, setAgent] = useState("claude-code");
  const [role, setRole] = useState<(typeof ROLES)[number]>("EXECUTOR");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);

  useEffect(() => {
    const base =
      localStorage.getItem(BASE_KEY) ||
      (window.location.port === "3000" ? "http://localhost:8000" : window.location.origin);
    setApiBase(base);
    const token = localStorage.getItem(SESSION_KEY);
    if (!token) {
      try {
        sessionStorage.setItem(RETURN_KEY, window.location.pathname + window.location.search);
      } catch {
        // The owner signs in and opens the link from the terminal again.
      }
      window.location.assign("/dashboard/login/");
      return;
    }
    setSession(token);
    const fromUrl = new URLSearchParams(window.location.search).get("code");
    if (fromUrl) {
      setCode(formatCode(fromUrl));
      void lookup(base, token, fromUrl);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function call(path: string, init: RequestInit = {}, base = apiBase, token = session) {
    const response = await fetch(`${normalizeBase(base)}${path}`, {
      ...init,
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        ...(init.headers || {}),
      },
      signal: AbortSignal.timeout(20000),
    });
    if (response.status === 401) {
      localStorage.removeItem(SESSION_KEY);
      try {
        sessionStorage.setItem(RETURN_KEY, window.location.pathname + window.location.search);
      } catch {
        // ignore
      }
      window.location.assign("/dashboard/login/");
      throw new Error(t("Your session expired. Sign in again."));
    }
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const errorCode = body?.error?.code as string | undefined;
      if (errorCode === "not_found") throw new Error(t("This code is unknown. Check it against your terminal."));
      if (errorCode === "cli_session_expired") throw new Error(t("This code expired. Run npx veynod init again."));
      throw new Error(body?.error?.message ?? t("Something went wrong."));
    }
    return body;
  }

  async function lookup(base = apiBase, token = session, value = code) {
    setError(null);
    setView(null);
    try {
      const found = (await call(
        `/v1/auth/cli/sessions/${encodeURIComponent(formatCode(value))}`, {}, base, token,
      )) as CliView;
      setView(found);
      const client = found.clients.find((item) => item !== "other");
      setAgent(client === "cursor" ? "cursor" : client === "claude-desktop" ? "claude-desktop" : "claude-code");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }

  async function decide(action: "approve" | "deny") {
    if (!view) return;
    setBusy(true);
    setError(null);
    try {
      const body = await call(
        `/v1/auth/cli/sessions/${encodeURIComponent(view.user_code)}/${action}`,
        {
          method: "POST",
          body: action === "approve"
            ? JSON.stringify({ agent_signature: agent.trim() || "claude-code", role_scope: role })
            : undefined,
        },
      );
      setOutcome(
        action === "approve"
          ? { kind: "approved", agent: body.agent_signature, workspace: body.workspace?.name ?? "" }
          : { kind: "denied" },
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  const statusNote =
    view && view.status !== "pending"
      ? view.status === "expired"
        ? t("This code expired. Run npx veynod init again.")
        : t("This code was already used.")
      : null;

  return (
    <div className="auth-page auth-page-connect">
      <div className="auth-aurora" aria-hidden />
      <div className="auth-grid-layer" aria-hidden />
      <header className="auth-header">
        <a className="auth-brand" href="/dashboard/">
          <span className="auth-brand-mark"><BrandMark size={34} /></span>
          <span>
            <strong>Veynod<span className="accent">.</span></strong>
            <small>{t("Human · Agent Blackboard")}</small>
          </span>
        </a>
        <div className="auth-header-actions">
          <LanguageToggle />
          <a className="auth-operator-link" href="/dashboard/">
            {t("Back to console")} <span aria-hidden>→</span>
          </a>
        </div>
      </header>

      <main className="auth-connect-main">
        <section className="auth-connect-story">
          <div className="auth-kicker">
            <span className="status-dot live" />
            npx veynod init
          </div>
          <h1>{t("Connect a terminal")}</h1>
          <p>
            {t("A terminal on your computer asked to join this workspace as an agent. Approving gives it its own agent key; nothing else on the computer is touched.")}
          </p>
          <ul className="auth-connect-list">
            <li>{t("The key is sealed to that terminal: only it can open it.")}</li>
            <li>{t("You can pause or revoke the agent any time under Agents.")}</li>
            <li>{t("Only approve a code you just saw in your own terminal.")}</li>
          </ul>
        </section>

        <section className="auth-card-wrap">
          <div className="auth-card">
            {outcome?.kind === "approved" ? (
              <div className="cli-done">
                <div className="cli-done-mark" aria-hidden>✓</div>
                <h2>{t("Approved. Go back to your terminal.")}</h2>
                <p>
                  {t("{agent} joins {workspace}. The terminal finishes the setup and calls get_orientation.", {
                    agent: outcome.agent,
                    workspace: outcome.workspace,
                  })}
                </p>
                <a className="auth-submit" href="/dashboard/">
                  {t("Open the console")} <span aria-hidden>→</span>
                </a>
              </div>
            ) : outcome?.kind === "denied" ? (
              <div className="cli-done">
                <h2>{t("Refused.")}</h2>
                <p>{t("The terminal gets nothing. You can close this tab.")}</p>
              </div>
            ) : (
              <>
                <div className="auth-card-head">
                  <span className="auth-card-kicker">{t("Terminal sign-in")}</span>
                  <h2>{view ? t("Does this code match your terminal?") : t("Enter the code from your terminal")}</h2>
                </div>
                {!view ? (
                  <form
                    className="auth-form"
                    onSubmit={(event) => {
                      event.preventDefault();
                      void lookup();
                    }}
                  >
                    <label className="field">
                      <span>{t("Code")}</span>
                      <input
                        className="cli-code-input"
                        value={code}
                        onChange={(event) => setCode(formatCode(event.target.value))}
                        placeholder="XXXX-XXXX"
                        autoComplete="off"
                        spellCheck={false}
                        required
                      />
                    </label>
                    {error && <div className="inline-error" role="alert">{error}</div>}
                    <button type="submit" className="auth-submit" disabled={!session || code.length < 9}>
                      {t("Continue")} <span aria-hidden>→</span>
                    </button>
                  </form>
                ) : (
                  <div className="auth-form">
                    <div className="cli-code" aria-label={t("Code")}>{view.user_code}</div>
                    <dl className="cli-facts">
                      <div><dt>{t("Workspace")}</dt><dd>{view.workspace.name}</dd></div>
                      <div><dt>{t("Computer")}</dt><dd>{view.client_label || t("Unnamed terminal")}</dd></div>
                      {view.clients.length > 0 && (
                        <div><dt>{t("Clients")}</dt><dd>{view.clients.join(", ")}</dd></div>
                      )}
                    </dl>
                    {statusNote ? (
                      <div className="inline-error" role="alert">{statusNote}</div>
                    ) : !view.can_approve ? (
                      <div className="inline-error" role="alert">
                        {t("Only an owner or admin of this workspace can approve. Ask one to open this link.")}
                      </div>
                    ) : (
                      <>
                        <label className="field">
                          <span>{t("Agent name")}</span>
                          <input
                            value={agent}
                            onChange={(event) => setAgent(event.target.value)}
                            maxLength={100}
                            spellCheck={false}
                          />
                        </label>
                        <label className="field">
                          <span>{t("Role")}</span>
                          <select value={role} onChange={(event) => setRole(event.target.value as typeof role)}>
                            {ROLES.map((item) => (
                              <option key={item} value={item}>{item}</option>
                            ))}
                          </select>
                        </label>
                        {error && <div className="inline-error" role="alert">{error}</div>}
                        <button type="button" className="auth-submit" disabled={busy}
                          onClick={() => void decide("approve")}>
                          {busy ? t("Approving…") : t("Approve this terminal")}
                          <span aria-hidden>→</span>
                        </button>
                        <button type="button" className="cli-deny" disabled={busy}
                          onClick={() => void decide("deny")}>
                          {t("This is not me: refuse")}
                        </button>
                      </>
                    )}
                  </div>
                )}
              </>
            )}
          </div>
        </section>
      </main>
    </div>
  );
}
