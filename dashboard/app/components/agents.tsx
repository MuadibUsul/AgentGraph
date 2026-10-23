"use client";

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";

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
  items: Array<{
    agent_signature: string;
    state: string;
    step: number;
    memory_seed_version?: number | null;
  }>;
  summary: {
    tokens: number;
    by_state: Record<string, number>;
    orientation_rate: number;
    constitution_ack_rate: number;
  };
}

interface NamespaceOption {
  namespace: string;
  purpose?: string | null;
  documents?: number;
  my_access?: string;
  last_updated_at?: string | null;
  created_at?: string;
}

interface NamespaceListResponse {
  items: NamespaceOption[];
}

function namespaceTimestamp(item: NamespaceOption): number {
  const raw = item.last_updated_at ?? item.created_at;
  const parsed = raw ? Date.parse(raw) : 0;
  return Number.isFinite(parsed) ? parsed : 0;
}

function fuzzyScore(value: string, query: string): number {
  const candidate = value.toLowerCase();
  const needle = query.trim().toLowerCase();
  if (!needle) return 1;
  if (candidate === needle) return 100;
  if (candidate.startsWith(needle)) return 80;
  const position = candidate.indexOf(needle);
  if (position >= 0) return 60 - Math.min(position, 20);
  let cursor = 0;
  for (const character of needle) {
    cursor = candidate.indexOf(character, cursor);
    if (cursor === -1) return 0;
    cursor += 1;
  }
  return 30;
}

// Mirrors app.auth.ALL_PERMISSIONS (what a delta may name).
const PERMISSIONS = [
  "action:propose", "action:read", "audit:read", "budget:read", "doc:create",
  "doc:propose", "doc:read", "doc:verify", "doc:write", "events:read", "link:read",
  "link:write", "proposal:read", "proposal:vote", "query", "state:read", "state:write",
  "task:claim", "task:create", "task:read", "task:review", "task:transition",
  "timeline:append", "timeline:read", "workspace:read",
];

function splitList(value: string): string[] {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
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
  onKey: (key: string, name: string, token?: AgentToken) => void;
  notify: (message: string) => void;
  openCreateSignal?: number;
}) {
  const { t } = useI18n();
  const [showCreate, setShowCreate] = useState(false);
  const [progress, setProgress] = useState<AgentOnboardingOverview | null>(null);
  const requestRef = useRef(request);
  requestRef.current = request;
  const namespaceInputRef = useRef<HTMLInputElement>(null);

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

  const [selectedNamespaces, setSelectedNamespaces] = useState<string[]>(["*"]);
  const [namespaceQuery, setNamespaceQuery] = useState("");
  const [namespaceOptions, setNamespaceOptions] = useState<NamespaceOption[]>([]);
  const [namespaceOpen, setNamespaceOpen] = useState(false);
  const [namespaceLoading, setNamespaceLoading] = useState(false);

  const loadNamespaces = useCallback(async () => {
    setNamespaceLoading(true);
    try {
      const result = await requestRef.current<NamespaceListResponse>(
        `/v1/admin/workspaces/${workspaceId}/namespaces`,
      );
      setNamespaceOptions(result.items ?? []);
    } catch {
      setNamespaceOptions([]);
    } finally {
      setNamespaceLoading(false);
    }
  }, [workspaceId]);

  useEffect(() => {
    if (showCreate) void loadNamespaces();
  }, [showCreate, loadNamespaces]);

  useEffect(() => {
    setSelectedNamespaces(["*"]);
    setNamespaceOptions([]);
    setNamespaceQuery("");
    setNamespaceOpen(false);
  }, [workspaceId]);

  const recentNamespaces = useMemo(
    () =>
      [...namespaceOptions]
        .sort((a, b) => namespaceTimestamp(b) - namespaceTimestamp(a))
        .slice(0, 10),
    [namespaceOptions],
  );

  const namespaceMatches = useMemo(() => {
    const query = namespaceQuery.trim();
    const source = query ? namespaceOptions : recentNamespaces;
    return source
      .map((item) => ({ item, score: fuzzyScore(item.namespace, query) }))
      .filter((entry) => entry.score > 0)
      .sort(
        (a, b) =>
          b.score - a.score ||
          namespaceTimestamp(b.item) - namespaceTimestamp(a.item),
      )
      .slice(0, 10)
      .map((entry) => entry.item);
  }, [namespaceOptions, namespaceQuery, recentNamespaces]);

  const byAgent = new Map(
    (progress?.items ?? []).map((item) => [item.agent_signature, item] as const),
  );
  const [signature, setSignature] = useState("");
  const [role, setRole] = useState("EXECUTOR");
  const [busy, setBusy] = useState(false);
  // Agents may delegate short-lived child tokens; they stay folded under the
  // token people issued, loaded only when someone opens them.
  const [openDelegated, setOpenDelegated] = useState<string | null>(null);
  const [delegated, setDelegated] = useState<AgentToken[] | null>(null);
  const delegatedActive = tokens.reduce((sum, item) => sum + (item.delegated_active ?? 0), 0);

  async function takeBack(token: AgentToken) {
    setBusy(true);
    setError(null);
    try {
      const result = await request<{ count: number }>(
        `/v1/admin/workspaces/${workspaceId}/agent-tokens/${token.id}/revoke-delegated`,
        { method: "POST", body: JSON.stringify({}) },
      );
      notify(t("Took back {count} delegated tokens from {name}.", {
        count: result.count, name: token.agent_signature,
      }));
      setOpenDelegated(null);
      await onRefresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Something went wrong."));
    } finally {
      setBusy(false);
    }
  }

  async function showDelegated(token: AgentToken) {
    const key = token.canonical_id ?? token.id;
    if (openDelegated === key) {
      setOpenDelegated(null);
      return;
    }
    setOpenDelegated(key);
    setDelegated(null);
    try {
      const page = await request<{ items: AgentToken[] }>(
        `/v1/admin/workspaces/${workspaceId}/agent-tokens?issued_by=agent&parent=${encodeURIComponent(key)}`,
      );
      setDelegated(page.items);
    } catch {
      setDelegated([]);
    }
  }
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<AgentToken | null>(null);
  const [editRole, setEditRole] = useState("EXECUTOR");
  const [editNamespaces, setEditNamespaces] = useState("");
  const [editAdd, setEditAdd] = useState("");
  const [editRemove, setEditRemove] = useState("");

  function addNamespace(value: string) {
    const name = value.trim();
    if (!name) return;
    if (name === "*") {
      setSelectedNamespaces(["*"]);
    } else {
      setSelectedNamespaces((current) => {
        const narrowed = current.filter((item) => item !== "*");
        return narrowed.includes(name) ? narrowed : [...narrowed, name];
      });
    }
    setNamespaceQuery("");
    setNamespaceOpen(true);
    namespaceInputRef.current?.focus();
  }

  function removeNamespace(name: string) {
    setSelectedNamespaces((current) => current.filter((item) => item !== name));
  }

  function handleNamespaceKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter" || event.key === "," || event.key === " ") {
      if (namespaceQuery.trim()) {
        event.preventDefault();
        addNamespace(namespaceQuery);
      }
    } else if (
      event.key === "Backspace" &&
      !namespaceQuery &&
      selectedNamespaces.length > 0
    ) {
      setSelectedNamespaces((current) => current.slice(0, -1));
    }
  }

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
            allowed_namespaces: selectedNamespaces,
          }),
        },
      );
      setShowCreate(false);
      setSignature("");
      setSelectedNamespaces(["*"]);
      setNamespaceQuery("");
      setNamespaceOpen(false);
      onKey(result.api_key, result.token.agent_signature, result.token);
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

  function startEdit(token: AgentToken) {
    setError(null);
    setEditing(token);
    setEditRole(token.role_scope);
    setEditNamespaces(token.allowed_namespaces.join(", "));
    setEditAdd((token.scope_add ?? []).join(", "));
    setEditRemove((token.scope_remove ?? []).join(", "));
  }

  async function saveEdit(event: React.FormEvent) {
    event.preventDefault();
    if (!editing) return;
    setBusy(true);
    setError(null);
    try {
      await request(`/v1/admin/workspaces/${workspaceId}/agent-tokens/${editing.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          role_scope: editRole,
          allowed_namespaces: splitList(editNamespaces),
          scope_add: splitList(editAdd),
          scope_remove: splitList(editRemove),
        }),
      });
      setEditing(null);
      await onRefresh();
      notify(t("{name} updated.", { name: editing.agent_signature }));
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
        {delegatedActive > 0 && (
          <p className="onboarding-summary">
            {t("Only tokens people issued are listed. Agents hold {count} active delegated tokens, folded under the token that issued them.", { count: delegatedActive })}
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
                  <Fragment key={token.id}>
                  <tr>
                    <td>
                      <div className="agent-cell">
                        <span className="agent-avatar">
                          <IconAgents size={14} />
                        </span>
                        <span title={token.agent_signature}>{token.agent_signature}</span>
                        {(token.delegated_total ?? 0) > 0 && (
                          <button
                            type="button"
                            className="delegated-chip"
                            aria-expanded={openDelegated === (token.canonical_id ?? token.id)}
                            onClick={() => void showDelegated(token)}
                            title={t("Tokens this agent delegated")}
                          >
                            ↳ {t("{active} delegated", { active: token.delegated_active ?? 0 })}
                            {(token.delegated_total ?? 0) > (token.delegated_active ?? 0) && (
                              <span className="muted"> / {token.delegated_total}</span>
                            )}
                          </button>
                        )}
                      </div>
                    </td>
                    <td>
                      <RolePill role={token.role_scope} />
                      {((token.scope_add?.length ?? 0) > 0 || (token.scope_remove?.length ?? 0) > 0) && (
                        <small
                          className="field-hint"
                          title={[
                            ...(token.scope_add ?? []).map((item) => `+${item}`),
                            ...(token.scope_remove ?? []).map((item) => `-${item}`),
                          ].join(" ")}
                        >
                          {" "}+{token.scope_add?.length ?? 0} / −{token.scope_remove?.length ?? 0}
                        </small>
                      )}
                    </td>
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
                      <small className="field-hint">
                        {" "}
                        {byAgent.get(token.agent_signature)?.memory_seed_version != null
                          ? t("Memory seed: stored v{version}", {
                              version: byAgent.get(token.agent_signature)?.memory_seed_version ?? 0,
                            })
                          : t("Memory seed: not reported")}
                      </small>
                    </td>
                    <td>{relativeTime(token.created_at)}</td>
                    <td>
                      <button
                        type="button"
                        className="button ghost small"
                        disabled={busy}
                        onClick={() => startEdit(token)}
                      >
                        {t("Edit")}
                      </button>
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
                  {openDelegated === (token.canonical_id ?? token.id) && (
                    <tr className="delegated-row">
                      <td colSpan={7}>
                        {(token.delegated_active ?? 0) > 0 && (
                          <div className="delegated-actions">
                            <span className="field-hint">
                              {t("Delegated tokens expire on their own (1 hour by default).")}
                            </span>
                            <button
                              type="button"
                              className="button ghost small"
                              disabled={busy}
                              onClick={() => void takeBack(token)}
                            >
                              {t("Take back all delegations")}
                            </button>
                          </div>
                        )}
                        {delegated === null ? (
                          <span className="field-hint">{t("Loading…")}</span>
                        ) : delegated.length === 0 ? (
                          <span className="field-hint">{t("No delegated tokens.")}</span>
                        ) : (
                          <ul className="delegated-list">
                            {delegated.map((child) => {
                              const expired = !!child.expires_at && new Date(child.expires_at) < new Date();
                              const live = child.is_active && !child.revoked_at && !expired;
                              return (
                                <li key={child.id}>
                                  <span className={`status-dot-inline ${live ? "live" : "dead"}`} />
                                  <code>{child.agent_signature}</code>
                                  <RolePill role={child.role_scope} />
                                  <span className="field-hint">
                                    {t("issued by {agent}", { agent: child.issued_by_agent ?? "?" })}
                                    {" · "}
                                    {child.revoked_at
                                      ? t("revoked")
                                      : expired
                                        ? t("expired")
                                        : child.expires_at
                                          ? t("expires {time}", {
                                              time: new Date(child.expires_at).toLocaleString(undefined, {
                                                month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
                                              }),
                                            })
                                          : t("no expiry")}
                                  </span>
                                  {live && (
                                    <button
                                      type="button"
                                      className="button ghost small"
                                      disabled={busy}
                                      onClick={async () => {
                                        await toggle(child);
                                        setDelegated((items) => items?.map((item) =>
                                          item.id === child.id ? { ...item, is_active: false } : item) ?? null);
                                      }}
                                    >
                                      {t("Disable")}
                                    </button>
                                  )}
                                </li>
                              );
                            })}
                          </ul>
                        )}
                      </td>
                    </tr>
                  )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {editing && (
        <Modal
          title={t("Edit {name}", { name: editing.agent_signature })}
          subtitle={t("Role package plus a permission delta. Changes apply to the next request.")}
          onClose={() => setEditing(null)}
          footer={
            <>
              <button type="button" className="button ghost" onClick={() => setEditing(null)}>
                {t("Cancel")}
              </button>
              <button
                type="submit"
                form="agent-token-edit-form"
                className="button primary"
                disabled={busy || splitList(editNamespaces).length === 0}
              >
                {busy ? <Spinner /> : null} {t("Save")}
              </button>
            </>
          }
        >
          <form id="agent-token-edit-form" onSubmit={saveEdit}>
            <label className="field">
              <span>{t("Role scope")}</span>
              <select value={editRole} onChange={(e) => setEditRole(e.target.value)}>
                {ROLES.map((item) => (
                  <option key={item.role} value={item.role}>{t(item.role)}</option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>{t("Allowed namespaces")}</span>
              <input value={editNamespaces} onChange={(e) => setEditNamespaces(e.target.value)} />
              <small className="field-hint">{t("Comma-separated; * allows every namespace.")}</small>
            </label>
            <label className="field">
              <span>{t("Add permissions")}</span>
              <input
                value={editAdd}
                onChange={(e) => setEditAdd(e.target.value)}
                placeholder="doc:create"
                list="permission-options"
              />
            </label>
            <label className="field">
              <span>{t("Remove permissions")}</span>
              <input
                value={editRemove}
                onChange={(e) => setEditRemove(e.target.value)}
                placeholder="proposal:vote"
                list="permission-options"
              />
              <small className="field-hint">
                {t("Comma-separated. Removals are inherited by every token this one issues.")}
              </small>
            </label>
            <datalist id="permission-options">
              {PERMISSIONS.map((item) => (
                <option key={item} value={item} />
              ))}
            </datalist>
            <div className="inline-note">
              <a href="/docs/auth/" target="_blank" rel="noreferrer">{t("Tokens and roles")}</a>
            </div>
            {error && <div className="inline-error">{error}</div>}
          </form>
        </Modal>
      )}

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
                disabled={busy || !signature.trim() || selectedNamespaces.length === 0}
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
            <div className="field namespace-field">
              <span>{t("Allowed namespaces")}</span>
              <div
                className={`namespace-picker${namespaceOpen ? " open" : ""}`}
                onBlur={(event) => {
                  if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
                    setNamespaceOpen(false);
                  }
                }}
              >
                <div
                  className="namespace-selected"
                  onClick={() => {
                    setNamespaceOpen(true);
                    namespaceInputRef.current?.focus();
                  }}
                >
                  {selectedNamespaces.map((namespace) => (
                    <span
                      className={`namespace-chip selected${namespace === "*" ? " all" : ""}`}
                      key={namespace}
                      title={namespace === "*" ? t("All namespaces") : namespace}
                    >
                      {namespace === "*" ? t("* (all)") : namespace}
                      <button
                        type="button"
                        aria-label={t("Remove {namespace}", { namespace })}
                        onClick={(event) => {
                          event.stopPropagation();
                          removeNamespace(namespace);
                        }}
                      >
                        ×
                      </button>
                    </span>
                  ))}
                  <input
                    ref={namespaceInputRef}
                    value={namespaceQuery}
                    onChange={(event) => {
                      setNamespaceQuery(event.target.value);
                      setNamespaceOpen(true);
                    }}
                    onFocus={() => setNamespaceOpen(true)}
                    onKeyDown={handleNamespaceKeyDown}
                    placeholder={
                      selectedNamespaces.length
                        ? t("Search or add a namespace")
                        : t("Search namespaces")
                    }
                    aria-label={t("Search namespaces")}
                    aria-autocomplete="list"
                    aria-expanded={namespaceOpen}
                    aria-controls="namespace-options"
                    role="combobox"
                  />
                </div>
                {namespaceOpen && (
                  <div className="namespace-dropdown">
                    <div className="namespace-dropdown-head">
                      <span>{namespaceQuery.trim() ? t("Matches") : t("Recently edited")}</span>
                      {namespaceLoading && <Spinner />}
                    </div>
                    {namespaceMatches.length > 0 ? (
                      <div
                        className="namespace-options"
                        id="namespace-options"
                        role="listbox"
                        aria-label={t("Namespace suggestions")}
                      >
                        {namespaceMatches.map((option) => (
                          <button
                            type="button"
                            role="option"
                            aria-selected={selectedNamespaces.includes(option.namespace)}
                            className="namespace-option"
                            key={option.namespace}
                            onMouseDown={(event) => event.preventDefault()}
                            onClick={() => addNamespace(option.namespace)}
                          >
                            <span>{option.namespace}</span>
                            <small>
                              {option.documents ?? 0} {t("documents")}
                            </small>
                          </button>
                        ))}
                      </div>
                    ) : (
                      <div className="namespace-empty">
                        {namespaceLoading
                          ? t("Loading namespaces…")
                          : t("No matching namespaces")}
                      </div>
                    )}
                    <div className="namespace-help">
                      {t(
                        "Click a tag to add it; use × to remove. Type a name and press Enter for a new namespace.",
                      )}
                    </div>
                  </div>
                )}
              </div>
              <small className="field-hint">
                {t("* allows every namespace. Specific namespaces narrow the token's access.")}
              </small>
            </div>
            <div className="inline-note">
              {t("After minting you can copy the key, a ready-to-use agent prompt, or an MCP config.")}{" "}
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
