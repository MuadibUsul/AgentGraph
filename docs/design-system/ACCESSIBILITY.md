# AgentGraph Accessibility

## Target

WCAG 2.2 AA for the web console. This is a desktop-first operations surface,
but 320px/390px widths are part of the verification set.

## Implemented in this pass

- Skip link to `#main-content`, localized in both copy sets.
- Global `:focus-visible` outline uses the indigo focus ring; interactive control
  borders measure 3.03:1 against panels.
- Dialog semantics (`role="dialog"`, `aria-modal`, `aria-labelledby`), focus
  entry, Tab trap, Escape close, focus restoration and scroll lock in `Modal`.
- Tabs are native buttons with `aria-selected` and `aria-controls`; the active
  view is a linked `role="tabpanel"`. Tabs stay labeled at 390px instead of
  collapsing to ambiguous icons.
- Roving tabindex: only the selected tab is in the Tab order; arrow keys, Home
  and End move focus and selection. The panel content is therefore the next Tab
  stop, not six tab buttons.
- State-table Override actions are real buttons with unique accessible names;
  task cards are native buttons. DOM inspection after the fix found 3 focusable
  content controls on the Blackboard and 4 task cards + New task in Tasks.
- At 200% (CSS viewport 640px) the state table reflows into labelled cards:
  headers are hidden, every value has a visible label, and Override is visible
  without horizontal scrolling.
- Banner dismiss buttons have explicit accessible names; workspace buttons expose
  `aria-current`.
- Status uses text + color + dots/labels; taint has a text label and title.
- Long state JSON is clipped inside a fixed table column and exposes the full
  value through `title`.
- Numeric values use tabular numerals.
- `prefers-reduced-motion: reduce` disables non-essential motion.
- Destructive console actions use confirmation dialogs.
- Empty, loading and error states exist for every governance panel.
- Core text/background and control-border pairs measured with
  `scripts/check_contrast.py`.

## Contrast evidence

Command: `python scripts/check_contrast.py`

```
PASS text-primary/page        17.63:1 (min 4.5:1)
PASS text-secondary/page       9.34:1 (min 4.5:1)
PASS text-muted/page           6.68:1 (min 4.5:1)
PASS text-muted/panel          6.11:1 (min 4.5:1)
PASS text-secondary/panel      8.54:1 (min 4.5:1)
PASS primary-button            6.29:1 (min 4.5:1)
PASS accent/page               9.94:1 (min 4.5:1)
PASS accent-strong/page       13.28:1 (min 4.5:1)
PASS success/page             10.30:1 (min 4.5:1)
PASS danger/page              10.48:1 (min 4.5:1)
PASS warning/page             13.74:1 (min 4.5:1)
PASS info/page                10.99:1 (min 4.5:1)
PASS proposal/page            10.73:1 (min 4.5:1)
PASS control-border/panel      3.03:1 (min 3.0:1)
PASS focus-ring/panel          9.09:1 (min 3.0:1)
```

## Manual checks still required

- Keyboard walk-through of the full console: automated Tab injection in the
  in-app browser still does not move focus, so the final manual pass is pending.
  Structural checks passed: only one tab stop in the tablist, focusable content
  controls exist, ArrowLeft/Right/Home/End handlers switch tabs, and Escape
  returns focus to the New task trigger.
- 200% text zoom and 400% reflow at 320px. Layout metrics at 320/390px pass;
  the 640px CSS viewport used for 200% table reflow was checked and the table
  stacked without page-level horizontal overflow.
- Automated axe/Playwright is not configured in this repository; visual
  regression is therefore **NOT TESTED** in this pass.
- Safari/Firefox rendering was not exercised; Chrome-based checks only.

## Known risks

- The console is dark-only by product decision; forced-colors receives a
  border/outline fallback but was not tested on a native high-contrast device.
- Legacy declarations still contain small values in a few non-visible rules;
  the v3.3 component layer raises every visible caption/chip to at least 10.5px.
- Mobile screenshots intermittently timed out in the in-app browser after
  viewport changes; DOM measurements and one successful 390px capture were used
  as evidence, and the remaining visual pass is marked incomplete.
