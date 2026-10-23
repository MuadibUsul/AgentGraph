"use client";

import { useEffect, useRef, useState } from "react";

import { LanguageToggle, useI18n } from "../i18n";
import { IconGraph } from "../components/ui";
import type { AuthConfig, AuthSession } from "../types";
import { normalizeBase } from "../types";

const SESSION_KEY = "agentgraph.session_token";
const TOKEN_KEY = "agentgraph.admin_token";
const BASE_KEY = "agentgraph.api_base";

type AuthMode = "login" | "register";

export default function LoginPage() {
  const { t } = useI18n();
  const [apiBase, setApiBase] = useState("");
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const [mode, setMode] = useState<AuthMode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [workspaceName, setWorkspaceName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const googleRef = useRef<HTMLDivElement>(null);
  const googleSignInRef = useRef<(credential: string) => Promise<void>>(
    async () => undefined,
  );

  useEffect(() => {
    const storedBase = localStorage.getItem(BASE_KEY);
    const base =
      storedBase ||
      (window.location.port === "3000"
        ? "http://localhost:8000"
        : window.location.origin);
    setApiBase(base);
    const existing = localStorage.getItem(SESSION_KEY);
    if (existing) {
      void fetch(`${normalizeBase(base)}/v1/auth/me`, {
        headers: { Authorization: `Bearer ${existing}` },
        signal: AbortSignal.timeout(8000),
      }).then((response) => {
        if (response.ok) {
          window.location.assign("/dashboard/");
        } else {
          localStorage.removeItem(SESSION_KEY);
        }
      }).catch(() => undefined);
    } else if (
      sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem(TOKEN_KEY)
    ) {
      window.location.assign("/dashboard/");
    }
    void fetch(`${normalizeBase(base)}/v1/auth/config`, {
      signal: AbortSignal.timeout(10000),
    })
      .then(async (response) => {
        if (response.ok) setAuthConfig((await response.json()) as AuthConfig);
      })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!authConfig?.google_client_id || !googleRef.current) return;
    let cancelled = false;
    const initialize = () => {
      if (cancelled || !googleRef.current) return;
      const google = (window as unknown as {
        google?: {
          accounts?: {
            id?: {
              initialize: (options: Record<string, unknown>) => void;
              renderButton: (element: HTMLElement, options: Record<string, unknown>) => void;
            };
          };
        };
      }).google;
      const id = google?.accounts?.id;
      if (!id) return;
      id.initialize({
        client_id: authConfig.google_client_id,
        callback: (response: { credential?: string }) => {
          if (response.credential) void googleSignInRef.current(response.credential);
        },
      });
      const widthSource =
        googleRef.current.closest(".auth-card")?.querySelector(".auth-form") ??
        googleRef.current;
      const width = Math.max(
        220,
        Math.min(400, Math.floor(widthSource.getBoundingClientRect().width || 320)),
      );
      id.renderButton(googleRef.current, {
        theme: "outline",
        size: "large",
        shape: "rectangular",
        logo_alignment: "left",
        width,
        text: "continue_with",
      });
    };
    const existing = document.getElementById("google-identity-script");
    if (existing) {
      initialize();
    } else {
      const script = document.createElement("script");
      script.id = "google-identity-script";
      script.src = "https://accounts.google.com/gsi/client";
      script.async = true;
      script.onload = initialize;
      document.head.appendChild(script);
    }
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authConfig]);

  async function finish(session: AuthSession) {
    localStorage.setItem(SESSION_KEY, session.token);
    sessionStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(TOKEN_KEY);
    localStorage.setItem(BASE_KEY, normalizeBase(apiBase));
    window.location.assign("/dashboard/");
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const registering = mode === "register";
      const response = await fetch(
        `${normalizeBase(apiBase)}/v1/auth/${registering ? "register" : "login"}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(
            registering
              ? {
                  email,
                  password,
                  display_name: displayName,
                  workspace_name: workspaceName || undefined,
                }
              : { email, password },
          ),
          signal: AbortSignal.timeout(30000),
        },
      );
      if (!response.ok) {
        let message = t("Unable to sign in.");
        try {
          const body = await response.json();
          message = body?.error?.message ?? message;
        } catch {
          // Keep the generic message.
        }
        throw new Error(message);
      }
      await finish((await response.json()) as AuthSession);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Unable to sign in."));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    googleSignInRef.current = googleSignIn;
  });

  async function googleSignIn(credential: string) {
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(`${normalizeBase(apiBase)}/v1/auth/google`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          credential,
          ...(mode === "register"
            ? {
                display_name: displayName.trim() || undefined,
                workspace_name: workspaceName.trim() || undefined,
              }
            : {}),
        }),
        signal: AbortSignal.timeout(30000),
      });
      if (!response.ok) {
        let message = t("Google sign-in failed.");
        try {
          const body = await response.json();
          message = body?.error?.message ?? message;
        } catch {
          // Keep the generic message.
        }
        throw new Error(message);
      }
      await finish((await response.json()) as AuthSession);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Google sign-in failed."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-page">
      <div className="auth-aurora" aria-hidden />
      <div className="auth-grid-layer" aria-hidden />
      <header className="auth-header">
        <a className="auth-brand" href="/dashboard/">
          <span className="auth-brand-mark"><IconGraph size={22} /></span>
          <span>
            <strong>AgentGraph<span className="accent">.</span></strong>
            <small>{t("Human · Agent Blackboard")}</small>
          </span>
        </a>
        <div className="auth-header-actions">
          <a className="auth-docs-link" href="/docs/">{t("Docs")}</a>
          <LanguageToggle />
        </div>
      </header>

      <main className="auth-main">
        <section className="auth-story">
          <div className="auth-kicker">
            <span className="status-dot live" />
            {t("Human-in-the-loop control plane")}
          </div>
          <h1>
            {t("Agents do the work.")}
            <br />
            <span className="auth-gradient">{t("You hold the line.")}</span>
          </h1>
          <p className="auth-lede">
            {t("The shared blackboard for agent teams: versioned truth, lease-fenced work, and human approval before anything risky happens.")}
          </p>

          <div className="auth-proof-grid">
            <div>
              <span className="auth-proof-index">01</span>
              <strong>{t("Approve the risky move")}</strong>
              <p>{t("Human gates hold escalations until a person decides.")}</p>
            </div>
            <div>
              <span className="auth-proof-index">02</span>
              <strong>{t("Never lose the thread")}</strong>
              <p>{t("Every document version, lease and handoff is auditable.")}</p>
            </div>
            <div>
              <span className="auth-proof-index">03</span>
              <strong>{t("Bring any runtime")}</strong>
              <p>{t("MCP-native tools meet your agents where they already work.")}</p>
            </div>
          </div>

          <div className="auth-capabilities" aria-label={t("Platform capabilities")}>
            <span>{t("PostgreSQL 16 + pgvector")}</span>
            <span>{t("MCP-native")}</span>
            <span>{t("Lease fencing")}</span>
            <span>{t("Immutable versions")}</span>
            <span>{t("Batch undo")}</span>
          </div>
        </section>

        <section className="auth-card-wrap">
          <div className="auth-card">
            <div className="auth-card-head">
              <span className="auth-card-kicker">
                {mode === "login" ? t("Welcome back") : t("Start your workspace")}
              </span>
              <h2>{mode === "login" ? t("Sign in to AgentGraph") : t("Create your account")}</h2>
              <p>
                {mode === "login"
                  ? t("Your workspace is waiting on the other side.")
                  : t("A free workspace, ready for your first agent team.")}
              </p>
            </div>

            <div className="auth-tabs" role="tablist" aria-label={t("Account access")}>
              <button
                type="button"
                role="tab"
                aria-selected={mode === "login"}
                className={`auth-tab ${mode === "login" ? "active" : ""}`}
                onClick={() => {
                  setMode("login");
                  setError(null);
                }}
              >
                {t("Sign in")}
              </button>
              {authConfig?.signup_enabled !== false && (
                <button
                  type="button"
                  role="tab"
                  aria-selected={mode === "register"}
                  className={`auth-tab ${mode === "register" ? "active" : ""}`}
                  onClick={() => {
                    setMode("register");
                    setError(null);
                  }}
                >
                  {t("Create account")}
                </button>
              )}
            </div>

            <form className="auth-form" onSubmit={submit}>
              {mode === "register" && (
                <div className="auth-form-row">
                  <label className="field">
                    <span>{t("Your name")}</span>
                    <input
                      value={displayName}
                      onChange={(event) => setDisplayName(event.target.value)}
                      placeholder={t("Ada Lovelace")}
                      autoComplete="name"
                      required
                    />
                  </label>
                  <label className="field">
                    <span>{t("Workspace name")}</span>
                    <input
                      value={workspaceName}
                      onChange={(event) => setWorkspaceName(event.target.value)}
                      placeholder={t("My Agent Workspace")}
                      autoComplete="organization"
                      maxLength={128}
                    />
                  </label>
                </div>
              )}
              <label className="field">
                <span>{t("Email")}</span>
                <input
                  type="email"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  placeholder="you@example.com"
                  autoComplete="email"
                  required
                />
              </label>
              <label className="field">
                <span>{t("Password")}</span>
                <input
                  type="password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  placeholder={
                    mode === "register"
                      ? t("At least 8 characters")
                      : t("Your password")
                  }
                  autoComplete={mode === "register" ? "new-password" : "current-password"}
                  minLength={8}
                  required
                />
              </label>
              {error && <div className="inline-error" role="alert">{error}</div>}
              <button type="submit" className="auth-submit" disabled={busy}>
                {busy
                  ? t("Please wait…")
                  : mode === "register"
                    ? t("Create free workspace")
                    : t("Enter workspace")}
                <span aria-hidden>→</span>
              </button>
            </form>

            {authConfig?.google_enabled && (
              <>
                <div className="auth-divider"><span>{t("or")}</span></div>
                <div ref={googleRef} className="google-button" />
              </>
            )}

            <div className="auth-card-foot">
              <span>{t("Free workspace · no card required")}</span>
            </div>
          </div>
          <p className="auth-legal">
            {t("By continuing, you agree to the workspace terms and acceptable-use policy.")}
          </p>
        </section>
      </main>
    </div>
  );
}
