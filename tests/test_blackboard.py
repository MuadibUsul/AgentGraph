from __future__ import annotations

from datetime import timedelta

import pytest

from app.engine.blackboard import (
    apply_json_patch,
    lease_can_be_taken,
    lease_expiry,
    lease_is_active,
    parse_doc_uri,
    patch_markdown_section,
    utcnow,
    validate_handoff_brief,
)
from app.errors import ApiError, NotFound


def test_json_patch_add_replace_remove():
    state = {"bias": "LONG", "risk": {"max_drawdown": 0.02}, "status": "DRAFT"}
    patched, applied = apply_json_patch(
        state,
        [
            {"op": "replace", "path": "/bias", "value": "SHORT"},
            {"op": "add", "path": "/owner", "value": "executor"},
            {"op": "remove", "path": "/status"},
        ],
    )
    assert patched == {
        "bias": "SHORT",
        "risk": {"max_drawdown": 0.02},
        "owner": "executor",
    }
    assert len(applied) == 3
    assert state["bias"] == "LONG"  # input is not mutated


def test_json_patch_nested_replace():
    state = {"risk_model": {"max_drawdown": 0.02}}
    patched, _ = apply_json_patch(
        state,
        [{"op": "replace", "path": "/risk_model/max_drawdown", "value": 0.04}],
    )
    assert patched["risk_model"]["max_drawdown"] == 0.04


def test_invalid_json_patch_is_422():
    with pytest.raises(ApiError) as exc:
        apply_json_patch({"a": 1}, [{"op": "replace", "path": "/missing", "value": 2}])
    assert exc.value.status_code == 422
    assert exc.value.code == "invalid_json_patch"


def test_empty_patch_rejected():
    with pytest.raises(ApiError):
        apply_json_patch({"a": 1}, [])


def test_patch_markdown_section():
    content = "# Plan\n\n## Risk\nkeep 2%\n\n### Detail\nold detail\n\n## Levels\nbuy 64200\n"
    updated = patch_markdown_section(content, "Risk", "keep 3%\n\n### Detail\nnew detail")
    assert "keep 3%" in updated
    assert "new detail" in updated
    assert "old detail" not in updated
    assert "## Levels" in updated
    assert updated.startswith("# Plan")


def test_patch_markdown_section_not_found():
    with pytest.raises(NotFound):
        patch_markdown_section("# Plan\n\n## Risk\n2%", "Missing", "x")


def test_validate_handoff_brief():
    brief = validate_handoff_brief(
        {
            "summary": "BTC breakout confirmed",
            "key_metrics": {"price": 92450},
            "unverified_hypotheses": ["holds above 91800"],
            "risk_flag": False,
        }
    )
    assert brief["summary"] == "BTC breakout confirmed"
    with pytest.raises(ApiError):
        validate_handoff_brief({"summary": "only summary"})
    with pytest.raises(ApiError):
        validate_handoff_brief(
            {
                "summary": "x",
                "key_metrics": {},
                "unverified_hypotheses": [],
                "risk_flag": "false",
            }
        )


def test_leases():
    now = utcnow()
    expires = lease_expiry(60)
    assert expires > now + timedelta(seconds=50)
    assert lease_is_active("agent", expires)
    assert not lease_is_active("agent", now - timedelta(seconds=1))
    assert lease_can_be_taken("agent", expires, "agent")
    assert not lease_can_be_taken("agent", expires, "other")
    assert lease_can_be_taken("agent", now - timedelta(seconds=1), "other")


def test_parse_doc_uri():
    assert parse_doc_uri("doc://strategy/weekly") == "strategy/weekly"
    assert parse_doc_uri("doc:///strategy/weekly") == "strategy/weekly"
    with pytest.raises(ApiError):
        parse_doc_uri("https://example.com/report.md")
