"use client";

import { useEffect, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest, AuditPayload } from "../types";
import { formatNumber, relativeTime, shortId } from "../types";
import { Empty, IconAudit, IconRefresh, Spinner } from "./ui";

export function AuditView({
  workspaceId,
  audit,
  request,
  onRefresh,
  notify,
}: {
  workspaceId: string;
  audit: AuditPayload;
  request: ApiRequest;
  onRefresh: () => Promise<void>;
  notify: (message: string) => void;
}) {
  const { t } = useI18n();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [conflictBatch, setConflictBatch] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<AuditPayload | null>(null);

  useEffect(() => {
    setExpanded(null);
  }, [audit]);

  const view = expanded ?? audit;
  const totalBatches = view.total_batches ?? view.batches.length;

  async function loadMore() {
    if (!view.next_cursor) return;
    setBusy("audit");
    setError(null);
    try {
      const next = await request<AuditPayload>(
        `/v1/admin/workspaces/${workspaceId}/audit?limit=100&cursor=${encodeURIComponent(view.next_cursor)}`,
      );
      const seen = new Set(view.batches.map((batch) => batch.session_batch_id));
      const diffSeen = new Set(view.diffs.map((diff) => diff.id));
      setExpanded({
        batches: [
          ...view.batches,
          ...next.batches.filter((batch) => !seen.has(batch.session_batch_id)),
        ],
        diffs: [
          ...view.diffs,
          ...next.diffs.filter((diff) => !diffSeen.has(diff.id)),
        ],
        total_batches: next.total_batches ?? totalBatches,
        next_cursor: next.next_cursor ?? null,
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Unable to load audit history."));
    } finally {
      setBusy(null);
    }
  }

  async function undo(batchId: string, force = false) {
    if (
      !window.confirm(
        force
          ? t("Force undo? This overwrites changes made after this batch.")
          : t("Undo every mutation in this session batch? The restore is audited as a new batch."),
      )
    ) {
      return;
    }
    setBusy(batchId);
    setError(null);
    if (!force) setConflictBatch(null);
    try {
      const result = await request<{
        restored: number;
        undo_batch_id: string;
        warnings?: string[];
      }>(
        `/v1/admin/workspaces/${workspaceId}/batches/${batchId}/undo${
          force ? "?force=true" : ""
        }`,
        { method: "POST" },
      );
      await onRefresh();
      notify(
        t("Undid {count} record(s). Undo batch {batch}.", {
          count: result.restored,
          batch: shortId(result.undo_batch_id, 8),
        }) + (result.warnings?.length ? ` ${t("Timeline events were preserved.")}` : ""),
      );
    } catch (caught) {
      setConflictBatch(batchId);
      setError(caught instanceof Error ? caught.message : t("Unable to undo batch"));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="tab-stack">
      <section className="card">
        <div className="card-head">
          <div>
            <h2>{t("Session batches")}</h2>
            <p>{t("Every agent mutation records a before/after image. Undo restores the batch.")}</p>
          </div>
          <button type="button" className="button ghost" onClick={() => void onRefresh()}>
            <IconRefresh /> {t("Refresh")}
          </button>
        </div>
        {error && <div className="inline-error">{error}</div>}
        {view.batches.length === 0 ? (
          <Empty
            title={t("No audit batches")}
            detail={t("Agent writes will appear here grouped by session_batch_id.")}
            action={
              <a className="button ghost small" href="/docs/console/" target="_blank" rel="noreferrer">
                {t("How audit and undo work")}
              </a>
            }
          />
        ) : (
          <div className="batch-grid">
            {view.batches.map((batch) => (
              <div key={batch.session_batch_id} className="batch-card">
                <div className="batch-top">
                  <span className="batch-agent" title={batch.agent_signature}>{batch.agent_signature}</span>
                  <span className="mono muted">{shortId(batch.session_batch_id, 8)}</span>
                </div>
                <div className="batch-count">
                  {batch.entry_count === 1
                    ? t("{count} change", { count: formatNumber(batch.entry_count) })
                    : t("{count} changes", { count: formatNumber(batch.entry_count) })}
                </div>
                <div className="namespace-chips">
                  {batch.targets.map((target) => (
                    <span key={target} className="namespace-chip">{target}</span>
                  ))}
                </div>
                <div className="batch-foot">
                  <span>{relativeTime(batch.last_at)}</span>
                  <button
                    type="button"
                    className="button ghost small"
                    onClick={() => void undo(batch.session_batch_id)}
                    disabled={busy === batch.session_batch_id}
                  >
                    {busy === batch.session_batch_id ? <Spinner /> : <IconAudit size={13} />} {t("Undo")}
                  </button>
                  {conflictBatch === batch.session_batch_id && (
                    <button
                      type="button"
                      className="button danger small"
                      onClick={() => void undo(batch.session_batch_id, true)}
                      disabled={busy === batch.session_batch_id}
                    >
                      {t("Force")}
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
        <div className="list-footer">
          <span className="muted">
            {t("Loaded {loaded} / {total}", {
              loaded: formatNumber(view.batches.length),
              total: formatNumber(totalBatches),
            })}
          </span>
          {view.next_cursor && view.batches.length < totalBatches && (
            <button
              type="button"
              className="button ghost small"
              onClick={() => void loadMore()}
              disabled={busy === "audit"}
            >
              {busy === "audit" ? <Spinner /> : null} {t("Load more")}
            </button>
          )}
        </div>
      </section>

      <section className="card">
        <div className="card-head">
          <div>
            <h2>{t("Recent diffs")}</h2>
            <p>{t("Newest first. Each entry carries the full before-image used for rollback.")}</p>
          </div>
          <span className="count-chip">{view.diffs.length}</span>
        </div>
        {view.diffs.length === 0 ? (
          <Empty title={t("No diffs")} />
        ) : (
          <div className="diff-list">
            {view.diffs.slice(0, 40).map((diff) => (
              <details key={diff.id} className="diff-row">
                <summary>
                  <span className="mono">{diff.operation}</span>
                  <span>{diff.target_table}</span>
                  <span className="mono muted">{shortId(diff.target_record_id, 8)}</span>
                  <span>{diff.agent_signature}</span>
                  <span className="diff-time">{relativeTime(diff.created_at)}</span>
                </summary>
                <div className="diff-columns">
                  <div>
                    <span className="diff-label">{t("before")}</span>
                    <pre className="json-block">{JSON.stringify(diff.before_diff, null, 2)}</pre>
                  </div>
                  <div>
                    <span className="diff-label">{t("after")}</span>
                    <pre className="json-block">{JSON.stringify(diff.after_diff, null, 2)}</pre>
                  </div>
                </div>
              </details>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
