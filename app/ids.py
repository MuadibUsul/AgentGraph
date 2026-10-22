"""Canonical object ids: ``<prefix>_<ULID>`` (PRD §2.1, AG-ST-17).

Ids are opaque, never change and are never reused. The ULID part is 48 bits of
millisecond time plus 80 random bits in Crockford base32, so ids sort by
creation time. The database function ``agentgraph_new_id`` uses the same format.
"""

from __future__ import annotations

import secrets
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid() -> str:
    millis = int(time.time() * 1000)
    head = "".join(_ALPHABET[(millis >> (5 * i)) & 31] for i in reversed(range(10)))
    tail = "".join(_ALPHABET[secrets.randbelow(32)] for _ in range(16))
    return head + tail


def new_id(prefix: str) -> str:
    return f"{prefix}_{ulid()}"
