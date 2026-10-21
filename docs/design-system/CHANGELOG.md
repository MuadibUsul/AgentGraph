# Design System Changelog

## 2026-10-20 — v3.3 visual refresh

### Inherited

- Dark control-room identity, indigo action accent, compact information density,
  monospace identifiers, and all existing navigation, task, document, agent,
  governance and audit information architecture.
- All product copy, task/document/lease/proposal semantics and backend APIs.

### Changed

- Replaced the 296px translucent sidebar with a flat 264px graphite panel.
- Removed the page-wide grid/gradient background, card gradients, glow shadows,
  hover lifts and the pulsing live dot.
- `--ds-*` tokens rebuilt around a 1–12 graphite ramp with explicit page,
  panel, raised and inset surfaces; indigo action is now `#4f46e5`.
- Added `--ds-border-control` (`#52657d`, 3.03:1) for interactive control
  boundaries; dividers remain quieter.
- Typography raised: 14px body, 12px metadata, 11px minimum chips/captions.
  Radius scale tightened to 6/10/14.
- Statistics compacted to ≤138px cards with 26px values; workspace hero h1 is
  32px, so primary work is visible above the fold.
- Main tabs use an underline active state and a single alert-aware badge; tab
  panels now have `role="tabpanel"` and `aria-labelledby`.
- Kanban fits four columns at 1280px with 13px titles and 12px briefs.
- State grid uses fixed layout, ellipsized JSON and full value in `title`.
- Proposal, policy, action, audit and document cards now use flat surfaces and
  readable metadata sizes.
- Modals use solid raised surfaces without blur/gradient; fields are 14px with
  40px control height.
- Mobile: connection form moves into a top-bar modal; workspaces become a
  horizontal strip; stats become a compact 2×2 block; tab labels stay visible;
  server chip is hidden to preserve the first fold.
- Priority `0` renders as **Normal**; audit count uses singular/plural copy.
- Banner close buttons and workspace buttons expose accessible names/state.

### 2026-10-21 — Indigo / Iris palette

- Replaced the teal brand/action accent with indigo `#4f46e5` (hover
  `#5b5bf0`, foreground white) and periwinkle accent text `#a5b4fc` /
  `#c7d2fe`.
- Green is now reserved for success/DONE semantics (`#34d399`); action,
  selection and focus no longer share the success hue.
- Updated auth aurora/gradient/CTA, console active states, focus rings,
  favicon and all remaining hardcoded teal values.
- Contrast gate: 15/15 pairs pass (primary button 6.29:1, accent 9.94:1,
  focus ring 9.09:1).

## 2026-10-21 — self-service auth experience

- Split authentication out of the console: `/dashboard/login/` is a dedicated
  full-screen sign-in/signup surface with a Cult-UI-inspired aurora/grid
  backdrop, value proposition, proof points and one auth card.
- Moved admin token entry to a separate `/dashboard/connect/` operator page.
  The console no longer renders a second login form or an admin connection form
  in the sidebar; unauthenticated visits redirect to `/dashboard/login/`.
- The signed-in console uses a human session and maps tenant-admin paths to
  `/v1/human` and tenant `/v1` endpoints; the sidebar shows an account card
  instead of a token form.
- Email registration creates a free workspace + OWNER; Google ID-token login is
  implemented and appears automatically when `GOOGLE_CLIENT_ID` is configured.

### 2026-10-20 — browser tab identity

- Added `dashboard/app/icon.svg`, `icon.png`, `favicon.ico` (16/32/48) and
  `apple-icon.png` (180×180): graphite rounded tile with the three-node
  AgentGraph graph in indigo.
- Next.js static export emits favicon, 512px PNG and Apple touch icon links;
  verified all three return 200 in production.

### 2026-10-20 — accessibility follow-up (post-acceptance)

- Tabs now use roving tabindex with Arrow/Home/End navigation, so Tab moves
  from the active tab into the panel instead of through six tab buttons.
- State-table Override is a real button with a unique accessible name; the
  blackboard now has keyboard-reachable content actions.
- State table reflow now uses a container query (`@container (max-width:760px)`)
  with the viewport media query as fallback; this covers browser zoom even when
  the viewport breakpoint evaluates differently.
- Tab focus rings use an inset outline so the horizontally scrollable tab strip
  cannot clip their top/bottom edges.
- JSON previews always show a visible ellipsis (`…`), with the full JSON in
  `title`.
- Modal focus restoration captures the trigger before `autoFocus` and falls back
  to `[data-modal-trigger="new-task"]`; Escape returns to New task.
- Connection errors distinguish empty endpoint, empty token, whitespace,
  too-short token, rejected token, missing admin API and network failure.

### Verified

- `npx tsc --noEmit` and `npm run build` pass.
- `scripts/check_contrast.py`: 13/13 pairs pass.
- 1440px, 1280px, 390px and 320px layout metrics checked; 320px document
  scrollWidth 311, 390px 381, no page-level horizontal overflow.
- Bilingual EN/中文 reviewed on the refreshed layout.

### Known exceptions

- Legacy v3.2 declarations remain underneath the v3.3 component layer; visible
  surfaces are overridden, but the duplication is tracked for cleanup.
- The console remains dark-only by product decision.
- Automated axe/Playwright visual regression is not configured.
- Full keyboard walk-through and 200% zoom remain manual/blocked in this pass.

## 2026-10-19 — v3.2 console pass

### Inherited

- Dark control-room identity, indigo action accent, compact information density,
  monospace identifiers, existing sidebar/topbar/task/document/audit layouts.
- Product copy and all task/document/lease/proposal semantics.

### Added

- Primitive → semantic token layer in `dashboard/app/design-tokens.css`.
- `--ds-text-muted` raised to `#74869b`; measured 4.99:1 on panels and 5.31:1 on
  page background.
- Governance view: Proposals, Policies, Operations, Access & budget.
- Taint badges in document list/editor/version history, task cards and state grid.
- Shared `Modal` focus entry/trap, Escape close, focus restoration and scroll lock.
- Skip link to `#main-content`.
- `prefers-reduced-motion` remains global.
- Tabular numerals for statistics and data tables.
- Responsive governance grids at 1100px/700px, plus existing 900px/640px shell
  breakpoints.

### 2026-10-20 — bilingual console

- Added persistent EN/中文 switch in the top bar.
- Added `dashboard/app/i18n.tsx` with complete static copy, status/role labels,
  event types and interpolation.
- Localized dates, relative times, numbers, tiers and verification/trust labels.
- Updated `<html lang>` and stored preference in `localStorage.agentgraph.lang`.

### 2026-10-20 — Notion HTML compatibility

- Added `app/engine/notion.py`: detects Notion/HTML uploads, converts tables,
  headings, lists and links to Markdown, strips scripts/unsafe URLs, and keeps
  surrounding Markdown intact.
- Added automatic normalization on document create/update/artifact and an admin
  repair endpoint for legacy documents.
- Added `MarkdownContent` with GFM + `rehype-raw` + `rehype-sanitize` and table
  styling for legacy raw HTML.
