"""Generate docs/ERROR_CODES.md from app/error_registry.py.

Usage: python scripts/gen_error_codes.py [--check]
With --check the script exits 1 when the committed file is out of date.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.error_registry import (  # noqa: E402
    ERROR_CLASSES,
    RETRY_POLICY,
    WARNING_CODES,
    registry_table,
)

TARGET = ROOT / "docs" / "ERROR_CODES.md"


def render() -> str:
    lines = [
        "# AgentGraph error codes",
        "",
        "Generated from `app/error_registry.py` by `scripts/gen_error_codes.py`;",
        "do not edit by hand. Every error body is",
        "`{\"error\": {\"code\", \"message\", \"class\", \"action\", \"retryable\",",
        "\"retry_after_ms\", \"details\"}}`.",
        "",
        "## Classes",
        "",
        "| class | action | retryable | rule |",
        "|---|---|---|---|",
    ]
    for name, meta in ERROR_CLASSES.items():
        lines.append(
            f"| `{name}` | `{meta['action']}` | {str(meta['retryable']).lower()} | {meta['rule']} |"
        )
    lines += [
        "",
        "## Retry algorithm",
        "",
        f"`{RETRY_POLICY['formula']}` with base {RETRY_POLICY['base_ms']} ms, cap "
        f"{RETRY_POLICY['cap_ms']} ms, at most {RETRY_POLICY['max_attempts']} attempts. "
        f"{RETRY_POLICY['idempotency']} {RETRY_POLICY['delivery']}",
        "",
        "## Codes",
        "",
        "| code | HTTP | class | action | rule |",
        "|---|---|---|---|---|",
    ]
    for row in registry_table():
        lines.append(
            f"| `{row['code']}` | {row['http']} | `{row['class']}` | `{row['action']}` "
            f"| {row['rule']} |"
        )
    lines += [
        "",
        "## Warnings",
        "",
        "Soft constraints reported in `warnings[]`; the request still succeeds.",
        "",
        "| code | meaning |",
        "|---|---|",
    ]
    lines += [f"| `{code}` | {meaning} |" for code, meaning in WARNING_CODES.items()]
    return "\n".join(lines) + "\n"


def main() -> int:
    content = render()
    if "--check" in sys.argv:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != content:
            print("docs/ERROR_CODES.md is out of date; run scripts/gen_error_codes.py")
            return 1
        return 0
    TARGET.write_text(content, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
