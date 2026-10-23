"use client";

import { useCallback, useEffect, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";
import { relativeTime } from "../types";
import { Spinner } from "./ui";

type Circuit = {
  principal_key: string;
  reason: string;
  doc_key: string | null;
  opened_at: string;
  reopen_at: string;
};

type EmbeddingUsage = {
  mode: string;
  embedding_tokens_used: number;
  embedding_tokens_quota: number;
  writes: number;
  would_rate_limit: number;
  would_circuit_open: number;
  circuit_open: Circuit[];
};

const REASONS: Record<string, string> = {
  hot_key_loop: "Rewrote one document in a loop",
  write_spike: "Wrote far above its usual rate",
  ignored_backoff: "Kept retrying before Retry-After",
};

/** Open runaway-loop circuit breakers with one-click release (AG-ST-15). */
export function BudgetCircuits({ request }: { request: ApiRequest }) {
  const { t } = useI18n();
  const [usage, setUsage] = useState<EmbeddingUsage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setUsage(await request<EmbeddingUsage>("/v1/usage/embedding?window=day"));
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, [request]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 30_000);
    return () => window.clearInterval(timer);
  }, [load]);

  async function clear(key: string) {
    setBusy(key);
    try {
      await request(`/v1/human/budget/circuits/${encodeURIComponent(key)}/clear`, {
        method: "POST",
      });
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="circuits-panel">
      <div className="panel-toolbar spaced">
        <div>
          <h3 className="section-title">{t("Circuit breakers")}</h3>
          <p className="section-sub">
            {t("Tokens paused for a runaway write loop. Release one when you have checked the agent.")}
          </p>
        </div>
      </div>
      {usage && (
        <div className="budget-stats">
          <div>
            <span className="muted">{t("Embedding tokens today")}</span>
            <strong className="tabular">{usage.embedding_tokens_used.toLocaleString()}</strong>
            <span className="muted">
              {t("limit {value}", { value: usage.embedding_tokens_quota.toLocaleString() })}
            </span>
          </div>
          <div>
            <span className="muted">{t("Writes today")}</span>
            <strong className="tabular">{usage.writes.toLocaleString()}</strong>
            <span className="muted">{t("mode {value}", { value: usage.mode })}</span>
          </div>
          <div>
            <span className="muted">{t("Would have tripped")}</span>
            <strong className="tabular">{usage.would_circuit_open}</strong>
            <span className="muted">{t("in shadow mode")}</span>
          </div>
        </div>
      )}
      {error && <p className="danger-text">{error}</p>}
      {!usage && !error && <Spinner />}
      {usage && usage.circuit_open.length === 0 && (
        <p className="muted">{t("No open circuit breakers.")}</p>
      )}
      {usage && usage.circuit_open.length > 0 && (
        <ul className="circuit-list">
          {usage.circuit_open.map((circuit) => (
            <li key={circuit.principal_key}>
              <div>
                <strong>{circuit.principal_key}</strong>
                <span className="muted">
                  {t(REASONS[circuit.reason] ?? circuit.reason)}
                  {circuit.doc_key ? ` · ${circuit.doc_key}` : ""}
                </span>
                <span className="muted">
                  {t("Opened {when}; reopens {until}", {
                    when: relativeTime(circuit.opened_at),
                    until: relativeTime(circuit.reopen_at),
                  })}
                </span>
              </div>
              <button
                type="button"
                className="button ghost small"
                disabled={busy === circuit.principal_key}
                onClick={() => void clear(circuit.principal_key)}
              >
                {busy === circuit.principal_key ? <Spinner /> : null} {t("Release")}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
