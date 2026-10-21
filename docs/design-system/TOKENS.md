# AgentGraph Design Tokens

The token layer lives in `dashboard/app/design-tokens.css`; the v3.3 component
contract is appended to `dashboard/app/v3.css` so it always wins over the legacy
v3.2 declarations. Existing component CSS keeps its short aliases (`--bg`,
`--surface`, `--text`, `--accent`, …) mapped to semantic tokens.

## Architecture

1. **Primitive** — real graphite/slate ramp plus teal, violet, blue, amber,
   ruby and jade status colors.
2. **Semantic** — surface, text, border, action, focus and status roles.
3. **Component** — component CSS uses the aliases and semantic `--ds-*` tokens.

## Core mapping

| Semantic token | Value / role | Existing alias |
|---|---|---|
| `--ds-surface-page` | `#080a0e` app background | `--bg` |
| `--ds-surface-panel` | `#12161e` panels | `--surface`, `--panel` |
| `--ds-surface-raised` | `#171d27` selected/raised surfaces | `--surface-2` |
| `--ds-surface-inset` | `#0a0d12` inputs/code blocks | — |
| `--ds-border-subtle` | `#232d3b` dividers | `--line` |
| `--ds-border-default` | `#2c3848` quiet card borders | — |
| `--ds-border-control` | `#52657d` interactive borders (3.03:1) | `--line-strong` |
| `--ds-text-primary` | `#eef2f8` (17.63:1 on page) | `--text` |
| `--ds-text-secondary` | `#a7b3c5` (9.34:1 on page) | `--muted` |
| `--ds-text-muted` | `#8a97a9` (6.68:1 page / 6.11:1 panel) | `--muted-2` |
| `--ds-action-primary` | `#2dd4bf`; fg `#041512` (10.06:1) | `--accent` |
| `--ds-status-warning` | `#fcd34d` | `--amber` |
| `--ds-status-danger` | `#fda4af` | `--danger` |
| `--ds-status-info` | `#93c5fd` | `--sky` |
| `--ds-status-proposal` | `#c4b5fd` | `--violet` |
| `--ds-focus-ring` | `#5eead4` (12.25:1) | `:focus-visible` |
| `--ds-radius-sm/md/lg` | 6 / 10 / 14px | `--radius-sm`, `--radius` |
| `--ds-motion-fast/normal` | 120 / 180ms | transitions |

## Rules for new code

- Do not add a raw hex value when a semantic token exists.
- A new color must have a role (surface, text, border, action or status), not
  just a hue.
- Any raw value needed for a third-party logo or illustrative asset must be
  documented as an exception.
- New spacing should use the 4px rhythm; avoid 7/11/13/19px one-offs.
- Use `--ds-*` names in new components and keep the legacy aliases for existing
  component CSS.
- Interactive controls use `--ds-border-control`; dividers use
  `--ds-border-subtle`/`--ds-border-default`.

## Known exceptions

The legacy v3.2 declarations still contain a few decorative alpha values, but
the v3.3 component layer overrides all visible cards, buttons, tabs, tables and
banners to flat semantic surfaces. Remaining gradients are limited to legacy
rules that are no longer applied to visible console components; they are tracked
as an incremental cleanup item rather than a second theme.
