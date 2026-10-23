"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { useI18n } from "../i18n";
import type {
  ApiRequest,
  Budget,
  ExternalAction,
  HumanUser,
  HumanUserCreated,
  PathPolicy,
  Proposal,
  Usage,
  WebhookRule,
} from "../types";
import { relativeTime } from "../types";
import { BudgetCircuits } from "./circuits";
import { ProposalDiff } from "./proposal-diff";
import { Empty, IconCheck, IconClose, IconPulse, Modal, Spinner } from "./ui";

type Section = "proposals" | "policies" | "operations" | "access";

function statusTone(status: string): string {
  if (status === "APPLIED" || status === "SUCCEEDED" || status === "ACTIVE") return "mint";
  if (status === "REJECTED" || status === "FAILED") return "rose";
  if (status === "PENDING" || status === "APPROVED" || status === "EXECUTING") return "amber";
  return "slate";
}

export function GovernanceView({
  workspaceId,
  proposals,
  policies,
  budget,
  usage,
  webhooks,
  actions,
  users,
  request,
  onRefresh,
  notify,
}: {
  workspaceId: string;
  proposals: Proposal[];
  policies: PathPolicy[];
  budget: Budget | null;
  usage: Usage | null;
  webhooks: WebhookRule[];
  actions: ExternalAction[];
  users: HumanUser[];
  request: ApiRequest;
  onRefresh: () => Promise<void>;
  notify: (message: string) => void;
}) {
  const { t } = useI18n();
  const [section, setSection] = useState<Section>("proposals");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reviewNote, setReviewNote] = useState("");
  const [selectedProposal, setSelectedProposal] = useState<Proposal | null>(null);
  const [showPolicy, setShowPolicy] = useState(false);
  const [showWebhook, setShowWebhook] = useState(false);
  const [showAccount, setShowAccount] = useState(false);
  const [createdAccount, setCreatedAccount] = useState<HumanUserCreated | null>(null);

  const [policyName, setPolicyName] = useState("reviewed-docs");
  const [policyType, setPolicyType] = useState("document");
  const [policyPattern, setPolicyPattern] = useState("reviewed/*");
  const [policyRoles, setPolicyRoles] = useState<string[]>(["ANALYST"]);
  const [policyProposal, setPolicyProposal] = useState(true);
  const [policySchema, setPolicySchema] = useState(
    '{\n  "type": "object",\n  "required": ["status"]\n}',
  );
  const [policyMaxBytes, setPolicyMaxBytes] = useState("");

  const [webhookName, setWebhookName] = useState("ops-sink");
  const [webhookUrl, setWebhookUrl] = useState("https://example.com/agentgraph");
  const [webhookEvents, setWebhookEvents] = useState("DOC_CREATED,DOC_UPDATED");

  const [accountEmail, setAccountEmail] = useState("");
  const [accountName, setAccountName] = useState("");
  const [accountRole, setAccountRole] = useState("REVIEWER");

  const [budgetTokens, setBudgetTokens] = useState(String(budget?.monthly_token_budget ?? 10000000));
  const [budgetCost, setBudgetCost] = useState(String(budget?.monthly_cost_budget_usd ?? 1000));
  const [budgetRate, setBudgetRate] = useState(String(budget?.max_requests_per_minute ?? 600));

  const pending = useMemo(
    () => proposals.filter((item) => item.status === "PENDING"),
    [proposals],
  );

  async function run(key: string, action: () => Promise<void>) {
    setBusy(key);
    setError(null);
    try {
      await action();
      await onRefresh();
    } catch (caught) {
      if (caught instanceof DOMException && caught.name === "TimeoutError") {
        // The server may still finish the action: say so and show the latest state.
        setError(t("The request timed out; the action may still have completed. Showing the latest state."));
        await onRefresh().catch(() => undefined);
      } else {
        setError(caught instanceof Error ? caught.message : t("Operation failed."));
      }
    } finally {
      setBusy(null);
    }
  }

  async function applyProposal(proposal: Proposal) {
    await run(`apply:${proposal.id}`, async () => {
      await request(
        `/v1/admin/workspaces/${workspaceId}/proposals/${proposal.id}/apply`,
        {
          method: "POST",
          body: JSON.stringify({
            rebase: true,
            note: reviewNote || undefined,
          }),
        },
      );
      setSelectedProposal(null);
      setReviewNote("");
      notify(t("Proposal for {doc} applied.", { doc: proposal.doc_key }));
    });
  }

  async function rejectProposal(proposal: Proposal) {
    await run(`reject:${proposal.id}`, async () => {
      await request(
        `/v1/admin/workspaces/${workspaceId}/proposals/${proposal.id}/reject`,
        {
          method: "POST",
          body: JSON.stringify({ note: reviewNote || undefined }),
        },
      );
      setSelectedProposal(null);
      setReviewNote("");
      notify(t("Proposal for {doc} rejected.", { doc: proposal.doc_key }));
    });
  }

  async function createPolicy() {
    await run("policy:create", async () => {
      let schema: Record<string, unknown> = {};
      if (policySchema.trim()) schema = JSON.parse(policySchema);
      await request(`/v1/admin/workspaces/${workspaceId}/policies`, {
        method: "POST",
        body: JSON.stringify({
          name: policyName,
          resource_type: policyType,
          pattern: policyPattern,
          require_proposal: policyProposal,
          allowed_roles: policyRoles,
          json_schema: schema,
          max_content_bytes: policyMaxBytes ? Number(policyMaxBytes) : null,
        }),
      });
      setShowPolicy(false);
      notify(t("Path policy created."));
    });
  }

  async function togglePolicy(policy: PathPolicy) {
    await run(`policy:${policy.id}`, async () => {
      await request(
        `/v1/admin/workspaces/${workspaceId}/policies/${policy.id}`,
        {
          method: "PATCH",
          body: JSON.stringify({ active: !policy.active }),
        },
      );
      notify(t("Policy {name} {state}.", { name: policy.name, state: policy.active ? t("disabled") : t("enabled") }));
    });
  }

  async function deletePolicy(policy: PathPolicy) {
    if (!window.confirm(`Delete policy “${policy.name}”?`)) return;
    await run(`policy:${policy.id}`, async () => {
      await request(
        `/v1/admin/workspaces/${workspaceId}/policies/${policy.id}`,
        { method: "DELETE" },
      );
      notify(t("Policy deleted."));
    });
  }

  async function createWebhook() {
    await run("webhook:create", async () => {
      await request(`/v1/admin/workspaces/${workspaceId}/webhooks`, {
        method: "POST",
        body: JSON.stringify({
          name: webhookName,
          url: webhookUrl,
          event_types: webhookEvents
            .split(",")
            .map((item) => item.trim())
            .filter(Boolean),
        }),
      });
      setShowWebhook(false);
      notify(t("Webhook rule created."));
    });
  }

  async function testWebhook(rule: WebhookRule) {
    await run(`webhook:${rule.id}`, async () => {
      const result = await request<{ ok: boolean; status_code: number | null; error: string | null }>(
        `/v1/admin/workspaces/${workspaceId}/webhooks/${rule.id}/test`,
        { method: "POST" },
      );
      if (result.ok) notify(`Webhook delivered (HTTP ${result.status_code}).`);
      else setError(result.error ?? t("Webhook test failed."));
    });
  }

  async function deleteWebhook(rule: WebhookRule) {
    if (!window.confirm(`Delete webhook “${rule.name}”?`)) return;
    await run(`webhook:${rule.id}`, async () => {
      await request(
        `/v1/admin/workspaces/${workspaceId}/webhooks/${rule.id}`,
        { method: "DELETE" },
      );
      notify(t("Webhook deleted."));
    });
  }

  async function approveAction(action: ExternalAction) {
    await run(`action:${action.id}`, async () => {
      await request(
        `/v1/admin/workspaces/${workspaceId}/external-actions/${action.id}/approve`,
        { method: "POST" },
      );
      notify(t("External action approved for dispatch."));
    });
  }

  async function rejectAction(action: ExternalAction) {
    await run(`action:${action.id}`, async () => {
      await request(
        `/v1/admin/workspaces/${workspaceId}/external-actions/${action.id}/reject`,
        { method: "POST", body: JSON.stringify({ note: "Rejected by human" }) },
      );
      notify(t("External action rejected."));
    });
  }

  async function compensateAction(action: ExternalAction) {
    await run(`action:${action.id}`, async () => {
      await request(
        `/v1/admin/workspaces/${workspaceId}/external-actions/${action.id}/compensate`,
        { method: "POST", body: JSON.stringify({}) },
      );
      notify(t("Compensation queued."));
    });
  }

  async function saveBudget() {
    await run("budget", async () => {
      await request(`/v1/admin/workspaces/${workspaceId}/budget`, {
        method: "PUT",
        body: JSON.stringify({
          monthly_token_budget: Number(budgetTokens),
          monthly_cost_budget_usd: Number(budgetCost),
          max_requests_per_minute: Number(budgetRate),
          max_concurrent_leases: budget?.max_concurrent_leases ?? 100,
        }),
      });
      notify(t("Budget limits saved."));
    });
  }

  async function createAccount() {
    await run("account:create", async () => {
      const created = await request<HumanUserCreated>(
        `/v1/admin/workspaces/${workspaceId}/human-users`,
        {
          method: "POST",
          body: JSON.stringify({
            email: accountEmail,
            display_name: accountName,
            role: accountRole,
          }),
        },
      );
      setCreatedAccount(created);
      setShowAccount(false);
      setAccountEmail("");
      setAccountName("");
      notify(t("Human account created; share the temporary password once."));
    });
  }

  // Index repair (2026-10-22 retest): only broken indexes by default; a full
  // rebuild re-embeds everything, so it is a separate, confirmed action.
  const [indexHealth, setIndexHealth] = useState<IndexHealth | null>(null);
  const loadIndexHealth = useCallback(async () => {
    try {
      setIndexHealth(await request<IndexHealth>(`/v1/admin/workspaces/${workspaceId}/index-status`));
    } catch {
      setIndexHealth(null);
    }
  }, [request, workspaceId]);
  useEffect(() => {
    void loadIndexHealth();
  }, [loadIndexHealth]);

  async function reindex(mode: "repair" | "full") {
    if (
      mode === "full" &&
      !window.confirm(t("A full rebuild re-embeds every document and spends embedding quota. Continue?"))
    ) {
      return;
    }
    await run(`reindex:${mode}`, async () => {
      const result = await request<{ queued: number }>(
        `/v1/admin/workspaces/${workspaceId}/reindex`,
        { method: "POST", body: JSON.stringify(mode === "full" ? { mode, confirm: "REBUILD" } : { mode }) },
      );
      notify(t("Queued {count} document(s) for indexing; they are embedded in the background.", { count: result.queued }));
      await loadIndexHealth();
    });
  }
  const broken = indexHealth ? indexHealth.repairable + indexHealth.failed_jobs : 0;

  const sections: Array<{ id: Section; label: string; count?: number }> = [
    { id: "proposals", label: t("Proposals"), count: pending.length },
    { id: "policies", label: t("Policies"), count: policies.length },
    { id: "operations", label: t("Operations"), count: webhooks.length + actions.length },
    { id: "access", label: t("Access & budget"), count: users.length },
  ];

  return (
    <section className="governance">
      <div className="section-head">
        <div>
          <h2 className="section-title">{t("Governance")}</h2>
          <p className="section-sub">
            {t("Review proposals, gate write paths, sign webhooks, approve outbound actions and keep budgets observable.")}
          </p>
        </div>
        <div className="index-actions">
          <button
            type="button"
            className="button ghost"
            onClick={() => void reindex("repair")}
            disabled={busy !== null || broken === 0}
            title={broken === 0 ? t("Every index is current; nothing to repair.") : undefined}
          >
            {busy === "reindex:repair" ? <Spinner /> : <IconPulse />} {t("Repair broken indexes")}
            <span className={`tab-count ${broken ? "alert" : ""}`}>{broken}</span>
          </button>
          <button type="button" className="button ghost small" onClick={() => void reindex("full")} disabled={busy !== null}>
            {t("Full rebuild…")}
          </button>
          {indexHealth && indexHealth.pending_jobs + indexHealth.deferred_jobs > 0 && (
            <span className="muted small-note">
              {t("{count} waiting to be indexed", { count: indexHealth.pending_jobs + indexHealth.deferred_jobs })}
            </span>
          )}
        </div>
      </div>

      {error && (
        <div className="inline-error" role="alert">
          {error}
          <button type="button" className="icon-button" onClick={() => setError(null)} aria-label={t("Dismiss error")}>
            <IconClose size={14} />
          </button>
        </div>
      )}

      <div className="governance-tabs" role="tablist" aria-label={t("Governance sections")}>
        {sections.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={section === item.id}
            className={`governance-tab ${section === item.id ? "active" : ""}`}
            onClick={() => setSection(item.id)}
          >
            {item.label}
            {typeof item.count === "number" && <span className="tab-count">{item.count}</span>}
          </button>
        ))}
      </div>

      {section === "proposals" && (
        <div className="governance-panel">
          {proposals.length === 0 ? (
            <Empty title={t("No proposals yet")} detail={t("Agents can submit a proposal with POST /v1/documents/{doc_key}/proposals.")} />
          ) : (
            <div className="proposal-grid">
              {proposals.map((proposal) => (
                <article key={proposal.id} className="proposal-card">
                  <header className="proposal-head">
                    <div>
                      <div className="mono proposal-doc">{proposal.doc_key}</div>
                      <strong>{proposal.title}</strong>
                    </div>
                    <span className={`pill ${statusTone(proposal.status)}`}>{t(proposal.status)}</span>
                  </header>
                  <div className="proposal-meta">
                    <span>{t("base v{version}", { version: proposal.base_version })}</span>
                    <span>{t(proposal.mode)}</span>
                    <span>{t("score {score} / {count} vote(s)", { score: proposal.vote_score.toFixed(1), count: proposal.vote_count })}</span>
                    {proposal.tainted && <span className="pill rose">{t("tainted source")}</span>}
                  </div>
                  <pre className="proposal-body">{proposal.content.slice(0, 800)}</pre>
                  {proposal.content.length > 800 && (
                    <p className="proposal-truncated muted">
                      {t("Preview: first 800 of {total} characters. Open Review for the changes and the full text.", {
                        total: proposal.content.length.toLocaleString(),
                      })}
                    </p>
                  )}
                  {proposal.conflict_details && (
                    <div className="inline-error">{t("Rebase conflict — review the latest document before applying.")}</div>
                  )}
                  <footer className="proposal-actions">
                    <span className="muted">{t("by {actor} · {time}", { actor: proposal.created_by, time: relativeTime(proposal.created_at) })}</span>
                    {proposal.status === "PENDING" && (
                      <>
                        <button
                          type="button"
                          className="button ghost small"
                          onClick={() => {
                            setSelectedProposal(proposal);
                            setReviewNote("");
                          }}
                        >
                          {t("Review")}
                        </button>
                        <button
                          type="button"
                          className="button primary small"
                          onClick={() => {
                            setSelectedProposal(proposal);
                            setReviewNote("");
                          }}
                        >
                          <IconCheck /> {t("Apply")}
                        </button>
                      </>
                    )}
                  </footer>
                </article>
              ))}
            </div>
          )}
        </div>
      )}

      {section === "policies" && (
        <div className="governance-panel">
          <div className="panel-toolbar">
            <p className="section-sub">
              {t("Policies match resource keys with glob patterns. A require_proposal path rejects direct agent writes.")}
            </p>
            <button type="button" className="button primary" onClick={() => setShowPolicy(true)}>
              {t("New policy")}
            </button>
          </div>
          {policies.length === 0 ? (
            <Empty title={t("No path policies")} detail={t("Writes are currently unrestricted by path. Add a policy to require review on sensitive namespaces.")} />
          ) : (
            <div className="policy-list">
              {policies.map((policy) => (
                <article key={policy.id} className={`policy-card ${policy.active ? "" : "inactive"}`}>
                  <div className="policy-main">
                    <div className="policy-title">
                      <strong>{policy.name}</strong>
                      <span className="pill slate">{t(policy.resource_type)}</span>
                      {policy.require_proposal && <span className="pill amber">{t("proposal required")}</span>}
                      {!policy.active && <span className="pill rose">{t("disabled")}</span>}
                    </div>
                    <div className="mono muted">{policy.pattern}</div>
                    <div className="policy-meta">
                      <span>{t("roles:")} {policy.allowed_roles.length ? policy.allowed_roles.join(", ") : t("all")}</span>
                      <span>{t("priority")} {policy.priority}</span>
                      {policy.max_content_bytes && <span>{t("max {bytes} bytes", { bytes: policy.max_content_bytes })}</span>}
                      {Object.keys(policy.json_schema).length > 0 && <span>{t("JSON Schema")}</span>}
                    </div>
                  </div>
                  <div className="policy-actions">
                    <button type="button" className="button ghost small" onClick={() => void togglePolicy(policy)}>
                      {policy.active ? t("Disable") : t("Enable")}
                    </button>
                    <button type="button" className="button danger small" onClick={() => void deletePolicy(policy)}>
                      {t("Delete")}
                    </button>
                  </div>
                </article>
              ))}
            </div>
          )}
        </div>
      )}

      {section === "operations" && (
        <div className="governance-panel operations-grid">
          <section>
            <div className="panel-toolbar">
              <div>
                <h3 className="section-title">{t("Webhooks")}</h3>
                <p className="section-sub">{t("Signed, persistent, retried deliveries from the outbox.")}</p>
              </div>
              <button type="button" className="button ghost small" onClick={() => setShowWebhook(true)}>
                {t("New rule")}
              </button>
            </div>
            {webhooks.length === 0 ? (
              <Empty title={t("No webhook rules")} detail={t("Add an endpoint to receive signed workspace events.")} />
            ) : (
              <div className="stack-list">
                {webhooks.map((rule) => (
                  <article key={rule.id} className="compact-row">
                    <div>
                      <strong>{rule.name}</strong>
                      <div className="mono muted">{rule.url}</div>
                      <div className="proposal-meta">
                        <span>{rule.event_types.join(", ")}</span>
                        <span>{rule.active ? t("active") : t("paused")}</span>
                        <span>{t("{count} recent failure(s)", { count: rule.consecutive_failures })}</span>
                      </div>
                    </div>
                    <div className="row-actions">
                      <button type="button" className="button ghost small" onClick={() => void testWebhook(rule)}>
                        {t("Test")}
                      </button>
                      <button type="button" className="button danger small" onClick={() => void deleteWebhook(rule)}>
                        {t("Delete")}
                      </button>
                    </div>
                  </article>
                ))}
              </div>
            )}
          </section>

          <section>
            <div className="panel-toolbar">
              <div>
                <h3 className="section-title">{t("External actions")}</h3>
                <p className="section-sub">{t("Agent-proposed side effects wait for human approval; compensations are explicit.")}</p>
              </div>
            </div>
            {actions.length === 0 ? (
              <Empty title={t("No external actions")} detail={t("Agents propose them with POST /v1/external-actions.")} />
            ) : (
              <div className="stack-list">
                {actions.map((action) => (
                  <article key={action.id} className="compact-row">
                    <div>
                      <div className="action-title">
                        <strong>{action.action_type}</strong>
                        <span className={`pill ${statusTone(action.status)}`}>{t(action.status)}</span>
                      </div>
                      <div className="mono muted">{action.method} {action.target_url}</div>
                      {action.payload && (
                        <pre className="json-preview" aria-label="Action payload">
                          {JSON.stringify(action.payload, null, 2)}
                        </pre>
                      )}
                      {action.headers && Object.keys(action.headers).length > 0 && (
                        <pre className="json-preview" aria-label="Action headers">
                          {JSON.stringify(action.headers, null, 2)}
                        </pre>
                      )}
                      <div className="proposal-meta">
                        <span>{t("{attempts}/{max} attempts", { attempts: action.attempts, max: action.max_attempts })}</span>
                        <span>{t("by")} {action.created_by}</span>
                        {action.last_error && <span className="danger-text">{action.last_error}</span>}
                      </div>
                    </div>
                    <div className="row-actions">
                      {action.status === "PENDING_APPROVAL" && (
                        <>
                          <button type="button" className="button primary small" onClick={() => void approveAction(action)}>
                            {t("Approve")}
                          </button>
                          <button type="button" className="button danger small" onClick={() => void rejectAction(action)}>
                            {t("Reject")}
                          </button>
                        </>
                      )}
                      {["SUCCEEDED", "FAILED"].includes(action.status) && (
                        <button type="button" className="button ghost small" onClick={() => void compensateAction(action)}>
                          {t("Compensate")}
                        </button>
                      )}
                    </div>
                  </article>
                ))}
              </div>
            )}
          </section>
        </div>
      )}

      {section === "access" && (
        <div className="governance-panel access-grid">
          <section>
            <div className="panel-toolbar">
              <div>
                <h3 className="section-title">{t("Budget")}</h3>
                <p className="section-sub">{t("Monthly token/cost envelopes and per-minute request ceilings.")}</p>
              </div>
            </div>
            <div className="budget-stats">
              <div>
                <span className="muted">{t("Tokens used (30d)")}</span>
                <strong className="tabular">{usage?.tokens_used.toLocaleString() ?? "—"}</strong>
                <span className="muted">{t("limit {value}", { value: budget?.monthly_token_budget.toLocaleString() ?? "—" })}</span>
              </div>
              <div>
                <span className="muted">{t("Cost used (30d)")}</span>
                <strong className="tabular">${usage?.cost_usd_used.toFixed(2) ?? "—"}</strong>
                <span className="muted">{t("limit {value}", { value: `$${budget?.monthly_cost_budget_usd.toFixed(2) ?? "—"}` })}</span>
              </div>
              <div>
                <span className="muted">{t("Request limit")}</span>
                <strong className="tabular">{budget?.max_requests_per_minute ?? "—"}/min</strong>
                <span className="muted">{t("fixed window")}</span>
              </div>
            </div>
            <div className="form-grid">
              <label className="field">
                <span>{t("Monthly token budget")}</span>
                <input value={budgetTokens} onChange={(event) => setBudgetTokens(event.target.value)} inputMode="numeric" />
              </label>
              <label className="field">
                <span>{t("Monthly cost budget (USD)")}</span>
                <input value={budgetCost} onChange={(event) => setBudgetCost(event.target.value)} inputMode="decimal" />
              </label>
              <label className="field">
                <span>{t("Requests per minute")}</span>
                <input value={budgetRate} onChange={(event) => setBudgetRate(event.target.value)} inputMode="numeric" />
              </label>
            </div>
            <button type="button" className="button primary" onClick={() => void saveBudget()}>
              {t("Save budget")}
            </button>
            <BudgetCircuits request={request} />

            <div className="panel-toolbar spaced">
              <div>
                <h3 className="section-title">{t("Human accounts")}</h3>
                <p className="section-sub">{t("OWNER can administer; REVIEWER can approve; VIEWER is read-only.")}</p>
              </div>
              <button type="button" className="button ghost small" onClick={() => setShowAccount(true)}>
                {t("New account")}
              </button>
            </div>
            {users.length === 0 ? (
              <Empty title={t("No human accounts")} detail={t("Create an OWNER account before using the human console API.")} />
            ) : (
              <div className="stack-list">
                {users.map((user) => (
                  <article key={user.id} className="compact-row">
                    <div>
                      <strong>{user.display_name}</strong>
                      <div className="muted">{user.email}</div>
                    </div>
                    <div className="row-actions">
                      <span className="pill slate">{t(user.role)}</span>
                      <span className={`pill ${user.is_active ? "mint" : "rose"}`}>{user.is_active ? t("active") : t("disabled")}</span>
                      <span className="muted">{t("last login")} {relativeTime(user.last_login_at)}</span>
                    </div>
                  </article>
                ))}
              </div>
            )}
          </section>
        </div>
      )}

      {selectedProposal && (
        <Modal
          title={t("Review {doc}", { doc: selectedProposal.doc_key })}
          subtitle={t("Applying uses three-way rebase; overlapping edits return a conflict instead of silently overwriting.")}
          onClose={() => setSelectedProposal(null)}
          wide
          footer={
            <>
              <button type="button" className="button ghost" onClick={() => setSelectedProposal(null)}>
                {t("Cancel")}
              </button>
              <button
                type="button"
                className="button danger"
                onClick={() => void rejectProposal(selectedProposal)}
                disabled={busy !== null || !reviewNote.trim()}
                title={reviewNote.trim() ? undefined : t("A rejection needs a reason: write a review note first.")}
              >
                {t("Reject")}
              </button>
              <button
                type="button"
                className="button primary"
                onClick={() => void applyProposal(selectedProposal)}
                disabled={busy !== null}
              >
                {busy ? <Spinner /> : <IconCheck />} {t("Apply proposal")}
              </button>
            </>
          }
        >
          <div className="proposal-meta">
            <span>{t("base v{version}", { version: selectedProposal.base_version })}</span>
            <span>{t("{count} vote(s)", { count: selectedProposal.vote_count })}</span>
            <span>{t("score {score}", { score: selectedProposal.vote_score.toFixed(1) })}</span>
          </div>
          <ProposalDiff proposal={selectedProposal} workspaceId={workspaceId} request={request} />
          <label className="field">
            <span>{t("Review note")}</span>
            <textarea rows={3} value={reviewNote} onChange={(event) => setReviewNote(event.target.value)} />
          </label>
        </Modal>
      )}

      {showPolicy && (
        <Modal title={t("New path policy")} subtitle={t("The most specific matching active policy wins.")} onClose={() => setShowPolicy(false)}>
          <div className="form-grid">
            <label className="field">
              <span>{t("Name")}</span>
              <input value={policyName} onChange={(event) => setPolicyName(event.target.value)} />
            </label>
            <label className="field">
              <span>{t("Resource type")}</span>
              <select value={policyType} onChange={(event) => setPolicyType(event.target.value)}>
                <option value="document">{t("document")}</option>
                <option value="slot">{t("slot")}</option>
                <option value="task">{t("task")}</option>
              </select>
            </label>
            <label className="field">
              <span>{t("Glob pattern")}</span>
              <input value={policyPattern} onChange={(event) => setPolicyPattern(event.target.value)} placeholder="reviewed/*" />
            </label>
            <label className="field">
              <span>{t("Max content bytes (optional)")}</span>
              <input value={policyMaxBytes} onChange={(event) => setPolicyMaxBytes(event.target.value)} inputMode="numeric" />
            </label>
          </div>
          <label className="checkbox-row">
            <input type="checkbox" checked={policyProposal} onChange={(event) => setPolicyProposal(event.target.checked)} />
            {t("Require a reviewed proposal for direct agent writes")}
          </label>
          <fieldset className="role-fieldset">
            <legend>{t("Allowed agent roles")}</legend>
            {["SCOUT", "ANALYST", "AUDITOR", "EXECUTOR"].map((role) => (
              <label key={role} className="checkbox-row">
                <input
                  type="checkbox"
                  checked={policyRoles.includes(role)}
                  onChange={(event) =>
                    setPolicyRoles((current) =>
                      event.target.checked ? [...current, role] : current.filter((item) => item !== role),
                    )
                  }
                />
                {t(role)}
              </label>
            ))}
          </fieldset>
          <label className="field">
            <span>{t("Frontmatter JSON Schema")}</span>
            <textarea rows={7} className="mono-input" value={policySchema} onChange={(event) => setPolicySchema(event.target.value)} />
          </label>
          <div className="modal-actions">
            <button type="button" className="button ghost" onClick={() => setShowPolicy(false)}>
              {t("Cancel")}
            </button>
            <button type="button" className="button primary" onClick={() => void createPolicy()} disabled={busy !== null}>
              {t("Create policy")}
            </button>
          </div>
        </Modal>
      )}

      {showWebhook && (
        <Modal title={t("New webhook rule")} subtitle={t("Deliveries are signed with HMAC-SHA256 and retried with backoff.")} onClose={() => setShowWebhook(false)}>
          <label className="field">
            <span>{t("Name")}</span>
            <input value={webhookName} onChange={(event) => setWebhookName(event.target.value)} />
          </label>
          <label className="field">
            <span>{t("HTTPS endpoint")}</span>
            <input value={webhookUrl} onChange={(event) => setWebhookUrl(event.target.value)} spellCheck={false} />
          </label>
          <label className="field">
            <span>{t("Event types (comma-separated)")}</span>
            <input value={webhookEvents} onChange={(event) => setWebhookEvents(event.target.value)} />
          </label>
          <div className="modal-actions">
            <button type="button" className="button ghost" onClick={() => setShowWebhook(false)}>
              {t("Cancel")}
            </button>
            <button type="button" className="button primary" onClick={() => void createWebhook()} disabled={busy !== null}>
              {t("Create rule")}
            </button>
          </div>
        </Modal>
      )}

      {showAccount && (
        <Modal title={t("New human account")} subtitle={t("The temporary password is shown once.")} onClose={() => setShowAccount(false)}>
          <label className="field">
            <span>{t("Email")}</span>
            <input value={accountEmail} onChange={(event) => setAccountEmail(event.target.value)} type="email" />
          </label>
          <label className="field">
            <span>{t("Display name")}</span>
            <input value={accountName} onChange={(event) => setAccountName(event.target.value)} />
          </label>
          <label className="field">
            <span>{t("Role")}</span>
            <select value={accountRole} onChange={(event) => setAccountRole(event.target.value)}>
              <option value="OWNER">{t("OWNER")}</option>
              <option value="REVIEWER">{t("REVIEWER")}</option>
              <option value="VIEWER">{t("VIEWER")}</option>
            </select>
          </label>
          <div className="modal-actions">
            <button type="button" className="button ghost" onClick={() => setShowAccount(false)}>
              {t("Cancel")}
            </button>
            <button type="button" className="button primary" onClick={() => void createAccount()} disabled={busy !== null}>
              {t("Create account")}
            </button>
          </div>
        </Modal>
      )}

      {createdAccount && (
        <Modal title={t("Account created")} subtitle={t("Copy the temporary password now; it cannot be recovered.")} onClose={() => setCreatedAccount(null)}>
          <div className="key-box">
            <code>{createdAccount.temporary_password}</code>
          </div>
          <p className="section-sub">
            {t("Sign in with")} <strong>{createdAccount.user.email}</strong> {t("via POST /v1/auth/login, then use the returned bearer token on /v1/human endpoints.")}
          </p>
          <div className="modal-actions">
            <button type="button" className="button primary" onClick={() => setCreatedAccount(null)}>
              {t("Stored securely")}
            </button>
          </div>
        </Modal>
      )}
    </section>
  );
}

interface IndexHealth {
  pending_jobs: number;
  deferred_jobs: number;
  failed_jobs: number;
  repairable: number;
}
