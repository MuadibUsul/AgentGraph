"use client";

import { useState } from "react";
import { createPortal } from "react-dom";

import { useI18n } from "../i18n";
import { normalizeBase } from "../types";

// The upgrade "fake door" (growth plan, step 5): Pro is not sold yet. Opening
// the dialog and asking for Pro are recorded as product events, and the
// person is told plainly that they joined a waitlist.
export function UpgradeCard({ apiBase, sessionToken }: { apiBase: string; sessionToken: string }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [joined, setJoined] = useState(false);
  const [busy, setBusy] = useState(false);

  async function record(name: "upgrade_viewed" | "upgrade_intent") {
    try {
      await fetch(`${normalizeBase(apiBase)}/v1/human/product-events`, {
        method: "POST",
        headers: { Authorization: `Bearer ${sessionToken}`, "Content-Type": "application/json" },
        body: JSON.stringify({ name, source: "sidebar", plan: "pro" }),
        signal: AbortSignal.timeout(10000),
      });
    } catch {
      // Analytics never blocks the console.
    }
  }

  return (
    <>
      <button
        type="button"
        className="upgrade-card"
        onClick={() => {
          setOpen(true);
          void record("upgrade_viewed");
        }}
      >
        <span className="upgrade-card-kicker">Pro</span>
        <strong>{t("Upgrade to Pro")}</strong>
        <small>{t("Unlimited agents, full history, undo and audit.")}</small>
      </button>

      {open && createPortal(
        <div className="overlay" role="presentation" onClick={() => setOpen(false)}>
          <div
            className="modal upgrade-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="upgrade-title"
            onClick={(event) => event.stopPropagation()}
          >
            <span className="upgrade-card-kicker">Veynod Pro</span>
            <h2 id="upgrade-title">
              $12 <small>{t("per month")}</small>
            </h2>
            <ul className="upgrade-list">
              <li>{t("Unlimited agents in every workspace")}</li>
              <li>{t("Full document history with one-click undo")}</li>
              <li>{t("Complete audit trail and exports")}</li>
              <li>{t("Priority support from the team")}</li>
            </ul>
            {joined ? (
              <p className="upgrade-thanks" role="status">
                {t("You're on the list. Pro is not on sale yet; we'll write to you first when it is.")}
              </p>
            ) : (
              <>
                <p className="upgrade-note">
                  {t("Pro is not on sale yet. Tell us you want it and you'll be first in line; nothing is charged.")}
                </p>
                <button
                  type="button"
                  className="button primary full"
                  disabled={busy}
                  onClick={async () => {
                    setBusy(true);
                    await record("upgrade_intent");
                    setBusy(false);
                    setJoined(true);
                  }}
                >
                  {t("I want Pro")}
                </button>
              </>
            )}
            <button type="button" className="button subtle full" onClick={() => setOpen(false)}>
              {t("Close")}
            </button>
          </div>
        </div>,
        document.body,
      )}
    </>
  );
}
