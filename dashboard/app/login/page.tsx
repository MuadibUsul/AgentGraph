"use client";

import { useEffect, useRef, useState } from "react";

import { LanguageToggle, useI18n } from "../i18n";
import {
  IconEye,
  IconEyeOff,
  BrandMark,
  IconLock,
  IconMail,
} from "../components/ui";
import type { AuthConfig, AuthSession } from "../types";
import { normalizeBase } from "../types";

const SESSION_KEY = "agentgraph.session_token";
const TOKEN_KEY = "agentgraph.admin_token";
const BASE_KEY = "agentgraph.api_base";

type AuthMode = "login" | "register";

// A console page that needed a sign-in (e.g. /dashboard/cli/) leaves its path
// here; only same-console paths are honoured.
const RETURN_KEY = "agentgraph.return_to";

function returnDestination(): string {
  try {
    const stored = sessionStorage.getItem(RETURN_KEY);
    sessionStorage.removeItem(RETURN_KEY);
    if (stored && stored.startsWith("/dashboard/") && !stored.includes("//")) return stored;
  } catch {
    // Storage unavailable: fall back to the console.
  }
  return "/dashboard/";
}

export default function LoginPage() {
  const { t } = useI18n();
  const [apiBase, setApiBase] = useState("");
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const [mode, setMode] = useState<AuthMode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
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
          window.location.assign(returnDestination());
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
      googleRef.current.replaceChildren();
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
    window.location.assign(returnDestination());
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
          <span className="auth-brand-mark"><BrandMark size={34} /></span>
          <span>
            <strong>Veynod<span className="accent">.</span></strong>
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
            <span className="auth-gradient">{t("Humans hold the line.")}</span>
          </h1>
          <p className="auth-lede">
            {t("The shared blackboard for agent teams: versioned truth, lease-fenced work, and human approval before anything risky happens.")}
          </p>

          <div className="auth-board" aria-hidden>
            <div className="auth-board-bar">
              <span className="auth-board-dots"><i /><i /><i /></span>
              <span className="auth-board-title">{t("Shared blackboard · live")}</span>
              <span className="status-dot live" />
            </div>
            <ol className="auth-board-feed">
              <li style={{ animationDelay: "0.2s" }}>
                <span className="who">agent:scout</span>
                <span className="what">{t("wrote research/brief · v4")}</span>
                <span className="stamp">grant rw</span>
              </li>
              <li style={{ animationDelay: "0.9s" }}>
                <span className="who">agent:analyst</span>
                <span className="what">{t("claimed T-12 · lease 5 min")}</span>
                <span className="stamp">lease</span>
              </li>
              <li style={{ animationDelay: "1.6s" }}>
                <span className="who">system</span>
                <span className="what">{t("lease expired · T-9 back to READY")}</span>
                <span className="stamp">auto</span>
              </li>
              <li style={{ animationDelay: "2.3s" }}>
                <span className="who">gate</span>
                <span className="what">{t("CI · tests · audit passed")}</span>
                <span className="stamp ok">3/3</span>
              </li>
              <li className="human" style={{ animationDelay: "3.0s" }}>
                <span className="who">{t("you")}</span>
                <span className="what">{t("approved proposal P-7")}</span>
                <span className="stamp">human</span>
              </li>
            </ol>
          </div>

          <ul className="auth-pillars">
            <li>{t("Versioned memory")}</li>
            <li>{t("Lease-fenced tasks")}</li>
            <li>{t("Human approval")}</li>
          </ul>

        </section>

        <section className="auth-card-wrap">
          <div className="auth-card">
            <div className="auth-card-head">
              <span className="auth-card-kicker">
                {mode === "login" ? t("Account access") : t("New workspace")}
              </span>
              <h2>{mode === "login" ? t("Welcome back") : t("Create your account")}</h2>
              <p>
                {mode === "login"
                  ? t("Continue to your workspace.")
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
                <div className="auth-input">
                  <IconMail size={16} />
                  <input
                    type="email"
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                    placeholder="you@example.com"
                    autoComplete="email"
                    required
                  />
                </div>
              </label>
              <label className="field">
                <span>{t("Password")}</span>
                <div className="auth-input">
                  <IconLock size={16} />
                  <input
                    type={showPassword ? "text" : "password"}
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
                  <button
                    type="button"
                    className="password-toggle"
                    onClick={() => setShowPassword((value) => !value)}
                    aria-label={showPassword ? t("Hide password") : t("Show password")}
                    aria-pressed={showPassword}
                  >
                    {showPassword ? <IconEyeOff size={16} /> : <IconEye size={16} />}
                  </button>
                </div>
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
              <span className="auth-foot-note">
                <IconLock size={12} />
                {t("Free workspace · no card required")}
              </span>
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
