"use client";

import { useEffect, useMemo, useState } from "react";

import { useI18n } from "../i18n";
import { MarkdownContent } from "./markdown";
import type {
  ApiRequest,
  DocumentDetail,
  DocumentSummary,
  DocumentVersion,
  DocumentVersionSummary,
} from "../types";
import { pretty, relativeTime } from "../types";
import {
  Empty,
  IconCheck,
  IconDocuments,
  IconLock,
  IconPlus,
  IconUnlock,
  IconX,
  Modal,
  Spinner,
} from "./ui";

function verificationTone(status: string): string {
  if (status === "VERIFIED") return "mint";
  if (status === "PENDING_REVIEW") return "amber";
  if (status === "DEPRECATED") return "rose";
  return "slate";
}

function docUrl(workspaceId: string, docKey: string): string {
  const path = docKey
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/");
  return `/v1/admin/workspaces/${workspaceId}/documents/${path}`;
}

export function DocumentsView({
  workspaceId,
  documents,
  selected,
  request,
  onSelect,
  onUpdated,
  notify,
}: {
  workspaceId: string;
  documents: DocumentSummary[];
  selected: DocumentDetail | null;
  request: ApiRequest;
  onSelect: (docKey: string) => void;
  onUpdated: (doc: DocumentDetail) => void;
  notify: (message: string) => void;
}) {
  const { t } = useI18n();
  const [mode, setMode] = useState<"preview" | "edit">("preview");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [frontmatterText, setFrontmatterText] = useState("{}");
  const [humanNote, setHumanNote] = useState("");
  const [lockDoc, setLockDoc] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [newKey, setNewKey] = useState("");
  const [newTitle, setNewTitle] = useState("");
  const [newContent, setNewContent] = useState("# New document\n\n");
  const [showHistory, setShowHistory] = useState(false);
  const [versions, setVersions] = useState<DocumentVersionSummary[]>([]);
  const [versionPreview, setVersionPreview] = useState<DocumentVersion | null>(
    null,
  );
  const [versionBusy, setVersionBusy] = useState(false);
  const [versionError, setVersionError] = useState<string | null>(null);

  useEffect(() => {
    if (!selected) return;
    setTitle(selected.title);
    setContent(selected.content);
    setFrontmatterText(pretty(selected.frontmatter));
    setHumanNote(selected.human_note ?? "");
    setLockDoc(selected.locked_by_human);
    setMode("preview");
    setError(null);
  }, [selected?.doc_key, selected?.version]);

  const grouped = useMemo(() => {
    const groups = new Map<string, DocumentSummary[]>();
    for (const doc of documents) {
      const list = groups.get(doc.namespace) ?? [];
      list.push(doc);
      groups.set(doc.namespace, list);
    }
    return Array.from(groups.entries()).sort(([a], [b]) => a.localeCompare(b));
  }, [documents]);

  async function save() {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      const frontmatter = JSON.parse(frontmatterText || "{}");
      if (!frontmatter || typeof frontmatter !== "object" || Array.isArray(frontmatter)) {
        throw new Error(t("Frontmatter must be a JSON object"));
      }
      const result = await request<{ document: DocumentDetail }>(
        docUrl(workspaceId, selected.doc_key),
        {
          method: "PUT",
          body: JSON.stringify({
            title,
            content,
            frontmatter,
            expected_version: selected.version,
            human_note: humanNote || undefined,
            lock: lockDoc,
          }),
        },
      );
      onUpdated(result.document);
      notify(t("Document {doc} saved as v{version}.", { doc: selected.doc_key, version: result.document.version }));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Unable to save document"));
    } finally {
      setBusy(false);
    }
  }

  async function unlock() {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      const result = await request<{ document: DocumentDetail }>(
        docUrl(workspaceId, selected.doc_key),
        {
          method: "PUT",
          body: JSON.stringify({
            expected_version: selected.version,
            unlock: true,
            human_note: humanNote || undefined,
          }),
        },
      );
      onUpdated(result.document);
      notify(t("Document unlocked; agents may write again."));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Unable to unlock document"));
    } finally {
      setBusy(false);
    }
  }

  async function create() {
    setBusy(true);
    setError(null);
    try {
      const result = await request<{ document: DocumentDetail }>(
        `/v1/admin/workspaces/${workspaceId}/documents`,
        {
          method: "POST",
          body: JSON.stringify({
            doc_key: newKey.trim(),
            title: newTitle || undefined,
            content: newContent,
            frontmatter: {},
            expected_version: 0,
          }),
        },
      );
      setShowCreate(false);
      setNewKey("");
      setNewTitle("");
      setNewContent("# New document\n\n");
      onUpdated(result.document);
      notify(t("Document {doc} created.", { doc: result.document.doc_key }));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Unable to create document"));
    } finally {
      setBusy(false);
    }
  }

  async function verifyDocument() {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      const result = await request<{ document: DocumentDetail }>(
        `${docUrl(workspaceId, selected.doc_key)}/verify`,
        {
          method: "POST",
          body: JSON.stringify({
            expected_version: selected.version,
            note: humanNote || undefined,
          }),
        },
      );
      onUpdated(result.document);
      notify(t("Document verified against its current content hash."));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Unable to verify document"));
    } finally {
      setBusy(false);
    }
  }

  async function deprecateDocument() {
    if (!selected) return;
    if (!window.confirm(t("Deprecate this document? Agents can still read it explicitly."))) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await request<{ document: DocumentDetail }>(
        `${docUrl(workspaceId, selected.doc_key)}/deprecate`,
        {
          method: "POST",
          body: JSON.stringify({
            expected_version: selected.version,
            note: humanNote || undefined,
          }),
        },
      );
      onUpdated(result.document);
      notify(t("Document deprecated."));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Unable to deprecate document"));
    } finally {
      setBusy(false);
    }
  }

  async function openHistory() {
    if (!selected) return;
    setShowHistory(true);
    setVersionBusy(true);
    setVersionError(null);
    setVersionPreview(null);
    try {
      const result = await request<{ versions: DocumentVersionSummary[] }>(
        `${docUrl(workspaceId, selected.doc_key)}/versions`,
      );
      setVersions(result.versions);
      if (result.versions.length > 0) {
        const newest = await request<DocumentVersion>(
          `${docUrl(workspaceId, selected.doc_key)}/versions/${result.versions[0].version}`,
        );
        setVersionPreview(newest);
      }
    } catch (caught) {
      setVersionError(caught instanceof Error ? caught.message : t("Unable to load history"));
    } finally {
      setVersionBusy(false);
    }
  }

  async function previewVersion(version: number) {
    if (!selected) return;
    setVersionBusy(true);
    try {
      const detail = await request<DocumentVersion>(
        `${docUrl(workspaceId, selected.doc_key)}/versions/${version}`,
      );
      setVersionPreview(detail);
    } catch (caught) {
      setVersionError(caught instanceof Error ? caught.message : t("Unable to load version"));
    } finally {
      setVersionBusy(false);
    }
  }

  return (
    <div className="documents-layout">
      <aside className="doc-list card">
        <div className="card-head compact">
          <div>
            <h2>{t("Library")}</h2>
            <p>{t("{count} documents · namespace tree", { count: documents.length })}</p>
          </div>
          <button
            type="button"
            className="icon-button"
            onClick={() => setShowCreate(true)}
            aria-label={t("New document")}
          >
            <IconPlus size={15} />
          </button>
        </div>
        {documents.length === 0 ? (
          <Empty title={t("No documents")} detail={t("Create one or let an agent hand off an artifact.")} />
        ) : (
          grouped.map(([namespace, docs]) => (
            <div key={namespace} className="doc-group">
              <div className="doc-group-label">
                <span>{namespace}</span>
                <span>{docs.length}</span>
              </div>
              {docs.map((doc) => (
                <button
                  key={doc.id}
                  type="button"
                  className={`doc-item ${selected?.doc_key === doc.doc_key ? "active" : ""}`}
                  onClick={() => onSelect(doc.doc_key)}
                >
                  <span className="doc-item-icon">
                    <IconDocuments size={15} />
                  </span>
                  <span className="doc-item-main">
                    <span className="doc-item-title" title={doc.title}>{doc.title}</span>
                    <span className="doc-item-key" title={doc.doc_key}>{doc.doc_key}</span>
                  </span>
                  <span className="doc-item-side">
                    {doc.tainted && <span className="taint-dot" title={t("Tainted provenance")} />}
                    {doc.locked_by_human && <IconLock size={12} />}
                    <span className={`verification-dot tone-${verificationTone(doc.verification_status)}`} />
                    <span>v{doc.version}</span>
                  </span>
                </button>
              ))}
            </div>
          ))
        )}
      </aside>

      <section className="card doc-editor">
        {!selected ? (
          <div className="doc-placeholder">
            <IconDocuments size={30} />
            <h2>{t("Select a document")}</h2>
            <p>{t("Shared documents combine structured frontmatter with Markdown, like a machine-readable Notion page.")}</p>
          </div>
        ) : (
          <>
            <div className="doc-editor-head">
              <div>
                <div className="eyebrow">{selected.doc_key}</div>
                <h2>{selected.title}</h2>
                <div className="doc-meta">
                  <span>v{selected.version}</span>
                  <span>{t("Updated")} {relativeTime(selected.updated_at)} {t("by")} {selected.updated_by}</span>
                  <span className={`verification-pill tone-${verificationTone(selected.verification_status)}`}>
                    {t(selected.verification_status)}
                  </span>
                  <span className={`trust-pill ${selected.trust_level === "TRUSTED" ? "trusted" : "untrusted"}`}>
                    {t(selected.trust_level)}
                  </span>
                  {selected.tainted && (
                    <span className="pill rose" title={t("Derived from tainted or untrusted sources")}>
                      {t("Tainted provenance")}
                    </span>
                  )}
                  {selected.locked_by_human && (
                    <span className="lock-badge human"><IconLock size={11} /> {t("Human locked")}</span>
                  )}
                </div>
              </div>
              <div className="doc-actions">
                <div className="segmented">
                  <button
                    type="button"
                    className={mode === "preview" ? "active" : ""}
                    onClick={() => setMode("preview")}
                  >
                    {t("Preview")}
                  </button>
                  <button
                    type="button"
                    className={mode === "edit" ? "active" : ""}
                    onClick={() => setMode("edit")}
                  >
                    {t("Edit")}
                  </button>
                </div>
                <button type="button" className="button ghost" onClick={openHistory}>
                  {t("History")}
                </button>
                {selected.verification_status !== "VERIFIED" && (
                  <button type="button" className="button ghost" onClick={verifyDocument} disabled={busy}>
                    {busy ? <Spinner /> : <IconCheck />} {t("Verify")}
                  </button>
                )}
                {selected.verification_status !== "DEPRECATED" && (
                  <button type="button" className="button ghost" onClick={deprecateDocument} disabled={busy}>
                    <IconX /> {t("Deprecate")}
                  </button>
                )}
                {selected.locked_by_human && (
                  <button type="button" className="button ghost" onClick={unlock} disabled={busy}>
                    {busy ? <Spinner /> : <IconUnlock />} {t("Unlock")}
                  </button>
                )}
                <button type="button" className="button primary" onClick={save} disabled={busy}>
                  {busy ? <Spinner /> : <IconCheck />} {t("Save human edit")}
                </button>
              </div>
            </div>

            {error && <div className="inline-error">{error}</div>}

            {mode === "preview" ? (
              <div className="doc-preview">
                <div className="property-panel">
                  {Object.entries(selected.frontmatter).length === 0 ? (
                    <span className="muted">{t("No frontmatter properties")}</span>
                  ) : (
                    Object.entries(selected.frontmatter).map(([key, value]) => (
                      <span key={key} className="property-chip">
                        <span>{key}</span>
                        <strong>{typeof value === "string" ? value : JSON.stringify(value)}</strong>
                      </span>
                    ))
                  )}
                </div>
                <MarkdownContent content={selected.content} />
                {selected.human_note && (
                  <div className="human-note">
                    <IconLock size={13} /> {selected.human_note}
                  </div>
                )}
              </div>
            ) : (
              <div className="doc-edit-grid">
                <label className="field">
                  <span>{t("Title")}</span>
                  <input value={title} onChange={(e) => setTitle(e.target.value)} />
                </label>
                <label className="field">
                  <span>{t("Frontmatter (JSON properties)")}</span>
                  <textarea
                    rows={7}
                    className="mono-input"
                    value={frontmatterText}
                    onChange={(e) => setFrontmatterText(e.target.value)}
                  />
                </label>
                <label className="field">
                  <span>{t("Markdown body")}</span>
                  <textarea
                    rows={20}
                    className="mono-input content-input"
                    value={content}
                    onChange={(e) => setContent(e.target.value)}
                  />
                </label>
                <div className="doc-edit-foot">
                  <label className="field">
                    <span>{t("Human note (broadcast on save)")}</span>
                    <input value={humanNote} onChange={(e) => setHumanNote(e.target.value)} />
                  </label>
                  <label className="checkbox-row">
                    <input
                      type="checkbox"
                      checked={lockDoc}
                      onChange={(e) => setLockDoc(e.target.checked)}
                    />
                    {t("Lock this document against agent writes")}
                  </label>
                </div>
              </div>
            )}
          </>
        )}
      </section>

      {showHistory && selected && (
        <Modal
          title={t("Version history · {doc}", { doc: selected.doc_key })}
          subtitle={t("Immutable content versions; verification is bound to the current hash.")}
          onClose={() => setShowHistory(false)}
          wide
        >
          {versionError && <div className="inline-error">{versionError}</div>}
          <div className="version-layout">
            <div className="version-list">
              {versions.length === 0 && (
                <span className="muted">{t("No versions recorded.")}</span>
              )}
              {versions.map((version) => (
                <button
                  key={version.version}
                  type="button"
                  className={`version-row ${
                    versionPreview?.version === version.version ? "active" : ""
                  }`}
                  onClick={() => void previewVersion(version.version)}
                >
                  <span className="version-number">v{version.version}</span>
                  <span className="version-main">
                    <span>{version.change_summary ?? t("content change")}</span>
                    <span className="version-meta">
                      {version.author} · {relativeTime(version.created_at)}
                    </span>
                  </span>
                  <span
                    className={`verification-dot tone-${verificationTone(version.verification_status)}`}
                  />
                </button>
              ))}
            </div>
            <div className="version-preview">
              {versionBusy ? (
                <Spinner />
              ) : versionPreview ? (
                <>
                  <div className="version-meta">
                    v{versionPreview.version} · {versionPreview.trust_level} ·{" "}
                    {versionPreview.content_hash.slice(0, 12)}…
                  </div>
                  <MarkdownContent content={versionPreview.content} />
                </>
              ) : (
                <span className="muted">{t("Select a version to inspect.")}</span>
              )}
            </div>
          </div>
        </Modal>
      )}

      {showCreate && (
        <Modal
          title={t("New shared document")}
          subtitle={t("A doc_key is a stable path, e.g. strategy/btc_weekly_plan.")}
          onClose={() => setShowCreate(false)}
          footer={
            <>
              <button type="button" className="button ghost" onClick={() => setShowCreate(false)}>
                {t("Cancel")}
              </button>
              <button
                type="button"
                className="button primary"
                onClick={create}
                disabled={busy || !newKey.trim() || !newContent.trim()}
              >
                {busy ? <Spinner /> : <IconPlus />} {t("Create document")}
              </button>
            </>
          }
        >
          <label className="field">
            <span>{t("Document key")}</span>
            <input
              value={newKey}
              onChange={(e) => setNewKey(e.target.value)}
              placeholder="strategy/btc_weekly_plan"
              autoFocus
            />
          </label>
          <label className="field">
            <span>{t("Title")}</span>
            <input value={newTitle} onChange={(e) => setNewTitle(e.target.value)} />
          </label>
          <label className="field">
            <span>{t("Markdown body")}</span>
            <textarea
              rows={10}
              className="mono-input"
              value={newContent}
              onChange={(e) => setNewContent(e.target.value)}
            />
          </label>
          {error && <div className="inline-error">{error}</div>}
        </Modal>
      )}
    </div>
  );
}
