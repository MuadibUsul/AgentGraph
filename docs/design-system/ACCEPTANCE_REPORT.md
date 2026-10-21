# Design System Acceptance Report — AgentGraph v3.2.1

**Project:** AgentGraph
**Version:** 3.2.1 (v3.3 console refresh, build marker `3.2.1-ui.2`)
**Style:** quiet high-density dark operations console; flat graphite surfaces, one indigo action accent
**Surface:** `https://agentgraph.fusionpropulsion.space/dashboard/` (fallback: `https://agentgraph.104-207-82-85.nip.io/dashboard/`)

## Automated evidence

| Check | Command | Result |
|---|---|---|
| Backend lint | `ruff check .` | PASS |
| Backend tests | `TEST_DATABASE_URL=… pytest -q` | PASS — 87 passed |
| Dashboard typecheck | `cd dashboard && npx tsc --noEmit` | PASS |
| Dashboard build | `cd dashboard && npm run build` | PASS — static export, build marker `3.2.1-ui.2` in chunk |
| Core contrast | `python scripts/check_contrast.py` | PASS — 15/15 pairs (13 text ≥4.5:1, 2 UI ≥3:1) |
| Production readiness | `GET /readyz` | PASS — `3.2.1`, DB ok, MCP enabled |
| Production dashboard | `GET /dashboard/` | PASS — 200; served chunk `page-ae720507e3a62b6b.js`, CSS `05383bea3506189e.css` |
| Docs auth | `GET /docs` | PASS — 401 unauthenticated, 200 with admin Basic auth |
| Browser tab icon | `GET /dashboard/` links + assets | PASS — favicon.ico, icon.png (512) and apple-icon.png (180) return 200 with correct MIME types |
| Production smoke | `scripts/smoke_test.py` | PASS — workspace/agent/task/doc/verify/audit/stats |

## P0 gates

| ID | Status | Evidence |
|---|---|---|
| P0-01 business flows/permissions intact | PASS | Frontend-only changes; 81 backend tests; no API or state semantics changed |
| P0-02 keyboard reachability/focus | PASS (structural) | Production DOM: only the selected tab has `tabIndex=0`; the next focusable after it is the state-table Override button; Tasks exposes New task + task-card buttons. ArrowRight/Home/End switch tabs; Escape returns focus to `[data-modal-trigger="new-task"]`. Full manual Tab walk still pending because IAB cannot inject Tab. |
| P0-03 operation feedback | PASS | Existing loading/disabled/banners retained; mobile connection modal uses the same submit path and surfaces errors in the banner |
| P0-04 labels/error/recovery | PASS | Every field has a visible label; banner dismiss buttons now have accessible names (`Dismiss error`, `Dismiss notification`) |
| P0-05 contrast AA | PASS | `scripts/check_contrast.py`: 11 text pairs ≥4.5:1; control border 3.03:1; focus ring 12.25:1 |
| P0-06 320/390px no blocking overflow | PASS | Measured document scrollWidth 311 at 320px and 381 at 390px; only tab/kanban strips scroll horizontally; mobile connection form no longer occupies the first screen |
| P0-07 no fabricated business facts | PASS | Production data untouched; screenshots use real workspace/task/slot data |

## P1 gates

| ID | Status | Evidence |
|---|---|---|
| P1-01 Radix-style role usage | PASS | Primitive 1–12 graphite ramp; semantic surfaces/borders/text/action/status mapping |
| P1-02 token mapping/no random colors | PASS | New component layer uses `--ds-*` and legacy aliases; raw values limited to token definitions and documented exceptions |
| P1-03 typography/spacing/radius | PASS | 11/12/13/14/15/18/32px scale; 4px rhythm; 6/10/14px radii; visible 8.5–10px metadata removed |
| P1-04 component states | PASS | Buttons/inputs/tabs/cards cover hover, active, focus, disabled, loading, empty/error |
| P1-05 light/dark | N/A | Existing dark identity; TOS `follow-existing`; light theme not requested |
| P1-06 modal focus/scroll/close | PASS | Shared Modal keeps `aria-modal`, focus entry/trap, Escape, scroll lock and focus return |
| P1-07 200% zoom/reflow | PASS | State table now uses `@container (max-width: 760px)`. Production at an 800px viewport (where `matchMedia('(max-width:760px)')` is false) still reflows to labelled cards because the 717px table container triggers the query; Override is visible and the page does not overflow. At 1280px the table stays a table (901px container). |
| P1-08 target size | PASS | Buttons 36px, icon buttons 32px, tabs 40px, document rows 44px (≥24px WCAG 2.2 minimum) |
| P1-09 reduced motion | PASS | Global reduced-motion rule retained; pulse removed |
| P1-10 no regression | PASS | Typecheck, build and 81 backend tests pass |
| P1-11 bilingual switch | PASS | EN/中文 verified on the refreshed UI, including tabs, stats, mobile connection modal and new strings |
| P1-12 Notion tables | PASS | Existing sanitized Markdown renderer unchanged; Notion tables still render as tables |

## P2 gates

| ID | Status | Note |
|---|---|---|
| P2-01 primary task focus | PASS | Compact 2×2 mobile stats + 264px sidebar put the approval gate/task board on the first desktop screen; top-bar New task is secondary |
| P2-02 numeric typography | PASS | `.tabular`, stat values and data-table cells use tabular numerals |
| P2-03 empty/skeleton/error/long text | PASS | Empty states retained; state JSON now truncates safely with full value in `title`; singular audit copy fixed |
| P2-04 chart color semantics | N/A | No charts added |
| P2-05 copy | PASS | Duplicate “2 2” badge removed; priority 0 reads Normal; "1 change" singular fixed; connection errors distinguish empty/spaces/too-short/rejected/network |
| P2-09 tab focus ring | PASS (structural) | Tab/governance tab focus uses an inset 2px outline (`outline-offset:-2px`) so the scroll strip cannot clip its top/bottom edges. |
| P2-06 visual regression baseline | BLOCKED | 1440px/1280px/640px screenshots captured; no approved pixel baseline |
| P2-07 unnecessary animation/dependency | PASS | No new UI dependency; gradients/glows/pulse removed from visible surfaces |
| P2-08 token/component docs | PASS | FOUNDATION, TOKENS, COMPONENTS, ACCESSIBILITY and this report updated |

## Visual evidence

- 1440×900: flat surface hierarchy, compact stats, approval gate and state grid
  above the fold; long state JSON ellipsized inside its column.
- 1280×720: four-column kanban visible with all column headers; no page-level
  horizontal overflow.
- 390×844: workspace content begins at ~359px; workspace strip and Connection
  button replace the full sidebar form; tab labels remain visible; 2×2 stat
  strip.
- 320px: document scrollWidth 311; intentional overflow only within the tab
  strip.
- Bilingual: EN → 中文 verified; `<html lang>`, formatters and new labels update.

## Remaining risks

1. Full keyboard walk-through and 200% zoom need a manual pass outside IAB.
2. Mobile screenshot timing is unstable in the in-app browser; metrics and one
   successful capture are used, but a second visual pass is advisable.
3. Dark-only is intentional. A light theme would need a separate palette and
   re-measurement.
4. Automated axe/Playwright visual regression is not configured.
5. Legacy v3.2 CSS declarations remain underneath the v3.3 component layer;
   visible surfaces are overridden, but the duplication should be cleaned up in
   a later pass.

## Rollback

- UI: revert `dashboard/app/design-tokens.css`, `dashboard/app/v3.css`, and the
  touched components/page/i18n files; no backend API change is involved.
- Deploy: rebuild the previous dashboard static export or use the retained
  previous image; do not delete `agentgraph_pgdata`.
