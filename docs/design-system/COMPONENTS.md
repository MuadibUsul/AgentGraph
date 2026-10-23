# AgentGraph Component Contract

## Authentication pages

- `/dashboard/login/` is the only customer sign-in surface; it uses a two-column
  value-proposition/auth-card layout on desktop and a compact stacked layout on
  mobile (proof grid hidden to keep the form above the fold).
- `/dashboard/connect/` is the operator-only admin-token page; customer login
  never shows an admin token field.
- The console redirects unauthenticated visitors to `/login/` and renders an
  account card, not a second login form, once signed in.
- Google Identity Services renders only when `/v1/auth/config` returns a client
  ID; otherwise the email/password flow remains the only visible option.

## Language toggle

- A two-button segmented control (`EN` / `中文`) sits in the top bar.
- Native buttons with `aria-pressed`; persisted across reloads; updates
  `<html lang>`, numbers, dates, status/role labels and all static UI copy.
- Technical identifiers (`doc://`, enum values in payloads, agent signatures)
  remain monospace English by design.

## Navigation and layout

- Sidebar workspaces are native buttons; active state uses surface + a 2px indigo
  inset edge + `aria-current`, not color alone.
- Tabs are native buttons inside a `role="tablist"`; each tab has `aria-selected`
  and `aria-controls`, and the content is a `<div role="tabpanel">` linked by
  `aria-labelledby`.
- A tab has one badge. When a tab has an alert count, the same number is styled
  amber instead of rendering a duplicate “2 2” badge.
- Tabs use the WAI-ARIA roving-tabindex pattern: only the selected tab is in the
  Tab order; ArrowLeft/ArrowRight/ArrowUp/ArrowDown/Home/End move selection and
  focus. The active tab controls a `role="tabpanel"` labelled by it.
- Tab focus rings use an inset 2px outline so the top/bottom edges are not
  clipped by the horizontally scrollable tab strip.
- A skip link moves focus to `#main-content` and follows the selected language.
- ≤900px: the sidebar connection form moves into a modal opened by the top-bar
  `Connection` button; workspaces become a horizontal strip so workspace content
  remains above the fold.

## Buttons

Variants: primary, ghost, subtle, danger, small, icon. Minimum interactive size
is 36px for standard buttons, 32px for icon buttons, 40px for tabs and 44px for
document rows; WCAG 2.2 target-size minimum is 24px. Primary buttons are solid
indigo "texture" buttons (v3.4): a light-edged shell around a graded fill with
an inset highlight and a pressed state; ghost and subtle buttons use the same
construction in neutral glass. Loading buttons disable repeated submission and
show a spinner. Focus-visible draws a 2px indigo ring offset from the surface.

## Motion helpers (v3.4)

`components/motion.tsx`: `AnimatedNumber` (count-up, ~600 ms ease-out),
`useSlidingIndicator` (one indicator that slides to the active tab) and
`spotlight` (a cursor-following glow for `.stat-card`). All render the final
state without motion under `prefers-reduced-motion`.

## Inputs and forms

- Every input has a visible `<label>`/field span.
- JSON/Schema inputs use the monospace input style.
- Errors are rendered as text in an `inline-error` or banner, never color-only.
- Banner dismiss buttons have explicit accessible names.
- Connection errors distinguish empty endpoint, empty token, whitespace in the
  token, too-short token, rejected token, missing admin API, and unreachable API.
- Destructive operations (delete policy/webhook, rotate commander key) require
  an explicit confirmation and state what will break.

## Dialog

- `role="dialog"`, `aria-modal`, `aria-labelledby`.
- Focus moves into the dialog, Tab is trapped, Escape closes, focus returns to
  the trigger, and page scroll is locked. The trigger is captured before child
  `autoFocus` runs; `returnFocusSelector` provides a fallback for the New task
  modal.
- Emitted by the shared `Modal` component; new dialogs must use it.
- The mobile connection dialog reuses the same state and submit path as the
  desktop sidebar form.

## Tables and state

- Data tables keep stable numeric alignment (`tabular-nums`) and provide empty
  states.
- The state grid uses `table-layout: fixed`; long JSON is clipped in-column with
  a visible ellipsis and the full value in `title`.
- Each state row exposes a real **Override** button (not a span) with a unique
  accessible name, so keyboard users can reach the action.
- The state table reflows into labelled cards based on **container width**
  (`@container (max-width: 760px)`), with a viewport media query retained as a
  fallback. This triggers at browser zoom even when the viewport breakpoint is
  evaluated differently; headers cannot merge and Override stays visible.
- Task cards show status text + color, ping-pong count and lease state.
- Priority `0` is displayed as **Normal**, not a misleading `P0`.
- Tainted documents/tasks/slots show a text label and a danger dot; provenance
  is never hidden behind a color-only signal.

## Empty, loading and error states

- `Empty` is used for no proposals, policies, webhooks, actions and accounts.
- Skeletons preserve layout size.
- Failed review/apply/dispatch operations surface the server error message and
  remain retryable.
- Audit batch count uses singular/plural copy (`1 change`, `n changes`).

## Governance-specific components

- **Proposal card** — document key, status, base version, vote score/count,
  taint warning, content preview, Review/Apply actions.
- **Policy card** — resource type, glob pattern, proposal requirement, allowed
  roles, JSON Schema indicator, enable/disable/delete.
- **Webhook row** — endpoint, event types, recent failures, Test/Delete.
- **External action row** — action type, status, target, attempts, last error,
  Approve/Reject/Compensate.
- **Budget/account panels** — real server values only; no fabricated numbers.
