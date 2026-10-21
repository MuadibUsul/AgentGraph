from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.engine.formatter import (
    compact_json,
    estimate_tokens,
    preview,
    render_document,
    render_handoff_brief,
    render_slot,
    render_task_card,
    time_ago,
)


def document(**overrides):
    base = dict(
        doc_key="strategy/weekly",
        title="Weekly plan",
        content="# Weekly plan\n\n- stay defensive",
        frontmatter={"status": "ACTIVE", "risk_limit_pct": 2.5},
        version=3,
        namespace="default",
        updated_by="executor_1",
        locked_by_human=False,
        human_note=None,
        verification_status="DRAFT",
        trust_level="UNTRUSTED",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def task(**overrides):
    base = dict(
        task_key="TASK-1",
        title="Review risk",
        status=SimpleNamespace(value="REVIEWING"),
        assigned_role="AUDITOR",
        priority=80,
        version=3,
        claimed_by="auditor_1",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        ping_pong_count=1,
        max_retries=3,
        handoff_brief={
            "summary": "risk model changed",
            "key_metrics": {"var": 0.03},
            "unverified_hypotheses": ["vol stays low"],
            "risk_flag": True,
        },
        artifact_uri="doc://artifact/TASK-1/abc",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def slot(**overrides):
    base = dict(
        slot_key="trading:regime",
        version=4,
        updated_by="executor_1",
        locked_by="executor_1",
        locked_by_human=False,
        current_state={"bias": "LONG", "confidence": 0.8},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_document_frontmatter_view():
    rendered = render_document(document())
    assert rendered.startswith("---\n")
    assert "doc_key: strategy/weekly" in rendered
    assert "version: 3" in rendered
    assert "status: ACTIVE" in rendered
    assert "risk_limit_pct: 2.5" in rendered
    assert rendered.rstrip().endswith(
        "- stay defensive" + chr(10) + "[[/UNTRUSTED_SOURCE]]"
    )


def test_document_preview_without_content():
    rendered = render_document(document(content="x" * 1000), include_content=False)
    assert "x" * 1000 not in rendered
    assert "..." in rendered


def test_handoff_brief_render():
    rendered = render_handoff_brief(
        "TASK-1", task().handoff_brief, artifact_uri=task().artifact_uri
    )
    assert "TASK-1" in rendered
    assert "risk model changed" in rendered
    assert "artifact_uri: doc://" in rendered
    assert "risk_flag: True" in rendered


def test_task_card_and_slot_render():
    card = render_task_card(task())
    assert "REVIEWING" in card
    assert "ping_pong=1/3" in card
    slot_line = render_slot(slot())
    assert "trading:regime" in slot_line
    assert "v4" in slot_line
    assert 'bias' in slot_line


def test_time_preview_tokens():
    now = datetime.now(timezone.utc)
    assert time_ago(now, now) == "just now"
    assert time_ago(now - timedelta(minutes=4), now) == "4m ago"
    assert time_ago(now - timedelta(hours=3), now) == "3h ago"
    assert preview("a b c") == "a b c"
    assert compact_json({"a": 1, "b": 2}) == '{"a":1,"b":2}'
    assert estimate_tokens("x" * 400) == 100
