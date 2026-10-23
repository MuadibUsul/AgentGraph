"use client";

import { useEffect, useState } from "react";

import { LanguageToggle, useI18n } from "../i18n";
import { IconGraph, IconKey } from "../components/ui";
import { normalizeBase } from "../types";

const SESSION_KEY = "agentgraph.session_token";
const TOKEN_KEY = "agentgraph.admin_token";
const BASE_KEY = "agentgraph.api_base";

export default function ConnectPage() {
  const { t } = useI18n();
  const [apiBase, setApiBase] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const storedBase = localStorage.getItem(BASE_KEY);
    const base =
      storedBase ||
      (window.location.port === "3000"
        ? "http://localhost:8000"
        : window.location.origin);
    setApiBase(base);
  }, []);

  async function connect(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(`${normalizeBase(apiBase)}/v1/admin/workspaces`, {
        headers: { Authorization: `Bearer ${token.trim()}` },
        signal: AbortSignal.timeout(20000),
      });
      if (!response.ok) {
        let message = t("Admin token was rejected. It does not match this server's ADMIN_TOKEN.");
        try {
          const body = await response.json();
          message = body?.error?.message ?? message;
        } catch {
          // Keep the generic message.
        }
        throw new Error(message);
      }
      sessionStorage.setItem(TOKEN_KEY, token.trim());
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(SESSION_KEY);
      localStorage.setItem(BASE_KEY, normalizeBase(apiBase));
      window.location.assign("/dashboard/");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Unable to connect."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-page auth-page-connect">
      <div className="auth-aurora" aria-hidden />
      <div className="auth-grid-layer" aria-hidden />
      <header className="auth-header">
        <a className="auth-brand" href="/dashboard/login/">
          <span className="auth-brand-mark"><IconGraph size={22} /></span>
          <span>
            <strong>Veynod<span className="accent">.</span></strong>
            <small>{t("Human · Agent Blackboard")}</small>
          </span>
        </a>
        <div className="auth-header-actions">
          <LanguageToggle />
          <a className="auth-operator-link" href="/dashboard/login/">
            {t("User sign in")} <span aria-hidden>→</span>
          </a>
        </div>
      </header>

      <main className="auth-connect-main">
        <section className="auth-connect-story">
          <div className="auth-kicker">
            <IconKey size={13} />
            {t("Deployment operator")}
          </div>
          <h1>{t("Control-plane access")}</h1>
          <p>
            {t("This door is for the operator of this Veynod deployment. It is separate from customer workspaces and never appears on the sign-in page.")}
          </p>
          <ul className="auth-connect-list">
            <li>{t("The admin token stays in this browser.")}</li>
            <li>{t("It is never written to the repository or application logs.")}</li>
            <li>{t("Customer accounts use email or Google sign-in.")}</li>
          </ul>
        </section>

        <section className="auth-card-wrap">
          <div className="auth-card">
            <div className="auth-card-head">
              <span className="auth-card-kicker">{t("Operator console")}</span>
              <h2>{t("Connect the control plane")}</h2>
              <p>{t("Enter the API endpoint and the server's ADMIN_TOKEN.")}</p>
            </div>
            <form className="auth-form" onSubmit={connect}>
              <label className="field">
                <span>{t("API endpoint")}</span>
                <input
                  value={apiBase}
                  onChange={(event) => setApiBase(event.target.value)}
                  placeholder="https://your-agentgraph.example"
                  autoComplete="url"
                  spellCheck={false}
                  required
                />
              </label>
              <label className="field">
                <span>{t("Admin token")}</span>
                <input
                  type="password"
                  value={token}
                  onChange={(event) => setToken(event.target.value)}
                  placeholder="ADMIN_TOKEN"
                  autoComplete="off"
                  required
                />
              </label>
              {error && <div className="inline-error" role="alert">{error}</div>}
              <button type="submit" className="auth-submit" disabled={busy}>
                {busy ? t("Connecting…") : t("Enter control plane")}
                <span aria-hidden>→</span>
              </button>
            </form>
            <p className="auth-note">
              {t("Only use this console on a trusted machine. Rotate the admin token if it was ever shared.")}
            </p>
            <div className="auth-card-foot">
              <span>{t("Looking for your workspace?")}</span>
              <a href="/dashboard/login/">{t("User sign in")}</a>
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}
