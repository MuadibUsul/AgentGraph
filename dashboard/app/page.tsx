"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { AgentsView } from "./components/agents";
import { AuditView } from "./components/audit";
import { SituationView } from "./components/situation";
import { Blackboard } from "./components/blackboard";
import { DocumentsView } from "./components/documents";
import { GovernanceView } from "./components/governance";
import { AnimatedNumber, spotlight, useSlidingIndicator } from "./components/motion";
import { AgentConnect, GettingStarted, type OnboardingStepId } from "./components/onboarding";
import { LanguageToggle, useI18n } from "./i18n";
import {
  CreateTaskModal,
  TaskBoard,
  TaskDetailModal,
} from "./components/task-board";
import {
  IconAgents,
  IconAudit,
  IconBoard,
  IconPulse,
  IconChevron,
  IconClose,
  IconCopy,
  IconDocuments,
  IconGraph,
  IconKey,
  IconLayers,
  IconLock,
  IconPlus,
  IconRefresh,
  IconTasks,
  IconUnlock,
  Modal,
  Spinner,
} from "./components/ui";
import type {
  AgentToken,
  ApiRequest,
  AuditPayload,
  AuthSession,
  Budget,
  DocumentDetail,
  DocumentPage,
  DocumentSummary,
  ExternalAction,
  HumanUser,
  KeyReveal,
  PathPolicy,
  Proposal,
  ReadyInfo,
  Slot,
  Stats,
  StreamEvent,
  TaskDetail,
  TaskSummary,
  TimelineItem,
  Usage,
  WebhookRule,
  WorkspaceSummary,
} from "./types";
import {
  formatNumber,
  normalizeBase,
  pretty,
  relativeTime,
  shortDate,
  tierLabel,
} from "./types";

const CONSOLE_BUILD = "3.5.0";
const TOKEN_KEY = "agentgraph.admin_token";
const SESSION_KEY = "agentgraph.session_token";
const BASE_KEY = "agentgraph.api_base";

function sessionPath(path: string, workspaceId: string, method: string): string {
  const prefix = `/v1/admin/workspaces/${workspaceId}`;
  if (!path.startsWith(prefix)) return path;
  const rest = path.slice(prefix.length);
  if (rest === "/stats") return "/v1/human/workspace";
  if (rest === "/agent-onboarding") return "/v1/human/agent-onboarding";
  if (rest === "/agent-tokens" || rest.startsWith("/agent-tokens/")) {
    return `/v1/human${rest}`;
  }
  if (rest === "/namespaces") return "/v1/human/namespaces";
  if (rest.startsWith("/document-parts")) return `/v1/human${rest}`;
  if (rest === "/audit" || rest.startsWith("/audit?")) return `/v1/human/audit${rest.slice(6)}`;
  if (rest.startsWith("/batches")) return `/v1/human${rest}`;
  if (rest.startsWith("/proposals")) return `/v1/human${rest}`;
  if (rest.startsWith("/policies")) return `/v1/human${rest}`;
  if (rest.startsWith("/webhooks")) return `/v1/human${rest}`;
  if (rest.startsWith("/external-actions")) return `/v1/human${rest}`;
  if (rest === "/budget") return "/v1/human/budget";
  if (rest === "/usage") return "/v1/human/usage";
  if (rest === "/reindex") return "/v1/human/reindex";
  if (rest === "/index-status") return "/v1/index/status";
  if (rest.startsWith("/human-users")) {
    return `/v1/human/accounts${rest.slice("/human-users".length)}`;
  }
  if (rest.startsWith("/timeline")) return `/v1/timeline${rest.slice("/timeline".length)}`;
  if (rest.startsWith("/alerts") || rest.startsWith("/metrics")) return `/v1${rest}`;
  if (rest === "/tasks" || rest.startsWith("/tasks?")) return `/v1/tasks${rest.slice(6)}`;
  if (rest.startsWith("/tasks/")) {
    if (rest.endsWith("/review") || rest.endsWith("/escalate")) {
      return `/v1/human${rest}`;
    }
    return `/v1/tasks${rest.slice(6)}`;
  }
  if (rest === "/slots" || rest.startsWith("/slots?")) return `/v1/slots${rest.slice(6)}`;
  if (rest.startsWith("/slots/")) {
    return method === "PUT" || method === "PATCH"
      ? `/v1/human${rest}`
      : `/v1/slots${rest.slice(6)}`;
  }
  if (rest === "/documents" || rest.startsWith("/documents?")) {
    return method === "POST" ? "/v1/human/documents" : `/v1/documents${rest.slice(10)}`;
  }
  if (rest.startsWith("/documents/")) {
    if (rest.includes("/versions")) return `/v1/documents${rest.slice(10)}`;
    if (rest.endsWith("/verify") || rest.endsWith("/deprecate")) return `/v1/human${rest}`;
    if (method === "PUT" || method === "PATCH") return `/v1/human${rest}`;
    return `/v1/documents${rest.slice(10)}`;
  }
  return path;
}
type Tab = "situation" | "blackboard" | "tasks" | "documents" | "agents" | "governance" | "audit";

export default function ConsolePage() {
  const { t, lang } = useI18n();
  const [mounted, setMounted] = useState(false);
  const [apiBase, setApiBase] = useState("http://localhost:8000");
  const [adminToken, setAdminToken] = useState("");
  const [sessionMode, setSessionMode] = useState(false);
  const [sessionToken, setSessionToken] = useState("");
  const [sessionUser, setSessionUser] = useState<HumanUser | null>(null);
  const [connected, setConnected] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [slots, setSlots] = useState<Slot[]>([]);
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  const [docTotal, setDocTotal] = useState(0);
  const [docCursor, setDocCursor] = useState<string | null>(null);
  const [docLoadingMore, setDocLoadingMore] = useState(false);
  const [docFilters, setDocFilters] = useState({
    namespace: "",
    verificationStatus: "",
    tainted: "",
    updatedAfter: "",
    updatedBefore: "",
    sort: "updated_desc",
  });
  const [agents, setAgents] = useState<AgentToken[]>([]);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [policies, setPolicies] = useState<PathPolicy[]>([]);
  const [budget, setBudget] = useState<Budget | null>(null);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [webhooks, setWebhooks] = useState<WebhookRule[]>([]);
  const [actions, setActions] = useState<ExternalAction[]>([]);
  const [humanUsers, setHumanUsers] = useState<HumanUser[]>([]);
  const [audit, setAudit] = useState<AuditPayload>({ batches: [], diffs: [] });
  const [timeline, setTimeline] = useState<TimelineItem[]>([]);
  const [tab, setTab] = useState<Tab>("situation");
  const tabBarRef = useRef<HTMLDivElement>(null);
  const tabIndicator = useSlidingIndicator(tabBarRef, tab);
  const [selectedDocKey, setSelectedDocKey] = useState<string | null>(null);
  const [selectedDoc, setSelectedDoc] = useState<DocumentDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [ready, setReady] = useState<ReadyInfo | null>(null);
  const [serverOnline, setServerOnline] = useState<boolean | null>(null);
  const [live, setLive] = useState(false);
  const [lastEvent, setLastEvent] = useState<StreamEvent | null>(null);

  const [showCreateWorkspace, setShowCreateWorkspace] = useState(false);
  const [newWorkspaceName, setNewWorkspaceName] = useState("");
  const [newWorkspaceTier, setNewWorkspaceTier] = useState("developer_5usd");
  const [creatingWorkspace, setCreatingWorkspace] = useState(false);

  const [showCreateTask, setShowCreateTask] = useState(false);
  // Getting-started checklist (signed-in people only): refresh and reopen.
  const [onboardingTick, setOnboardingTick] = useState(0);
  const [checklistOpen, setChecklistOpen] = useState(false);
  const [mintSignal, setMintSignal] = useState(0);
  const [creatingTask, setCreatingTask] = useState(false);
  const [taskDetail, setTaskDetail] = useState<TaskDetail | null>(null);
  const [taskBusy, setTaskBusy] = useState(false);

  const [slotEditor, setSlotEditor] = useState<Slot | null>(null);
  const [slotState, setSlotState] = useState("{}");
  const [slotNote, setSlotNote] = useState("");
  const [slotLock, setSlotLock] = useState(true);
  const [slotBusy, setSlotBusy] = useState(false);
  const [slotError, setSlotError] = useState<string | null>(null);

  const [keyReveal, setKeyReveal] = useState<KeyReveal | null>(null);
  const [copied, setCopied] = useState(false);

  const refreshRef = useRef<(quiet?: boolean) => void>(() => {});
  const lastEventIdRef = useRef<number | null>(null);
  const selectedWorkspace = useMemo(
    () => workspaces.find((workspace) => workspace.id === selectedId) ?? null,
    [workspaces, selectedId],
  );
  const approvals = tasks.filter((task) =>
    ["AWAITING_HUMAN", "ESCALATED_TO_HUMAN"].includes(task.status),
  );

  useEffect(() => {
    const storedBase = localStorage.getItem(BASE_KEY);
    const devDefault =
      window.location.port === "3000" ? "http://localhost:8000" : window.location.origin;
    const base = storedBase || devDefault;
    const token =
      sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem(TOKEN_KEY) || "";
    const session = localStorage.getItem(SESSION_KEY) || "";
    setApiBase(base);
    setAdminToken(token);
    setSessionToken(session);
    setMounted(true);
    void probe(base);
    if (session) {
      void connectWithSession(base, session, { silent: true });
    } else if (token) {
      void connectWith(base, token, { silent: true });
    } else {
      window.location.replace("/dashboard/login/");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 6000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setShowCreateWorkspace(false);
        setShowCreateTask(false);
        setTaskDetail(null);
        setSlotEditor(null);
        setKeyReveal(null);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  useEffect(() => {
    refreshRef.current = (quiet = true) => {
      if (!selectedId) return;
      if (quiet) void refreshLive(selectedId);
      else void loadWorkspace(selectedId, false);
    };
  });

  useEffect(() => {
    if (connected && selectedId) {
      setLoading(true);
      setStats(null);
      setTasks([]);
      setSlots([]);
      setDocuments([]);
      setAgents([]);
      setProposals([]);
      setPolicies([]);
      setBudget(null);
      setUsage(null);
      setWebhooks([]);
      setActions([]);
      setHumanUsers([]);
      setAudit({ batches: [], diffs: [] });
      setTimeline([]);
      setSelectedDoc(null);
      setSelectedDocKey(null);
      lastEventIdRef.current = null;
      void loadWorkspace(selectedId, false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connected, selectedId]);

  useEffect(() => {
    if (!connected || !selectedId || !adminToken || sessionMode) {
      setLive(false);
      return;
    }
    const controller = new AbortController();
    let stopped = false;
    let retry: number | undefined;
    let refresh: number | undefined;

    const scheduleRefresh = () => {
      if (document.visibilityState !== "visible") return;
      window.clearTimeout(refresh);
      refresh = window.setTimeout(() => refreshRef.current(true), 1200);
    };

    async function connectStream() {
      if (stopped) return;
      try {
        const headers: Record<string, string> = {
          Authorization: `Bearer ${adminToken}`,
        };
        if (lastEventIdRef.current !== null) {
          headers["Last-Event-ID"] = String(lastEventIdRef.current);
        }
        const response = await fetch(
          `${normalizeBase(apiBase)}/v1/admin/workspaces/${selectedId}/stream?tables=context_slots,collab_tasks,shared_documents,timeline_streams`,
          {
            headers,
            signal: controller.signal,
          },
        );
        if (!response.ok || !response.body) {
          throw new Error(`SSE ${response.status}`);
        }
        setLive(true);
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!stopped) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const frames = buffer.split("\n\n");
          buffer = frames.pop() ?? "";
          for (const frame of frames) {
            if (!frame.includes("event: workspace")) continue;
            const dataLine = frame
              .split("\n")
              .find((line) => line.startsWith("data: "));
            if (!dataLine) continue;
            try {
              const event = JSON.parse(dataLine.slice(6)) as StreamEvent;
              setLastEvent(event);
              scheduleRefresh();
            } catch {
              // Ignore malformed frames.
            }
          }
        }
        if (!stopped) {
          setLive(false);
          retry = window.setTimeout(connectStream, 3000);
        }
      } catch (caught) {
        if (stopped) return;
        setLive(false);
        retry = window.setTimeout(connectStream, 3000);
      }
    }

    void connectStream();
    return () => {
      stopped = true;
      controller.abort();
      window.clearTimeout(retry);
      window.clearTimeout(refresh);
      setLive(false);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connected, selectedId, adminToken, apiBase, sessionMode]);

  async function request<T>(path: string, init?: RequestInit): Promise<T> {
    const headers = new Headers(init?.headers);
    const credential = sessionMode ? sessionToken : adminToken;
    headers.set("Authorization", `Bearer ${credential}`);
    if (init?.body) headers.set("Content-Type", "application/json");
    const effectivePath =
      sessionMode && selectedId
        ? sessionPath(path, selectedId, (init?.method ?? "GET").toUpperCase())
        : path;
    const response = await fetch(`${normalizeBase(apiBase)}${effectivePath}`, {
      ...init,
      headers,
      signal: AbortSignal.timeout(15000),
    });
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try {
        const body = await response.json();
        message = body?.error?.message ?? message;
      } catch {
        // Keep the HTTP status.
      }
      throw new Error(message);
    }
    return (await response.json()) as T;
  }

  const api: ApiRequest = request;

  // Opening the audit trail is the checklist's last step; report it once.
  const auditVisited = useRef(false);
  useEffect(() => {
    if (!sessionMode || tab !== "audit" || auditVisited.current) return;
    auditVisited.current = true;
    void api("/v1/human/onboarding", {
      method: "POST",
      body: JSON.stringify({ action: "complete", step: "view_audit" }),
    })
      .then(() => setOnboardingTick((value) => value + 1))
      .catch(() => {
        // Optional bookkeeping; never interrupt the console.
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, sessionMode]);

  async function probe(base: string) {
    try {
      const response = await fetch(`${normalizeBase(base)}/readyz`, {
        signal: AbortSignal.timeout(5000),
      });
      const body = (await response.json()) as ReadyInfo;
      setReady(body);
      setServerOnline(response.ok && body.database === "ok");
    } catch {
      setServerOnline(false);
      setReady(null);
    }
  }

  async function connectWith(
    base: string,
    token: string,
    options: { silent?: boolean } = {},
  ) {
    const cleanBase = normalizeBase(base);
    const cleanToken = token.trim();
    setError(null);
    if (!cleanBase) {
      setError(t("API endpoint is required."));
      return;
    }
    if (!cleanToken) {
      setError(t("Admin token is required. Paste the ADMIN_TOKEN from the server."));
      return;
    }
    if (/\s/.test(token)) {
      setError(t("Admin token contains spaces. Paste it without spaces or line breaks."));
      return;
    }
    if (cleanToken.length < 8) {
      setError(t("Admin token is too short to be valid."));
      return;
    }
    setConnecting(true);
    try {
      const response = await fetch(`${cleanBase}/v1/admin/workspaces`, {
        headers: { Authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(20000),
      });
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) {
          throw new Error(
            t("Admin token was rejected. It does not match this server's ADMIN_TOKEN."),
          );
        }
        if (response.status === 404) {
          throw new Error(
            t("Admin API not found at this endpoint. Check the API endpoint URL."),
          );
        }
        let message = `HTTP ${response.status}`;
        try {
          const body = await response.json();
          message = body?.error?.message ?? message;
        } catch {
          // Keep the HTTP status.
        }
        throw new Error(message);
      }
      const list = (await response.json()) as WorkspaceSummary[];
      setWorkspaces(list);
      setConnected(true);
      setSessionMode(false);
      setSessionToken("");
      setSessionUser(null);
      setApiBase(cleanBase);
      setAdminToken(cleanToken);
      localStorage.setItem(BASE_KEY, cleanBase);
      sessionStorage.setItem(TOKEN_KEY, cleanToken);
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(SESSION_KEY);
      setSelectedId((current) =>
        current && list.some((workspace) => workspace.id === current)
          ? current
          : list[0]?.id ?? null,
      );
      if (!options.silent) setNotice(t("Connected to the AgentGraph control plane."));
      void probe(cleanBase);
    } catch (caught) {
      setConnected(false);
      const message = caught instanceof Error ? caught.message : t("Unable to connect.");
      setError(
        message === "Failed to fetch"
          ? t("Could not reach the API endpoint. Check the URL, CORS, and network.")
          : message,
      );
    } finally {
      setConnecting(false);
    }
  }

  function applyWorkspaceSession(
    base: string,
    token: string,
    session: AuthSession,
  ) {
    const workspace = session.workspace;
    const summary: WorkspaceSummary = {
      ...workspace,
      documents: 0,
      slots: 0,
      tasks: 0,
      timeline_events: 0,
      last_activity_at: null,
    };
    setWorkspaces([summary]);
    setSelectedId(workspace.id);
    setApiBase(base);
    setAdminToken("");
    setSessionToken(token);
    setSessionUser(session.user);
    setSessionMode(true);
    setConnected(true);
    setError(null);
    localStorage.setItem(BASE_KEY, normalizeBase(base));
    localStorage.setItem(SESSION_KEY, token);
    sessionStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(TOKEN_KEY);
    void probe(base);
  }

  async function connectWithSession(
    base: string,
    token: string,
    options: { silent?: boolean } = {},
  ) {
    setConnecting(true);
    setError(null);
    try {
      const response = await fetch(`${normalizeBase(base)}/v1/auth/me`, {
        headers: { Authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(20000),
      });
      if (!response.ok) throw new Error(t("Your session expired. Sign in again."));
      const me = (await response.json()) as {
        user: HumanUser;
        workspace: AuthSession["workspace"];
        role: string;
      };
      applyWorkspaceSession(base, token, {
        token,
        expires_at: "",
        user: me.user,
        workspace: me.workspace,
      });
      if (!options.silent) {
        setNotice(t("Signed in as {email}.", { email: me.user.email }));
      }
    } catch (caught) {
      setConnected(false);
      setSessionMode(false);
      localStorage.removeItem(SESSION_KEY);
      setError(
        caught instanceof Error ? caught.message : t("Unable to sign in."),
      );
    } finally {
      setConnecting(false);
    }
  }

  async function refreshWorkspaces() {
    if (sessionMode) {
      // The signed-in session has exactly one workspace; loadWorkspace keeps
      // its counts current without resetting them to zero here.
      return;
    }
    try {
      const list = await api<WorkspaceSummary[]>("/v1/admin/workspaces");
      setWorkspaces(list);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Unable to refresh workspaces."));
    }
  }

  async function loadWorkspace(id: string, quiet: boolean) {
    if (!quiet) setLoading(true);
    const emergency = window.setTimeout(() => setLoading(false), 8000);
    const failures: string[] = [];

    async function loadOne<T>(
      label: string,
      promise: Promise<T>,
      apply: (value: T) => void,
    ) {
      try {
        apply(await promise);
      } catch (caught) {
        failures.push(
          `${label}: ${caught instanceof Error ? caught.message : "request failed"}`,
        );
        if (label === "stats") setLoading(false);
      }
    }

    await Promise.all([
      loadOne(
        "stats",
        api<Stats>(`/v1/admin/workspaces/${id}/stats`),
        (nextStats) => {
          setStats(nextStats);
          setLoading(false);
          setWorkspaces((current) =>
            current.map((workspace) =>
              workspace.id === id
                ? {
                    ...workspace,
                    documents: nextStats.documents,
                    slots: nextStats.slots,
                    tasks: nextStats.tasks,
                    timeline_events: nextStats.timeline_events,
                    last_activity_at: nextStats.last_activity_at,
                  }
                : workspace,
            ),
          );
        },
      ),
      loadOne(
        "tasks",
        api<{ items: TaskSummary[] }>(
          `/v1/admin/workspaces/${id}/tasks?limit=500`,
        ),
        (value) => setTasks(value.items),
      ),
      loadOne(
        "slots",
        api<{ items: Slot[] }>(`/v1/admin/workspaces/${id}/slots?limit=200`),
        (value) => setSlots(value.items),
      ),
      loadOne(
        "documents",
        api<DocumentPage>(
          `/v1/admin/workspaces/${id}/documents?${documentQuery(100, null)}`,
        ),
        (value) => {
          setDocuments(value.items);
          setDocTotal(value.total ?? value.items.length);
          setDocCursor(value.next_cursor ?? null);
        },
      ),
      loadOne(
        "agent-tokens",
        api<{ items: AgentToken[] }>(
          `/v1/admin/workspaces/${id}/agent-tokens`,
        ),
        (value) => setAgents(value.items),
      ),
      loadOne(
        "audit",
        api<AuditPayload>(`/v1/admin/workspaces/${id}/audit?limit=20`),
        (value) => setAudit(value),
      ),
      loadOne(
        "timeline",
        api<{ items: TimelineItem[] }>(
          `/v1/admin/workspaces/${id}/timeline?limit=100&hours=168`,
        ),
        (value) => setTimeline(value.items),
      ),
      loadOne(
        "proposals",
        api<{ items: Proposal[] }>(
          `/v1/admin/workspaces/${id}/proposals?limit=100`,
        ),
        (value) => setProposals(value.items),
      ),
      loadOne(
        "policies",
        api<{ items: PathPolicy[] }>(
          `/v1/admin/workspaces/${id}/policies`,
        ),
        (value) => setPolicies(value.items),
      ),
      loadOne(
        "budget",
        api<Budget>(`/v1/admin/workspaces/${id}/budget`),
        (value) => setBudget(value),
      ),
      loadOne(
        "usage",
        api<Usage>(`/v1/admin/workspaces/${id}/usage`),
        (value) => setUsage(value),
      ),
      loadOne(
        "webhooks",
        api<{ items: WebhookRule[] }>(
          `/v1/admin/workspaces/${id}/webhooks`,
        ),
        (value) => setWebhooks(value.items),
      ),
      loadOne(
        "actions",
        api<{ items: ExternalAction[] }>(
          `/v1/admin/workspaces/${id}/external-actions`,
        ),
        (value) => setActions(value.items),
      ),
      loadOne(
        "human-users",
        api<{ items: HumanUser[] }>(
          `/v1/admin/workspaces/${id}/human-users`,
        ),
        (value) => setHumanUsers(value.items),
      ),
    ]);
    window.clearTimeout(emergency);
    setLoading(false);
    if (!quiet || failures.length) {
      setError(failures[0] ?? null);
    }
  }

  async function refreshLive(id: string) {
    const jobs: Promise<unknown>[] = [
      api<Stats>(`/v1/admin/workspaces/${id}/stats`).then((nextStats) => {
        setStats(nextStats);
        setLoading(false);
        setWorkspaces((current) =>
          current.map((workspace) =>
            workspace.id === id
              ? {
                  ...workspace,
                  documents: nextStats.documents,
                  slots: nextStats.slots,
                  tasks: nextStats.tasks,
                  timeline_events: nextStats.timeline_events,
                  last_activity_at: nextStats.last_activity_at,
                }
              : workspace,
          ),
        );
      }),
      api<{ items: TaskSummary[] }>(
        `/v1/admin/workspaces/${id}/tasks?limit=500`,
      ).then((value) => setTasks(value.items)),
      api<{ items: Slot[] }>(
        `/v1/admin/workspaces/${id}/slots?limit=200`,
      ).then((value) => setSlots(value.items)),
    ];
    if (tab === "blackboard") {
      jobs.push(
        api<{ items: TimelineItem[] }>(
          `/v1/admin/workspaces/${id}/timeline?limit=100&hours=168`,
        ).then((value) => setTimeline(value.items)),
      );
    }
    if (tab === "documents") {
      jobs.push(
        api<DocumentPage>(
          `/v1/admin/workspaces/${id}/documents?${documentQuery(100, null)}`,
        ).then((value) => {
          setDocuments(value.items);
          setDocTotal(value.total ?? value.items.length);
          setDocCursor(value.next_cursor ?? null);
        }),
      );
    }
    if (tab === "audit") {
      jobs.push(
        api<AuditPayload>(
          `/v1/admin/workspaces/${id}/audit?limit=20`,
        ).then((value) => setAudit(value)),
      );
    }
    await Promise.allSettled(jobs);
  }

  function documentQuery(
    limit: number,
    cursor: string | null,
    filters = docFilters,
  ): string {
    const params = new URLSearchParams({ limit: String(limit) });
    if (filters.namespace.trim()) {
      params.set("namespace", filters.namespace.trim());
    }
    if (filters.verificationStatus) {
      params.set("verification_status", filters.verificationStatus);
    }
    if (filters.tainted) params.set("tainted", filters.tainted);
    if (filters.updatedAfter) {
      params.set("updated_after", `${filters.updatedAfter}T00:00:00Z`);
    }
    if (filters.updatedBefore) {
      params.set("updated_before", `${filters.updatedBefore}T23:59:59Z`);
    }
    params.set("sort", filters.sort);
    if (cursor) params.set("cursor", cursor);
    return params.toString();
  }

  async function applyDocumentFilters(next: typeof docFilters) {
    setDocFilters(next);
    if (!selectedId) return;
    setDocLoadingMore(true);
    try {
      const page = await api<DocumentPage>(
        `/v1/admin/workspaces/${selectedId}/documents?${documentQuery(100, null, next)}`,
      );
      setDocuments(page.items);
      setDocTotal(page.total ?? page.items.length);
      setDocCursor(page.next_cursor ?? null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Unable to load documents."));
    } finally {
      setDocLoadingMore(false);
    }
  }

  async function loadMoreDocuments() {
    if (!selectedId || !docCursor || docLoadingMore) return;
    setDocLoadingMore(true);
    try {
      const page = await api<DocumentPage>(
        `/v1/admin/workspaces/${selectedId}/documents?${documentQuery(100, docCursor)}`,
      );
      setDocuments((current) => {
        const seen = new Set(current.map((item) => item.id));
        return [...current, ...page.items.filter((item) => !seen.has(item.id))];
      });
      setDocCursor(page.next_cursor ?? null);
      setDocTotal(page.total ?? docTotal);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Unable to load more documents."));
    } finally {
      setDocLoadingMore(false);
    }
  }

  async function openDocument(docKey: string) {
    if (!selectedId) return;
    setTab("documents");
    setSelectedDocKey(docKey);
    try {
      const doc = await api<DocumentDetail>(
        `/v1/admin/workspaces/${selectedId}/documents/${docKey
          .split("/")
          .map(encodeURIComponent)
          .join("/")}`,
      );
      setSelectedDoc(doc);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Unable to open document."));
    }
  }

  async function disconnect() {
    const wasSession = sessionMode;
    if (sessionMode && sessionToken) {
      try {
        await fetch(`${normalizeBase(apiBase)}/v1/auth/logout`, {
          method: "POST",
          headers: { Authorization: `Bearer ${sessionToken}` },
          signal: AbortSignal.timeout(10000),
        });
      } catch {
        // Local sign-out still clears the browser session.
      }
    }
    sessionStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(SESSION_KEY);
    setConnected(false);
    setSessionMode(false);
    setSessionToken("");
    setSessionUser(null);
    setAdminToken("");
    setWorkspaces([]);
    setSelectedId(null);
    setStats(null);
    setTasks([]);
    setSlots([]);
    setDocuments([]);
    setAgents([]);
    setProposals([]);
    setPolicies([]);
    setBudget(null);
    setUsage(null);
    setWebhooks([]);
    setActions([]);
    setHumanUsers([]);
    setAudit({ batches: [], diffs: [] });
    setTimeline([]);
    setKeyReveal(null);
    setTaskDetail(null);
    setLive(false);
    window.location.assign(wasSession ? "/dashboard/login/" : "/dashboard/connect/");
  }

  async function createWorkspace(event: React.FormEvent) {
    event.preventDefault();
    if (!newWorkspaceName.trim()) return;
    setCreatingWorkspace(true);
    setError(null);
    try {
      const created = await api<{
        workspace: { id: string; name: string };
        api_key: string;
      }>("/v1/admin/workspaces", {
        method: "POST",
        body: JSON.stringify({
          name: newWorkspaceName.trim(),
          tier: newWorkspaceTier,
        }),
      });
      setShowCreateWorkspace(false);
      setNewWorkspaceName("");
      setKeyReveal({
        key: created.api_key,
        name: created.workspace.name,
        kind: "workspace",
      });
      await refreshWorkspaces();
      setSelectedId(created.workspace.id);
      setNotice(`Workspace “${created.workspace.name}” created.`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Unable to create workspace."));
    } finally {
      setCreatingWorkspace(false);
    }
  }

  async function rotateWorkspaceKey() {
    if (!selectedWorkspace) return;
    const confirmed = window.confirm(
      `Rotate the commander key for “${selectedWorkspace.name}”? Existing integrations using it will stop working.`,
    );
    if (!confirmed) return;
    try {
      const rotated = await api<{ api_key: string }>(
        `/v1/admin/workspaces/${selectedWorkspace.id}/rotate-key`,
        { method: "POST" },
      );
      setKeyReveal({
        key: rotated.api_key,
        name: selectedWorkspace.name,
        kind: "rotated",
      });
      setNotice(t("Commander key rotated."));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Unable to rotate key."));
    }
  }

  async function createTask(payload: Record<string, unknown>) {
    if (!selectedId) return;
    setCreatingTask(true);
    try {
      await api(`/v1/admin/workspaces/${selectedId}/tasks`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      setShowCreateTask(false);
      setNotice(t("Task created and ready for claim."));
      await loadWorkspace(selectedId, true);
      await refreshWorkspaces();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Unable to create task."));
    } finally {
      setCreatingTask(false);
    }
  }

  async function openTask(task: TaskSummary) {
    if (!selectedId) return;
    try {
      const detail = await api<TaskDetail>(
        `/v1/admin/workspaces/${selectedId}/tasks/${task.task_key}`,
      );
      setTaskDetail(detail);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Unable to load task."));
    }
  }

  async function reviewTask(decision: "approve" | "reject", feedback: string) {
    if (!selectedId || !taskDetail) return;
    setTaskBusy(true);
    try {
      const result = await api<{ escalated: boolean; task: TaskDetail }>(
        `/v1/admin/workspaces/${selectedId}/tasks/${taskDetail.task_key}/review`,
        {
          method: "POST",
          body: JSON.stringify({
            decision,
            feedback: feedback || undefined,
            expected_version: taskDetail.version,
          }),
        },
      );
      setTaskDetail(null);
      await loadWorkspace(selectedId, true);
      if (result.escalated) {
        setNotice(t("Task escalated to human review."));
      } else {
        setNotice(`Task ${decision === "approve" ? "approved" : "rejected"}.`);
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Unable to review task."));
    } finally {
      setTaskBusy(false);
    }
  }

  function openSlot(slot: Slot) {
    setSlotEditor(slot);
    setSlotState(pretty(slot.current_state));
    setSlotNote(slot.human_note ?? "");
    setSlotLock(true);
    setSlotError(null);
  }

  async function saveSlot(unlock = false) {
    if (!selectedId || !slotEditor) return;
    setSlotBusy(true);
    setSlotError(null);
    try {
      let state: Record<string, unknown> | undefined;
      if (!unlock) {
        const parsed = JSON.parse(slotState || "{}");
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
          throw new Error(t("State must be a JSON object"));
        }
        state = parsed as Record<string, unknown>;
      }
      await api(
        `/v1/admin/workspaces/${selectedId}/slots/${slotEditor.slot_key
          .split("/")
          .map(encodeURIComponent)
          .join("/")}`,
        {
          method: "PUT",
          body: JSON.stringify({
            state,
            expected_version: slotEditor.version,
            human_note: slotNote || undefined,
            ...(unlock ? { unlock: true } : { lock: slotLock }),
          }),
        },
      );
      setSlotEditor(null);
      await loadWorkspace(selectedId, true);
      setNotice(
        unlock
          ? t("Human lock released; agents may write again.")
          : t("State override saved and human sovereignty locked."),
      );
    } catch (caught) {
      setSlotError(caught instanceof Error ? caught.message : t("Unable to update slot."));
    } finally {
      setSlotBusy(false);
    }
  }

  async function copyKey() {
    if (!keyReveal) return;
    try {
      await navigator.clipboard.writeText(keyReveal.key);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setError(t("Clipboard access was blocked; select and copy the key manually."));
    }
  }

  function handleTabKeyDown(
    event: React.KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) {
    let nextIndex = index;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      nextIndex = (index + 1) % tabs.length;
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      nextIndex = (index - 1 + tabs.length) % tabs.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = tabs.length - 1;
    } else {
      return;
    }
    event.preventDefault();
    const next = tabs[nextIndex];
    setTab(next.id);
    document.getElementById(`view-tab-${next.id}`)?.focus();
  }

  async function reopenChecklist() {
    try {
      await api("/v1/human/onboarding", {
        method: "POST",
        body: JSON.stringify({ action: "reopen" }),
      });
    } catch {
      // Still show it locally; the next load re-reads the stored state.
    }
    setChecklistOpen(true);
    setOnboardingTick((tick) => tick + 1);
  }

  function navigateOnboarding(step: OnboardingStepId) {
    if (step === "create_token") {
      setTab("agents");
      setMintSignal((value) => value + 1);
    } else if (step === "connect_mcp") {
      setTab("agents");
      window.open("/docs/mcp/", "_blank", "noopener");
    } else if (step === "first_content") {
      setTab("tasks");
      setShowCreateTask(true);
    } else if (step === "human_review") {
      setTab("tasks");
    } else if (step === "view_audit") {
      setTab("audit");
    }
  }

  const tabs: Array<{ id: Tab; label: string; count?: number; alert?: number; icon: JSX.Element }> = [
    { id: "situation", label: t("Situation"), icon: <IconPulse /> },
    { id: "blackboard", label: t("Blackboard"), count: slots.length, icon: <IconBoard /> },
    { id: "tasks", label: t("Tasks"), count: tasks.length, alert: approvals.length, icon: <IconTasks /> },
    { id: "documents", label: t("Documents"), count: docTotal, icon: <IconDocuments /> },
    { id: "agents", label: t("Agents"), count: agents.length, icon: <IconAgents /> },
    {
      id: "governance",
      label: t("Governance"),
      count: proposals.filter((item) => item.status === "PENDING").length + actions.filter((item) => item.status === "PENDING_APPROVAL").length,
      alert: proposals.filter((item) => item.status === "PENDING").length,
      icon: <IconLayers />,
    },
    { id: "audit", label: t("Audit"), count: audit.total_batches ?? audit.batches.length, icon: <IconAudit /> },
  ];

  if (!mounted) {
    return (
      <div className="boot">
        <div className="boot-mark"><IconGraph size={30} /></div>
        <span>{t("Loading console…")}</span>
      </div>
    );
  }

  return (
    <div className="shell" data-console-build={CONSOLE_BUILD}>
      <a className="skip-link" href="#main-content">
        {t("Skip to workspace content")}
      </a>
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark"><IconGraph /></div>
          <div>
            <div className="brand-name">AgentGraph<span className="accent">.</span></div>
            <div className="brand-sub">{t("Human · Agent Blackboard")}</div>
          </div>
        </div>

        <div className={`server-chip ${serverOnline === false ? "offline" : ""}`}>
          <span className={`status-dot ${serverOnline === true ? "live" : serverOnline === false ? "dead" : ""}`} />
          <span>
            {serverOnline === null
              ? t("Checking gateway…")
              : serverOnline
                ? `${t("Gateway online")}${ready ? ` · v${ready.version}` : ""}`
                : t("Gateway unreachable")}
          </span>
        </div>

        <div className="sidebar-head">
          <span>{t("Workspaces")}</span>
          {!sessionMode && (
            <button
              type="button"
              className="icon-button"
              onClick={() => setShowCreateWorkspace(true)}
              disabled={!connected}
              aria-label={t("Create workspace")}
              title={t("Create workspace")}
            >
              <IconPlus size={15} />
            </button>
          )}
        </div>

        <nav className="workspace-list" aria-label={t("Workspaces")}>
          {!connected && <div className="list-hint">{t("Connect below to load workspaces.")}</div>}
          {connected && workspaces.length === 0 && (
            <div className="list-hint">{t("No workspaces yet. Create one to begin.")}</div>
          )}
          {workspaces.map((workspace) => (
            <button
              key={workspace.id}
              type="button"
              className={`workspace-item ${workspace.id === selectedId ? "active" : ""}`}
              onClick={() => setSelectedId(workspace.id)}
              aria-current={workspace.id === selectedId ? "true" : undefined}
            >
              <span className="workspace-avatar">{workspace.name.slice(0, 1).toUpperCase()}</span>
              <span className="workspace-meta">
                <span className="workspace-name">{workspace.name}</span>
                <span className="workspace-stats">
                  {t("{docs} docs · {tasks} tasks", {
                    docs: formatNumber(workspace.documents),
                    tasks: formatNumber(workspace.tasks),
                  })}
                </span>
              </span>
              <span className="workspace-tier">
                {workspace.tier === "pro"
                  ? t("Pro")
                  : workspace.tier === "free"
                    ? t("Free")
                    : t("$5")}
              </span>
            </button>
          ))}
        </nav>

        <nav className="sidebar-help" aria-label={t("Help")}>
          <span>{t("Help")}</span>
          <a href="/docs/quickstart/" target="_blank" rel="noreferrer">{t("Quickstart")}</a>
          <a href="/docs/console/" target="_blank" rel="noreferrer">{t("Console tutorial")}</a>
          <a href="/docs/mcp/" target="_blank" rel="noreferrer">{t("MCP setup")}</a>
        </nav>

        {sessionMode ? (
          <div className="session-card">
            <div className="session-card-head">
              <span className="session-avatar">
                {(sessionUser?.display_name || sessionUser?.email || "?").slice(0, 1).toUpperCase()}
              </span>
              <span>
                <strong>{sessionUser?.display_name ?? sessionUser?.email}</strong>
                <small>{selectedWorkspace?.name}</small>
              </span>
            </div>
            <button type="button" className="button subtle full" onClick={() => void disconnect()}>
              {t("Sign out")}
            </button>
          </div>
        ) : (
          <div className="session-card">
            <div className="session-card-head">
              <span className="session-avatar"><IconKey size={14} /></span>
              <span>
                <strong>{t("Operator session")}</strong>
                <small>{t("Admin token")}</small>
              </span>
            </div>
            <a className="button subtle full" href="/dashboard/connect/">
              {t("Connection settings")}
            </a>
            <button type="button" className="button subtle full" onClick={() => void disconnect()}>
              {t("Sign out")}
            </button>
          </div>
        )}
      </aside>

      <main className="main" id="main-content" tabIndex={-1}>
        <header className="topbar">
          <div className="breadcrumbs">
            <span>{t("Console")}</span>
            <span className="crumb-sep">/</span>
            <span className="crumb-current">{selectedWorkspace?.name ?? t("Overview")}</span>
            {connected && !sessionMode && (
              <span className={`live-pill ${live ? "on" : ""}`}>
                <span className={`status-dot ${live ? "live" : "dead"}`} />
                {live ? t("LIVE") : t("RECONNECTING")}
              </span>
            )}
          </div>
          <div className="topbar-actions">
            <LanguageToggle />
            {sessionMode && (
              <button
                type="button"
                className="button ghost"
                onClick={() => void reopenChecklist()}
              >
                {t("Getting started")}
              </button>
            )}
            <a className="button ghost" href="/docs/" target="_blank" rel="noreferrer">
              {t("Docs")}
            </a>
            <button
              type="button"
              className="button ghost"
              onClick={() => {
                void refreshWorkspaces();
                if (selectedId) void loadWorkspace(selectedId, false);
              }}
              disabled={!connected}
            >
              <IconRefresh /> {t("Refresh")}
            </button>
            <button
              type="button"
              className="button ghost"
              onClick={() => setShowCreateTask(true)}
              disabled={!connected || !selectedId}
              data-modal-trigger="new-task"
            >
              <IconPlus /> {t("New task")}
            </button>
          </div>
        </header>

        <div className="content">
          {error && (
            <div className="banner error" role="alert">
              <strong>{t("Something went wrong.")}</strong> {error}
              <button
                type="button"
                className="banner-close"
                onClick={() => setError(null)}
                aria-label={t("Dismiss error")}
              >
                <IconClose size={15} />
              </button>
            </div>
          )}
          {notice && (
            <div className="banner notice" role="status">
              {notice}
              <button
                type="button"
                className="banner-close"
                onClick={() => setNotice(null)}
                aria-label={t("Dismiss notification")}
              >
                <IconClose size={15} />
              </button>
            </div>
          )}

          {!connected ? (
            <section className="welcome">
              <div className="eyebrow">{t("Human-in-the-loop shared blackboard")}</div>
              <h1>
                {t("Agents do the work.")}<br />
                <span className="accent">{t("Humans hold the line.")}</span>
              </h1>
              <p className="lede">
                {t("Shared documents, a live state grid, TTL task leases, progressive L1→L2 handoffs, a ping-pong circuit breaker, and session-batch undo — all in one PostgreSQL core with MCP-native access.")}
              </p>
              <div className="welcome-grid">
                <div className="welcome-card">
                  <span className="step">01</span>
                  <h3>{t("Connect the commander")}</h3>
                  <p>{t("Enter the API endpoint and ADMIN_TOKEN from your deployment in the sidebar.")}</p>
                </div>
                <div className="welcome-card">
                  <span className="step">02</span>
                  <h3>{t("Mint scoped agents")}</h3>
                  <p>{t("Give each runtime a role and namespace sandbox: SCOUT, ANALYST, AUDITOR, EXECUTOR.")}</p>
                </div>
                <div className="welcome-card">
                  <span className="step">03</span>
                  <h3>{t("Supervise live")}</h3>
                  <p>{t("Approve escalated tasks, override locked state, inspect L2 artifacts, undo whole sessions.")}</p>
                </div>
              </div>
              <div className="welcome-foot">
                <span className="pill">{t("Lease locks")}</span>
                <span className="pill">{t("L1 / L2 handoff")}</span>
                <span className="pill">{t("Human sovereignty")}</span>
                <span className="pill">{t("LISTEN / NOTIFY")}</span>
                <span className="pill">{t("Batch undo")}</span>
              </div>
            </section>
          ) : !selectedWorkspace ? (
            <section className="empty-state">
              <div className="empty-mark"><IconGraph size={34} /></div>
              <h2>{t("No workspace selected")}</h2>
              <p>{t("Create a workspace to provision a shared blackboard for your agent team.")}</p>
              <button type="button" className="button primary" onClick={() => setShowCreateWorkspace(true)}>
                <IconPlus /> {t("Create workspace")}
              </button>
            </section>
          ) : (
            <section className="workspace-view">
              <div className="workspace-hero">
                <div>
                  <div className="eyebrow">{t("Shared workspace")}</div>
                  <h1>{selectedWorkspace.name}</h1>
                  <div className="hero-meta">
                    <span className={`tier tier-${selectedWorkspace.tier}`}>{tierLabel(selectedWorkspace.tier)}</span>
                    <span>{t("Created")} {shortDate(selectedWorkspace.created_at)}</span>
                    <span>{t("Active")} {relativeTime(stats?.last_activity_at ?? selectedWorkspace.last_activity_at)}</span>
                    {stats && <span>{stats.embedding_provider} · {stats.embedding_dimensions}d</span>}
                    {lastEvent && <span>{t("Event")} {lastEvent.table}/{lastEvent.action}</span>}
                  </div>
                </div>
                {!sessionMode && (
                  <div className="hero-actions">
                    <button type="button" className="button ghost" onClick={rotateWorkspaceKey}>
                      <IconKey /> {t("Rotate commander key")}
                    </button>
                  </div>
                )}
              </div>

              <div className="stat-grid">
                <div className="stat-card" onPointerMove={spotlight}>
                  <div className="stat-icon mint"><IconDocuments /></div>
                  <div className="stat-label">{t("Shared documents")}</div>
                  <div className="stat-value"><AnimatedNumber value={stats?.documents ?? 0} format={formatNumber} /></div>
                  <div className="stat-foot">{t("frontmatter + Markdown · versioned")}</div>
                </div>
                <div className="stat-card" onPointerMove={spotlight}>
                  <div className="stat-icon sky"><IconTasks /></div>
                  <div className="stat-label">{t("Tasks on the board")}</div>
                  <div className="stat-value"><AnimatedNumber value={stats?.tasks ?? 0} format={formatNumber} /></div>
                  <div className="stat-foot">{t("claims, leases, review gates")}</div>
                </div>
                <div className="stat-card" onPointerMove={spotlight}>
                  <div className="stat-icon violet"><IconLayers /></div>
                  <div className="stat-label">{t("State slots")}</div>
                  <div className="stat-value"><AnimatedNumber value={stats?.slots ?? 0} format={formatNumber} /></div>
                  <div className="stat-foot">{t("human-locked live state")}</div>
                </div>
                <div className="stat-card" onPointerMove={spotlight}>
                  <div className="stat-icon amber"><IconAgents /></div>
                  <div className="stat-label">{t("Scoped agents")}</div>
                  <div className="stat-value"><AnimatedNumber value={stats?.agent_tokens ?? 0} format={formatNumber} /></div>
                  <div className="stat-foot">{t("Awaiting human review: {count}", { count: approvals.length })}</div>
                </div>
              </div>

              {sessionMode && (
                <GettingStarted
                  request={api}
                  refreshKey={onboardingTick}
                  forceOpen={checklistOpen}
                  onClosed={() => setChecklistOpen(false)}
                  onNavigate={navigateOnboarding}
                />
              )}

              <div
                ref={tabBarRef}
                className={`tab-bar ${tabIndicator.ready ? "has-indicator" : ""}`}
                role="tablist"
                aria-label={t("Workspace views")}
              >
                {tabs.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    role="tab"
                    id={`view-tab-${item.id}`}
                    aria-selected={tab === item.id}
                    aria-controls={`view-panel-${item.id}`}
                    tabIndex={tab === item.id ? 0 : -1}
                    className={`tab-button ${tab === item.id ? "active" : ""}`}
                    onClick={() => setTab(item.id)}
                    onKeyDown={(event) => handleTabKeyDown(event, tabs.indexOf(item))}
                  >
                    {item.icon}
                    <span>{item.label}</span>
                    {typeof item.count === "number" && item.count > 0 && (
                      <span className={`tab-count ${item.alert ? "alert" : ""}`}>{item.count}</span>
                    )}
                  </button>
                ))}
                {tabIndicator.ready && (
                  <span
                    className="tab-indicator"
                    aria-hidden="true"
                    style={{
                      width: tabIndicator.width - 16,
                      transform: `translateX(${tabIndicator.left + 8}px)`,
                    }}
                  />
                )}
              </div>

              <div
                className="tab-panel"
                role="tabpanel"
                id={`view-panel-${tab}`}
                aria-labelledby={`view-tab-${tab}`}
              >
              {loading && !stats ? (
                <div className="card skeleton-card">
                  <div className="skeleton line w40" />
                  <div className="skeleton line w70" />
                  <div className="skeleton line w55" />
                </div>
              ) : (
                <>
                  {tab === "blackboard" && (
                    <Blackboard
                      slots={slots}
                      tasks={tasks}
                      timeline={timeline}
                      namespaceCounts={stats?.document_namespaces ?? {}}
                      onOpenSlot={openSlot}
                      onOpenTask={openTask}
                    />
                  )}
                  {tab === "tasks" && (
                    <TaskBoard
                      tasks={tasks}
                      onOpen={openTask}
                      onCreate={() => setShowCreateTask(true)}
                    />
                  )}
                  {tab === "documents" && (
                    <DocumentsView
                      workspaceId={selectedWorkspace.id}
                      documents={documents}
                      total={docTotal}
                      namespaceCounts={stats?.document_namespaces ?? {}}
                      loadingMore={docLoadingMore}
                      onLoadMore={() => void loadMoreDocuments()}
                      filters={docFilters}
                      onFiltersChange={(next) => void applyDocumentFilters(next)}
                      selected={selectedDoc}
                      request={api}
                      onSelect={openDocument}
                      onUpdated={(doc) => {
                        setSelectedDoc(doc);
                        setSelectedDocKey(doc.doc_key);
                        setDocuments((current) => {
                          const exists = current.some(
                            (item) => item.doc_key === doc.doc_key,
                          );
                          if (!exists) setDocTotal((total) => total + 1);
                          return exists
                            ? current.map((item) =>
                                item.doc_key === doc.doc_key ? doc : item,
                              )
                            : [{ ...doc }, ...current];
                        });
                      }}
                      notify={setNotice}
                    />
                  )}
                  {tab === "agents" && (
                    <AgentsView
                      workspaceId={selectedWorkspace.id}
                      tokens={agents}
                      request={api}
                      onRefresh={async () => {
                        await loadWorkspace(selectedWorkspace.id, true);
                      }}
                      onKey={(key, name, token) =>
                        setKeyReveal({
                          key,
                          name,
                          kind: "agent",
                          role: token?.role_scope,
                          namespaces: token?.allowed_namespaces,
                        })
                      }
                      notify={setNotice}
                      openCreateSignal={mintSignal}
                    />
                  )}
                  {tab === "governance" && (
                    <GovernanceView
                      workspaceId={selectedWorkspace.id}
                      proposals={proposals}
                      policies={policies}
                      budget={budget}
                      usage={usage}
                      webhooks={webhooks}
                      actions={actions}
                      users={humanUsers}
                      request={api}
                      onRefresh={async () => {
                        await loadWorkspace(selectedWorkspace.id, true);
                      }}
                      notify={setNotice}
                    />
                  )}
                  {tab === "situation" && (
                    <SituationView
                      workspaceId={selectedWorkspace.id}
                      request={api}
                      notify={setNotice}
                    />
                  )}
                  {tab === "audit" && (
                    <AuditView
                      workspaceId={selectedWorkspace.id}
                      audit={audit}
                      request={api}
                      onRefresh={async () => {
                        await loadWorkspace(selectedWorkspace.id, true);
                      }}
                      notify={setNotice}
                    />
                  )}
                </>
              )}
              </div>
            </section>
          )}
        </div>
      </main>

      {showCreateWorkspace && (
        <Modal
          title={t("New workspace")}
          subtitle={t("An isolated blackboard: documents, tasks, slots, agents and audit.")}
          onClose={() => setShowCreateWorkspace(false)}
        >
          <form onSubmit={createWorkspace}>
            <label className="field">
              <span>{t("Name")}</span>
              <input
                value={newWorkspaceName}
                onChange={(event) => setNewWorkspaceName(event.target.value)}
                placeholder={t("Quant & Research Hub")}
                maxLength={128}
                autoFocus
                required
              />
            </label>
            <label className="field">
              <span>{t("Tier")}</span>
              <select value={newWorkspaceTier} onChange={(event) => setNewWorkspaceTier(event.target.value)}>
                <option value="developer_5usd">{t("Developer · $5")}</option>
                <option value="free">{t("Free")}</option>
                <option value="pro">{t("Pro")}</option>
              </select>
            </label>
            <div className="modal-actions">
              <button type="button" className="button ghost" onClick={() => setShowCreateWorkspace(false)}>
                {t("Cancel")}
              </button>
              <button type="submit" className="button primary" disabled={creatingWorkspace || !newWorkspaceName.trim()}>
                {creatingWorkspace ? <><Spinner /> {t("Creating…")}</> : t("Create workspace")}
              </button>
            </div>
          </form>
        </Modal>
      )}

      {showCreateTask && selectedWorkspace && (
        <CreateTaskModal
          busy={creatingTask}
          onClose={() => setShowCreateTask(false)}
          onCreate={createTask}
        />
      )}

      {taskDetail && (
        <TaskDetailModal
          task={taskDetail}
          busy={taskBusy}
          onClose={() => setTaskDetail(null)}
          onReview={reviewTask}
          onOpenArtifact={(uri) => {
            const docKey = uri.replace(/^doc:\/\/+/, "");
            void openDocument(docKey);
            setTaskDetail(null);
          }}
        />
      )}

      {slotEditor && (
        <Modal
          title={
            <span className="modal-title-row">
              <span className="mono">{slotEditor.slot_key}</span>
              <span className="version-chip">v{slotEditor.version}</span>
            </span>
          }
          subtitle={t("Human override is version-checked and broadcast as an interrupt.")}
          onClose={() => setSlotEditor(null)}
          wide
          footer={
            <>
              <button type="button" className="button ghost" onClick={() => setSlotEditor(null)}>
                {t("Cancel")}
              </button>
              {slotEditor.locked_by_human && (
                <button
                  type="button"
                  className="button danger"
                  onClick={() => void saveSlot(true)}
                  disabled={slotBusy}
                >
                  {slotBusy ? <Spinner /> : <IconUnlock />} {t("Unlock for agents")}
                </button>
              )}
              <button
                type="button"
                className="button primary"
                onClick={() => void saveSlot(false)}
                disabled={slotBusy}
              >
                {slotBusy ? <Spinner /> : <IconLock />} {t("Save human override")}
              </button>
            </>
          }
        >
          {slotError && <div className="inline-error">{slotError}</div>}
          <label className="field">
            <span>{t("Current state (JSON)")}</span>
            <textarea
              rows={10}
              className="mono-input"
              value={slotState}
              onChange={(event) => setSlotState(event.target.value)}
            />
          </label>
          <label className="field">
            <span>{t("Human note (broadcast to agents)")}</span>
            <input
              value={slotNote}
              onChange={(event) => setSlotNote(event.target.value)}
              placeholder={t("Risk off until CPI clears.")}
            />
          </label>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={slotLock}
              onChange={(event) => setSlotLock(event.target.checked)}
            />
            {t("Take human sovereignty lock (agents cannot write until unlocked)")}
          </label>
        </Modal>
      )}

      {keyReveal && (
        <Modal
          title={keyReveal.name}
          subtitle={
            keyReveal.kind === "agent"
              ? t("Agent token minted. Store it in the runtime now.")
              : keyReveal.kind === "rotated"
                ? t("Commander key rotated. Update existing integrations.")
                : t("Workspace provisioned. Store the commander key now.")
          }
          onClose={() => setKeyReveal(null)}
        >
          <div className="key-box">
            <code>{keyReveal.key}</code>
            <button type="button" className="button ghost" onClick={copyKey}>
              {copied ? t("Copied") : <><IconCopy /> {t("Copy")}</>}
            </button>
          </div>
          {keyReveal.kind === "agent" ? (
            <AgentConnect
              apiBase={apiBase}
              agentKey={keyReveal.key}
              agentName={keyReveal.name}
              role={keyReveal.role}
              namespaces={keyReveal.namespaces}
            />
          ) : (
            <div className="key-note">
              {t("Use it as")} <code>Authorization: Bearer …</code>.{" "}
              {t("Agent runtimes should instead receive a scoped")}{" "}
              <code>agt_</code> {t("token from the Agents tab.")}
            </div>
          )}
          <div className="modal-actions">
            <button
              type="button"
              className="button primary"
              onClick={() => {
                setKeyReveal(null);
                setOnboardingTick((tick) => tick + 1);
              }}
            >
              {t("I’ve stored it")}
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}
