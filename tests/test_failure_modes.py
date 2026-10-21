from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

from app.config import get_settings
from app.database import apply_schema
from app.errors import ApiError
from app.events import EventBus
from app.services.outbound import signature, validate_target

settings = get_settings()


def test_webhook_signature_is_stable_and_body_bound() -> None:
    first = signature("whsec_test", 1_700_000_000, b'{"a":1}')
    second = signature("whsec_test", 1_700_000_000, b'{"a":1}')
    changed = signature("whsec_test", 1_700_000_000, b'{"a":2}')
    assert first == second
    assert first != changed
    assert len(first) == 64


def test_event_bus_keeps_latest_payload_when_subscriber_queue_is_full() -> None:
    bus = EventBus()
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
    bus._subscribers[uuid.uuid4()] = queue
    bus._listener(None, 1, "workspace_events", '{"id":1}')
    bus._listener(None, 1, "workspace_events", '{"id":2}')
    assert json.loads(queue.get_nowait())["id"] == 2


@pytest.mark.asyncio
async def test_private_outbound_target_is_blocked(monkeypatch) -> None:
    monkeypatch.setattr(settings, "outbound_allow_private_networks", False)
    with pytest.raises(ApiError) as caught:
        await validate_target("http://127.0.0.1:8080/hook")
    assert caught.value.code in (
        "outbound_private_address",
        "invalid_target_url",
    )


@pytest.mark.integration
@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required",
)
@pytest.mark.asyncio
async def test_schema_application_is_idempotent() -> None:
    await apply_schema()
    await apply_schema()
