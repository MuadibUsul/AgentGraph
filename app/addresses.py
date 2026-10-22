"""Address grammar (PRD AG-ST-17 point 3): canonical ids, namespaces, doc keys.

The grammar is normative from API version ``2026-11``. Baseline clients keep
3.2.1 behaviour: keys that break the grammar are still accepted but flagged as
legacy aliases with a warning, because rejecting them would be a semantic
change inside one version (PRD invariant I12).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CANONICAL_PREFIXES = ("ten", "ws", "doc", "task", "slot", "prn", "tok", "lease", "evt")
_CANONICAL = re.compile(r"^(?P<prefix>[a-z]+)_[0-9A-HJKMNP-TV-Z]{26}$")
NAMESPACE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
SEGMENT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
RESERVED_NAMESPACES = frozenset({"system", "handoff", "reviews", "tasks", "ag"})
MAX_DOC_KEY_LENGTH = 256
MAX_DOC_KEY_SEGMENTS = 8


@dataclass(frozen=True)
class AddressViolation:
    field: str
    rule: str
    value: str

    def details(self) -> dict[str, str]:
        return {"field": self.field, "rule": self.rule, "value": self.value}


def canonical_prefix(value: str) -> str | None:
    match = _CANONICAL.match(value or "")
    if match and match.group("prefix") in CANONICAL_PREFIXES:
        return match.group("prefix")
    return None


def is_canonical(value: str, prefix: str | None = None) -> bool:
    found = canonical_prefix(value)
    return found is not None and (prefix is None or found == prefix)


def namespace_violation(namespace: str, *, allow_reserved: bool = False) -> AddressViolation | None:
    if not allow_reserved and (namespace in RESERVED_NAMESPACES or namespace.startswith("_")):
        return AddressViolation("namespace", "reserved_namespace", namespace)
    if namespace != namespace.lower() and NAMESPACE_PATTERN.match(namespace.lower()):
        return AddressViolation("namespace", "lowercase_only", namespace)
    if not NAMESPACE_PATTERN.match(namespace):
        return AddressViolation("namespace", "namespace_charset", namespace)
    return None


def doc_key_violation(doc_key: str) -> AddressViolation | None:
    if len(doc_key) > MAX_DOC_KEY_LENGTH:
        return AddressViolation("doc_key", "max_length_256", doc_key)
    segments = doc_key.split("/")
    if len(segments) > MAX_DOC_KEY_SEGMENTS:
        return AddressViolation("doc_key", "max_8_segments", doc_key)
    for segment in segments:
        if segment in (".", ".."):
            return AddressViolation("doc_key", "no_dot_segments", doc_key)
        if segment.startswith("_"):
            return AddressViolation("doc_key", "reserved_underscore_segment", doc_key)
        if segment != segment.lower() and SEGMENT_PATTERN.match(segment.lower()):
            return AddressViolation("doc_key", "lowercase_only", doc_key)
        if not SEGMENT_PATTERN.match(segment):
            return AddressViolation("doc_key", "segment_charset", doc_key)
    return None


def is_legacy_alias(namespace: str, doc_key: str) -> bool:
    return (
        namespace_violation(namespace, allow_reserved=True) is not None
        or doc_key_violation(doc_key) is not None
    )
