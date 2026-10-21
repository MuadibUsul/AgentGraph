# AgentGraph Design Foundation

## Visual claim

**A quiet operations console for long human-in-the-loop sessions:** flat
graphite surfaces, one indigo action color, precise 13–14px data type, compact
density, and one hierarchy: workspace → work → detail. This is an operations
surface, not a marketing page.

The existing dark identity is intentionally preserved. A light theme was not
required by the product and was not delivered; the token layer is ready for one
without changing components.

## Product constraints that outrank visuals

1. Human sovereignty, proposal review, taint and undo semantics must remain
   exactly as implemented.
2. Tenant, agent and human account boundaries are not visual choices.
3. Dense tables and timed task/lease information must stay scannable.
4. No fabricated business data, fake tenants or decorative statistics.

## Color

- Neutral family: cool graphite/slate, with distinct page, panel and raised
  surfaces instead of transparency layers.
- Brand/action: indigo (`#4f46e5`); hover `#a5b4fc`; foreground `#ffffff`.
- Secondary semantic accents: violet for proposals/review, blue for live state,
  amber for human gates, ruby for danger/failure, jade for success.
- Interactive control borders use `#52657d` (3.03:1 against panels); dividers
  remain quieter.
- Status is never communicated by color alone: text labels, dots with titles and
  counts accompany color.

## Typography

- Bilingual UI: English and Simplified Chinese are first-class copy sets; the
  language toggle persists in `localStorage.agentgraph.lang` and updates
  `<html lang>`, number/date formatting, status labels and all static copy.
- Body: 14px system UI stack with Noto Sans SC/PingFang/Microsoft YaHei fallbacks.
- Metadata: 12px; chips/captions: 11px minimum. The previous 8.5–10px metadata
  scale was removed from visible console surfaces.
- Identifiers, keys, timestamps and JSON: monospace.
- Numeric data: `font-variant-numeric: tabular-nums` via `.tabular`, statistics
  and data-table cells.

## Space and shape

4px rhythm; common steps 4/8/12/16/20/24/32/40/48/64. Radius scale 6/10/14;
primary surfaces use 10–14px, chips 4–6px. Borders are 1px; shadows are reserved
for modal/overlay separation. Information hierarchy comes from layout, type and
surface contrast before color or shadow.

## Motion

Micro transitions are 120ms, component transitions 180ms, larger movement
240ms. No decorative gradients, glows or pulsing status animation are used.
`prefers-reduced-motion: reduce` disables animations and transitions globally.

## Density and layout

- Desktop: 264px sidebar + flexible workspace surface, content max 1440px.
- The workspace overview is compact: 26px metric values and ≤138px stat cards,
  so the approval gate or task board remains above the first fold.
- ≤900px: sidebar becomes a compact top section; the connection form moves into
  a modal opened from the top bar; workspaces become a horizontal strip.
- ≤640px: topbar stacks, statistics become a 2×2 compact strip, tab labels stay
  visible, and the kanban scrolls horizontally by column.
- 320px is supported by single-column layout; horizontal overflow is limited to
  intentionally scrollable tab/kanban strips.
