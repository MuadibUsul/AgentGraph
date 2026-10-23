"use client";

import { useCallback, useEffect, useState } from "react";

import { useI18n } from "../i18n";
import { normalizeBase, relativeTime } from "../types";

type Grant = {
  id: string;
  note: string;
  allow_write: boolean;
  created_at: string;
  expires_at: string;
  revoked_at: string | null;
  requests: number;
  last_request_at: string | null;
};

type SupportStatus = {
  private: boolean;
  operator_managed: boolean;
  active: Grant | null;
  grants: Grant[];
  operator_requests: { at: string; method: string; path: string }[];
};

// Owner-side privacy card: the operator sees counts and changes, never
// content, unless the owner opens a time-limited support window here.
export function SupportAccessCard({ apiBase, sessionToken }: { apiBase: string; sessionToken: string }) {
  const { t } = useI18n();
  const [state, setState] = useState<SupportStatus | null>(null);
  const [hours, setHours] = useState(24);
  const [note, setNote] = useState("");
  const [allowWrite, setAllowWrite] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const call = useCallback(
    async (path: string, init: RequestInit = {}) => {
      const response = await fetch(`${normalizeBase(apiBase)}/v1/human/support-access${path}`, {
        ...init,
        headers: { Authorization: `Bearer ${sessionToken}`, "Content-Type": "application/json" },
        signal: AbortSignal.timeout(15000),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body?.error?.message ?? `HTTP ${response.status}`);
      return body as SupportStatus;
    },
    [apiBase, sessionToken],
  );

  const load = useCallback(async () => {
    try {
      setState(await call(""));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, [call]);

  useEffect(() => {
    void load();
  }, [load]);

  async function act(path: string, init: RequestInit) {
    setBusy(true);
    setError(null);
    try {
      setState(await call(path, init));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  if (!state) return null;
  const active = state.active;

  return (
    <section className="card support-card">
      <div className="card-head">
        <div>
          <h2>{t("Privacy and support access")}</h2>
          <p>
            {state.private
              ? t("This workspace is private. The Veynod operator sees its counts and changes, never its documents, tasks or audit.")
              : t("The Veynod operator created this workspace and can read it. Make it private to keep the operator to counts and changes.")}
          </p>
        </div>
        <span className={`privacy-pill ${state.private ? "private" : "managed"}`}>
          {state.private ? t("Private") : t("Operator-managed")}
        </span>
      </div>
      {error && <div className="inline-error">{error}</div>}

      {!state.private ? (
        <button
          type="button"
          className="button primary"
          disabled={busy}
          onClick={() => void act("/make-private", { method: "POST", body: "{}" })}
        >
          {t("Make this workspace private")}
        </button>
      ) : active ? (
        <div className="support-active">
          <strong>
            {active.allow_write
              ? t("Support can read and change this workspace until {time}.", {
                  time: new Date(active.expires_at).toLocaleString(),
                })
              : t("Support can read this workspace until {time}.", {
                  time: new Date(active.expires_at).toLocaleString(),
                })}
          </strong>
          <span className="field-hint">
            {t("{count} operator requests so far.", { count: active.requests })}
            {active.note ? ` · ${active.note}` : ""}
          </span>
          <button
            type="button"
            className="button ghost small"
            disabled={busy}
            onClick={() => void act("", { method: "DELETE" })}
          >
            {t("Close support access now")}
          </button>
        </div>
      ) : (
        <form
          className="support-form"
          onSubmit={(event) => {
            event.preventDefault();
            void act("", {
              method: "POST",
              body: JSON.stringify({ hours, note, allow_write: allowWrite }),
            });
          }}
        >
          <p className="field-hint">
            {t("Only when you ask Veynod for help: open a window, and close it when you are done. Every request the operator makes is listed below.")}
          </p>
          <label className="field">
            <span>{t("For how long")}</span>
            <select value={hours} onChange={(event) => setHours(Number(event.target.value))}>
              <option value={1}>{t("1 hour")}</option>
              <option value={24}>{t("24 hours")}</option>
              <option value={72}>{t("3 days")}</option>
              <option value={168}>{t("7 days")}</option>
            </select>
          </label>
          <label className="field">
            <span>{t("Why (optional)")}</span>
            <input value={note} onChange={(event) => setNote(event.target.value)} maxLength={500} />
          </label>
          <label className="checkbox-row">
            <input type="checkbox" checked={allowWrite} onChange={(event) => setAllowWrite(event.target.checked)} />
            <span>{t("Also allow changes (default: read-only)")}</span>
          </label>
          <button type="submit" className="button ghost" disabled={busy}>
            {t("Open support access")}
          </button>
        </form>
      )}

      {state.operator_requests.length > 0 && (
        <details className="support-log">
          <summary>{t("Operator requests ({count})", { count: state.operator_requests.length })}</summary>
          <ul>
            {state.operator_requests.map((item, index) => (
              <li key={`${item.at}-${index}`}>
                <span className="field-hint">{relativeTime(item.at)}</span>
                <code>{item.method} {item.path}</code>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
