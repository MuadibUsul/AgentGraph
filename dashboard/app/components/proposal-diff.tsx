"use client";

import { useEffect, useMemo, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest, DocumentVersion, Proposal } from "../types";
import { Spinner } from "./ui";

type Line = { kind: "same" | "add" | "del"; text: string };

const LCS_LIMIT = 4_000_000; // cells; beyond this show the changed block without alignment

/** Line diff: trim the common head and tail, then LCS on the middle. */
export function diffLines(before: string, after: string): Line[] {
  const a = before.split("\n");
  const b = after.split("\n");
  let head = 0;
  while (head < a.length && head < b.length && a[head] === b[head]) head += 1;
  let tail = 0;
  while (
    tail < a.length - head &&
    tail < b.length - head &&
    a[a.length - 1 - tail] === b[b.length - 1 - tail]
  ) {
    tail += 1;
  }
  const midA = a.slice(head, a.length - tail);
  const midB = b.slice(head, b.length - tail);
  const out: Line[] = a.slice(0, head).map((text) => ({ kind: "same", text }));
  if (midA.length * midB.length > LCS_LIMIT) {
    out.push(...midA.map((text) => ({ kind: "del" as const, text })));
    out.push(...midB.map((text) => ({ kind: "add" as const, text })));
  } else {
    const n = midA.length;
    const m = midB.length;
    const table = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
    for (let i = n - 1; i >= 0; i -= 1) {
      for (let j = m - 1; j >= 0; j -= 1) {
        table[i][j] =
          midA[i] === midB[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
      }
    }
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (midA[i] === midB[j]) {
        out.push({ kind: "same", text: midA[i] });
        i += 1;
        j += 1;
      } else if (table[i + 1][j] >= table[i][j + 1]) {
        out.push({ kind: "del", text: midA[i] });
        i += 1;
      } else {
        out.push({ kind: "add", text: midB[j] });
        j += 1;
      }
    }
    while (i < n) out.push({ kind: "del", text: midA[i++] });
    while (j < m) out.push({ kind: "add", text: midB[j++] });
  }
  out.push(...a.slice(a.length - tail).map((text) => ({ kind: "same" as const, text })));
  return out;
}

/** Headings whose section contains a change, in document order. */
function changedSections(lines: Line[]): string[] {
  const sections: string[] = [];
  let heading = "";
  for (const line of lines) {
    if (line.kind !== "del" && /^#{1,6}\s/.test(line.text)) heading = line.text.replace(/^#+\s*/, "");
    if (line.kind !== "same") {
      const name = heading || "(top)";
      if (!sections.includes(name)) sections.push(name);
    }
  }
  return sections;
}

const CONTEXT = 2;

/** Changes of a proposal against its base version, with a summary first. */
export function ProposalDiff({
  proposal,
  workspaceId,
  request,
}: {
  proposal: Proposal;
  workspaceId: string;
  request: ApiRequest;
}) {
  const { t } = useI18n();
  const [base, setBase] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<"changes" | "full">("changes");

  useEffect(() => {
    let live = true;
    const path = proposal.doc_key.split("/").map(encodeURIComponent).join("/");
    request<DocumentVersion>(
      `/v1/admin/workspaces/${workspaceId}/documents/${path}/versions/${proposal.base_version}`,
    )
      .then((version) => live && setBase(version.content))
      .catch((caught) => live && setError(caught instanceof Error ? caught.message : String(caught)));
    return () => {
      live = false;
    };
  }, [proposal.id, proposal.doc_key, proposal.base_version, workspaceId, request]);

  const lines = useMemo(() => (base === null ? [] : diffLines(base, proposal.content)), [base, proposal.content]);
  const added = lines.filter((line) => line.kind === "add").length;
  const removed = lines.filter((line) => line.kind === "del").length;
  const sections = useMemo(() => changedSections(lines), [lines]);

  // Changed lines plus a little context; long unchanged runs fold into one row.
  const shown = useMemo(() => {
    const keep = lines.map((line) => line.kind !== "same");
    const near = lines.map((_, index) =>
      keep.slice(Math.max(0, index - CONTEXT), index + CONTEXT + 1).some(Boolean),
    );
    const rows: Array<Line | { kind: "fold"; count: number }> = [];
    let folded = 0;
    lines.forEach((line, index) => {
      if (near[index]) {
        if (folded) rows.push({ kind: "fold", count: folded });
        folded = 0;
        rows.push(line);
      } else {
        folded += 1;
      }
    });
    if (folded) rows.push({ kind: "fold", count: folded });
    return rows;
  }, [lines]);

  return (
    <div className="proposal-diff">
      <div className="proposal-diff-head">
        {base === null && !error ? (
          <span className="muted"><Spinner /> {t("Comparing with base v{version}…", { version: proposal.base_version })}</span>
        ) : error ? (
          <span className="danger-text">{t("Could not load the base version: {error}", { error })}</span>
        ) : (
          <span className="diff-summary">
            <strong className="diff-add">+{added}</strong>
            <strong className="diff-del">−{removed}</strong>
            <span className="muted">
              {sections.length
                ? t("Changed sections: {sections}", { sections: sections.slice(0, 6).join(" · ") }) +
                  (sections.length > 6 ? ` +${sections.length - 6}` : "")
                : t("No text changes against the base version.")}
            </span>
          </span>
        )}
        <div className="segmented small" role="group" aria-label={t("Proposal view")}>
          <button type="button" className={view === "changes" ? "active" : ""} onClick={() => setView("changes")}>
            {t("Changes")}
          </button>
          <button type="button" className={view === "full" ? "active" : ""} onClick={() => setView("full")}>
            {t("Full text")}
          </button>
        </div>
      </div>
      {view === "full" || error ? (
        <pre className="proposal-body tall">{proposal.content}</pre>
      ) : (
        <div className="diff-view" role="table" aria-label={t("Changes")}>
          {shown.map((row, index) =>
            row.kind === "fold" ? (
              <div key={index} className="diff-fold">{t("{count} unchanged line(s)", { count: row.count })}</div>
            ) : (
              <div key={index} className={`diff-line diff-${row.kind}`}>
                <span className="diff-mark" aria-hidden="true">
                  {row.kind === "add" ? "+" : row.kind === "del" ? "−" : " "}
                </span>
                <span>{row.text || " "}</span>
              </div>
            ),
          )}
        </div>
      )}
    </div>
  );
}
