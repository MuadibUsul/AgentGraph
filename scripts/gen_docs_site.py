"""Generate the public docs site content from app/docs_site.py.

Usage: python scripts/gen_docs_site.py [--check]
Writes dashboard/app/docs/site.generated.json, which the Next.js docs pages
render. With --check the script exits 1 when the committed file is stale.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Generation needs the app's OpenAPI schema, not a database or real settings.
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("EMBEDDING_PROVIDER", "hash")

from app.docs_site import build, dumps  # noqa: E402

TARGET = ROOT / "dashboard" / "app" / "docs" / "site.generated.json"


def main() -> int:
    content = dumps(build())
    if "--check" in sys.argv:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != content:
            print(f"{TARGET.relative_to(ROOT)} is out of date; run scripts/gen_docs_site.py")
            return 1
        return 0
    TARGET.write_text(content, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
