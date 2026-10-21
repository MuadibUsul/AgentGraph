"use client";

import { useEffect, useId, useRef, type ReactNode } from "react";

import { useI18n } from "../i18n";
import { statusLabel } from "../types";

export function IconGraph({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <circle cx="5" cy="6" r="2.2" stroke="currentColor" strokeWidth="1.5" />
      <circle cx="19" cy="5" r="2.2" stroke="currentColor" strokeWidth="1.5" />
      <circle cx="12" cy="18" r="2.2" stroke="currentColor" strokeWidth="1.5" />
      <path d="m6.8 7.4 3.8 8.4M17.2 6.7l-3.7 8.9M7.2 6.2l9.6-.7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

export function IconBoard({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <rect x="3" y="4" width="18" height="16" rx="3" stroke="currentColor" strokeWidth="1.5" />
      <path d="M9 4v16M15 4v10" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

export function IconTasks({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <rect x="4" y="3" width="16" height="18" rx="3" stroke="currentColor" strokeWidth="1.5" />
      <path d="m8 9 1.8 1.8L13 7.6M8 15h8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function IconDocuments({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M6 3h7l5 5v13a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z" stroke="currentColor" strokeWidth="1.5" />
      <path d="M13 3v5h5M8 12h7M8 16h5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

export function IconAgents({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <circle cx="9" cy="8" r="3" stroke="currentColor" strokeWidth="1.5" />
      <path d="M3.5 20a5.5 5.5 0 0 1 11 0M16 5.5a3 3 0 0 1 0 5.8M17 14a5 5 0 0 1 3.5 6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

export function IconAudit({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M4 4v16h16" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <path d="m7 14 3-4 3 2 4-6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="7" cy="14" r="1.3" fill="currentColor" />
      <circle cx="10" cy="10" r="1.3" fill="currentColor" />
      <circle cx="13" cy="12" r="1.3" fill="currentColor" />
      <circle cx="17" cy="6" r="1.3" fill="currentColor" />
    </svg>
  );
}

export function IconPlus({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M12 5v14M5 12h14" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

export function IconRefresh({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M20 11a8 8 0 1 0-2.34 5.66M20 5v6h-6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function IconKey({ size = 15 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <circle cx="8" cy="15" r="4" stroke="currentColor" strokeWidth="1.6" />
      <path d="m11 12 8-8M16 7l2 2M14 9l2 2" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}

export function IconCopy({ size = 15 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <rect x="9" y="9" width="11" height="11" rx="2" stroke="currentColor" strokeWidth="1.6" />
      <path d="M15 9V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h3" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  );
}

export function IconClose({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="m6 6 12 12M18 6 6 18" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}

export function IconLock({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <rect x="5" y="10" width="14" height="10" rx="2" stroke="currentColor" strokeWidth="1.6" />
      <path d="M8 10V7a4 4 0 0 1 8 0v3" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  );
}

export function IconUnlock({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <rect x="5" y="10" width="14" height="10" rx="2" stroke="currentColor" strokeWidth="1.6" />
      <path d="M8 10V7a4 4 0 0 1 7.5-2" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}

export function IconCheck({ size = 15 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="m5 12.5 4.2 4.2L19 6.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function IconX({ size = 15 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="m6 6 12 12M18 6 6 18" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

export function IconPulse({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M3 12h4l2-6 4 12 2-6h6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function IconLayers({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="m12 3 8.5 4.5L12 12 3.5 7.5 12 3Z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      <path d="m3.5 12 8.5 4.5 8.5-4.5M3.5 16.5 12 21l8.5-4.5" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

export function IconDatabase({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <ellipse cx="12" cy="5.5" rx="7" ry="2.8" stroke="currentColor" strokeWidth="1.5" />
      <path d="M5 5.5v6c0 1.55 3.13 2.8 7 2.8s7-1.25 7-2.8v-6M5 11.5v6c0 1.55 3.13 2.8 7 2.8s7-1.25 7-2.8v-6" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

export function IconChevron({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="m9 6 6 6-6 6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function Spinner() {
  return <span className="spinner" aria-hidden />;
}

export function StatusPill({ status }: { status: string }) {
  return (
    <span className={`status-pill tone-${statusTone(status)}`}>{statusLabel(status)}</span>
  );
}

export function RolePill({ role }: { role: string | null }) {
  const { t } = useI18n();
  return (
    <span className={`role-pill tone-${roleTone(role)}`}>{role ? t(role) : t("ANY")}</span>
  );
}

export function statusTone(status: string): string {
  if (status === "DONE") return "mint";
  if (status === "FAILED") return "rose";
  if (status === "ESCALATED_TO_HUMAN" || status === "AWAITING_HUMAN") return "amber";
  if (status === "REVIEWING") return "violet";
  if (status === "IN_PROGRESS") return "sky";
  return "slate";
}

export function roleTone(role: string | null): string {
  if (role === "EXECUTOR") return "mint";
  if (role === "ANALYST") return "sky";
  if (role === "AUDITOR") return "violet";
  if (role === "SCOUT") return "amber";
  return "slate";
}

export function Modal({
  title,
  subtitle,
  onClose,
  children,
  footer,
  wide = false,
  returnFocusSelector,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
  returnFocusSelector?: string;
}) {
  const { t } = useI18n();
  const titleId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  // Capture the trigger during render, before a child input's autoFocus runs.
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  if (restoreFocusRef.current === null && typeof document !== "undefined") {
    restoreFocusRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
  }

  useEffect(() => {
    const previous = restoreFocusRef.current;
    const dialog = dialogRef.current;
    const focusable = dialog?.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );
    const first = focusable?.[0] ?? dialog;
    first?.focus();
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeRef.current();
        return;
      }
      if (event.key !== "Tab" || !dialog) return;
      const items = Array.from(
        dialog.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((item) => item.offsetParent !== null);
      if (items.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const firstItem = items[0];
      const lastItem = items[items.length - 1];
      if (event.shiftKey && document.activeElement === firstItem) {
        event.preventDefault();
        lastItem.focus();
      } else if (!event.shiftKey && document.activeElement === lastItem) {
        event.preventDefault();
        firstItem.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      document.body.style.overflow = previousOverflow;
      const fallback = returnFocusSelector
        ? document.querySelector<HTMLElement>(returnFocusSelector)
        : null;
      if (previous?.isConnected && previous !== document.body) {
        previous.focus();
      } else if (fallback?.isConnected) {
        fallback.focus();
      }
    };
  }, []);

  return (
    <div className="overlay" role="presentation" onMouseDown={onClose}>
      <div
        ref={dialogRef}
        className={`modal ${wide ? "modal-wide" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="modal-head">
          <div>
            <h2 id={titleId}>{title}</h2>
            {subtitle && <p>{subtitle}</p>}
          </div>
          <button type="button" className="icon-button" onClick={onClose} aria-label={t("Close dialog")}>
            <IconClose />
          </button>
        </div>
        {children}
        {footer && <div className="modal-actions">{footer}</div>}
      </div>
    </div>
  );
}

export function Empty({
  title,
  detail,
  action,
}: {
  title: string;
  detail?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-inline">
      <strong>{title}</strong>
      {detail && <span>{detail}</span>}
      {action}
    </div>
  );
}

export function CopyButton({ value, label }: { value: string; label?: string }) {
  const { t } = useI18n();
  return (
    <button
      type="button"
      className="button ghost"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
        } catch {
          // Clipboard access can be blocked; the value remains selectable.
        }
      }}
    >
      <IconCopy /> {label ?? t("Copy")}
    </button>
  );
}

export function initials(value: string): string {
  return value.trim().slice(0, 1).toUpperCase() || "?";
}

export function relativeTime(value: string | null | undefined): string {
  if (!value) return "never";
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return "unknown";
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 86400 * 30) return `${Math.floor(seconds / 86400)}d ago`;
  return new Date(value).toLocaleDateString();
}
