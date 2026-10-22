from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.schemas import (
    AgentTokenCreateRequest,
    DocumentUpsertRequest,
    HandoffBrief,
    HumanSlotUpdateRequest,
    LinkCreateRequest,
    SlotReplaceRequest,
    TaskCreateRequest,
    TaskHandoffRequest,
    TaskReviewRequest,
)


def test_handoff_brief_requires_contract_fields():
    with pytest.raises(ValidationError):
        HandoffBrief(summary="only summary")
    brief = HandoffBrief(
        summary="ok",
        key_metrics={"x": 1},
        unverified_hypotheses=[],
        risk_flag=False,
    )
    assert brief.risk_flag is False


def test_handoff_requires_brief_for_review_states():
    with pytest.raises(ValidationError):
        TaskHandoffRequest(next_status="REVIEWING")
    request = TaskHandoffRequest(
        next_status="REVIEWING",
        brief=HandoffBrief(
            summary="ok",
            key_metrics={},
            unverified_hypotheses=[],
            risk_flag=False,
        ),
    )
    assert request.brief is not None
    failed = TaskHandoffRequest(next_status="FAILED")
    assert failed.brief is None


def test_artifact_uri_must_be_doc_uri():
    with pytest.raises(ValidationError):
        TaskHandoffRequest(
            next_status="DONE",
            brief=HandoffBrief(
                summary="ok",
                key_metrics={},
                unverified_hypotheses=[],
                risk_flag=False,
            ),
            artifact_uri="https://example.com/x",
        )


def test_review_decision_constraints():
    # One validator serves REST and MCP (PRD AG-CO-01): it lives in the service.
    from app.errors import ApiError
    from app.services.tasks import validate_review_next_status

    with pytest.raises(ApiError) as approve_rework:
        validate_review_next_status("approve", "IN_PROGRESS")
    assert approve_rework.value.details["allowed"] == ["DONE", "HANDOFF", "READY"]
    with pytest.raises(ApiError):
        validate_review_next_status("reject", "DONE")
    with pytest.raises(ApiError) as unknown:
        validate_review_next_status("reject", "FOO")
    assert "IN_PROGRESS" in unknown.value.details["allowed"]
    validate_review_next_status("reject", "IN_PROGRESS")
    validate_review_next_status("approve", None)
    assert TaskReviewRequest(decision="approve").decision == "approve"


def test_human_slot_unlock_only():
    request = HumanSlotUpdateRequest(expected_version=2, unlock=True)
    assert request.unlock is True
    with pytest.raises(ValidationError):
        HumanSlotUpdateRequest(expected_version=2, lock=True, unlock=True)
    with pytest.raises(ValidationError):
        HumanSlotUpdateRequest(expected_version=2)


def test_slot_namespace_optional_and_size_guard():
    assert SlotReplaceRequest(state={}, expected_version=0).namespace is None
    with pytest.raises(ValidationError):
        SlotReplaceRequest(state={"blob": "x" * 100_001}, expected_version=0)


def test_document_requires_content_and_valid_namespace():
    with pytest.raises(ValidationError):
        DocumentUpsertRequest(content="", expected_version=0)
    with pytest.raises(ValidationError):
        DocumentUpsertRequest(
            content="x", expected_version=0, namespace="bad namespace"
        )
    assert DocumentUpsertRequest(content="x", expected_version=0).namespace is None


def test_agent_token_namespaces_are_sandboxed():
    token = AgentTokenCreateRequest(
        agent_signature="scout",
        role_scope="SCOUT",
        allowed_namespaces=["raw_inbox", "raw_inbox"],
    )
    assert token.allowed_namespaces == ["raw_inbox"]
    with pytest.raises(ValidationError):
        AgentTokenCreateRequest(
            agent_signature="scout",
            role_scope="SCOUT",
            allowed_namespaces=[],
        )


def test_task_dependencies_are_deduplicated():
    task = TaskCreateRequest(
        title="x", depends_on=["TASK-1", "TASK-1", "TASK-2"]
    )
    assert task.depends_on == ["TASK-1", "TASK-2"]
    with pytest.raises(ValidationError):
        TaskCreateRequest(title="x", task_key="bad key!")


def test_link_no_self_loop():
    node = uuid.uuid4()
    with pytest.raises(ValidationError):
        LinkCreateRequest(source_id=node, target_id=node)
