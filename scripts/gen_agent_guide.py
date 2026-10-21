"""Generate docs/AGENT_GUIDE.md from app/agent_guide.py.

Usage: python scripts/gen_agent_guide.py [--check]
With --check the script exits 1 when the committed file is out of date.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent_guide import render  # noqa: E402

TARGET = ROOT / "docs" / "AGENT_GUIDE.md"


def main() -> int:
    content = render(live=False)
    if "--check" in sys.argv:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != content:
            print("docs/AGENT_GUIDE.md is out of date; run scripts/gen_agent_guide.py")
            return 1
        return 0
    TARGET.write_text(content, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
