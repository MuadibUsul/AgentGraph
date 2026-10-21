"""Registry coverage: every raised code is registered (PRD AG-ST-18 acceptance 4/5)."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from app.error_registry import ERROR_CLASSES, REGISTRY, WARNING_CODES, classify, spec_for
from app.errors import ApiError, RateLimited

ROOT = Path(__file__).resolve().parent.parent
_RAISE = re.compile(
    r"(?:ApiError|_tool_error|super\(\)\.__init__|error_body)\(\s*(?:\d{3}\s*,\s*)?\"([A-Za-z_]+)\"",
    re.S,
)
_DICT_CODE = re.compile(r"\"code\":\s*\"([A-Za-z_]+)\"")
_WARNING = re.compile(r"add_warning\(\s*\"([a-z_]+)\"", re.S)
# Not error codes: resource names passed to helper constructors.
_IGNORED = {"context_slot"}


def _codes_in_source() -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        if path.name == "error_registry.py":
            continue
        text = path.read_text(encoding="utf-8")
        found.update(_RAISE.findall(text))
        found.update(code for code in _DICT_CODE.findall(text) if code.islower())
    return found - _IGNORED


def test_every_raised_code_is_registered():
    missing = sorted(
        code for code in _codes_in_source() if code not in REGISTRY and code not in WARNING_CODES
    )
    assert not missing, f"register these error codes in app/error_registry.py: {missing}"


def test_every_warning_code_is_registered():
    found: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        found.update(_WARNING.findall(path.read_text(encoding="utf-8")))
    missing = sorted(found - set(WARNING_CODES))
    assert not missing, f"register these warning codes in WARNING_CODES: {missing}"
    assert not set(WARNING_CODES) & set(REGISTRY)


def test_registry_entries_use_known_classes():
    for code, spec in REGISTRY.items():
        assert spec.error_class in ERROR_CLASSES, code
        assert 400 <= spec.status <= 599, code


def test_error_body_carries_protocol_fields():
    body = RateLimited(3).body()["error"]
    assert body["class"] == "retry_after"
    assert body["action"] == "wait_retry_after_then_retry_same_request"
    assert body["retryable"] is True
    assert body["retry_after_ms"] == 3000
    conflict = ApiError(409, "version_conflict", "stale").body()["error"]
    assert conflict["class"] == "refresh_and_retry"
    assert conflict["retry_after_ms"] is None


def test_fix_request_codes_are_not_retryable():
    for code, spec in REGISTRY.items():
        if spec.error_class == "fix_request":
            assert classify(code, spec.status)["retryable"] is False, code


def test_unknown_codes_fall_back_by_status():
    assert spec_for("brand_new", 503).error_class == "retry_after"
    assert spec_for("brand_new", 401).error_class == "reauth"


def test_generated_error_doc_is_current():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_error_codes.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
