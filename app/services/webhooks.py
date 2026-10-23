"""Persistent webhook rules, matching, signed delivery, and retries."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session_factory
from app.errors import ApiError, NotFound
from app.models import WebhookDelivery, WebhookRule, WorkspaceEvent
from app.schemas import (
    WebhookDeliveryListResponse,
    WebhookDeliveryOut,
    WebhookRuleCreateRequest,
    WebhookRuleListResponse,
    WebhookRuleOut,
    WebhookRuleUpdateRequest,
)
from app.services.common import (
    append_event,
    db_now,
    domain_event_type,
    event_identity,
    utcnow,
)
from app.services.events import event_out
from app.services.outbound import new_secret, send_json, validate_target

logger = logging.getLogger("agentgraph.webhooks")
settings = get_settings()


WEBHOOKS_PER_WORKSPACE = 10


def _rule_out(rule: WebhookRule) -> WebhookRuleOut:
    return WebhookRuleOut(
        id=rule.id,
        name=rule.name,
        url=rule.url,
        secret_set=bool(rule.secret),
        event_types=list(rule.event_types or ["*"]),
        tables=list(rule.tables or []),
        namespaces=list(rule.namespaces or []),
        active=rule.active,
        last_event_id=rule.last_event_id,
        consecutive_failures=rule.consecutive_failures,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


def _delivery_out(row: WebhookDelivery) -> WebhookDeliveryOut:
    return WebhookDeliveryOut(
        id=row.id,
        rule_id=row.rule_id,
        event_id=row.event_id,
        status=row.status,
        attempts=row.attempts,
        response_status=row.response_status,
        last_error=row.last_error,
        next_attempt_at=row.next_attempt_at,
        created_at=row.created_at,
        delivered_at=row.delivered_at,
    )


def event_type(event: dict[str, Any]) -> str:
    return domain_event_type(
        event.get("table"),
        event.get("action"),
        event.get("payload"),
    )


def _matches(rule: WebhookRule, event: dict[str, Any]) -> bool:
    table = event.get("table")
    namespace = event.get("namespace")
    if rule.tables and table not in rule.tables:
        return False
    if rule.namespaces and namespace not in rule.namespaces:
        return False
    if rule.event_types and "*" not in rule.event_types:
        candidates = {
            event_type(event),
            str(table or "").upper(),
            f"{str(table or '').upper()}.{str(event.get('action') or '').upper()}",
        }
        if not candidates.intersection({item.upper() for item in rule.event_types}):
            return False
    return True


async def list_rules(
    session: AsyncSession, workspace_id: uuid.UUID
) -> WebhookRuleListResponse:
    rows = (
        await session.execute(
            select(WebhookRule)
            .where(WebhookRule.workspace_id == workspace_id)
            .order_by(WebhookRule.created_at.desc())
        )
    ).scalars().all()
    return WebhookRuleListResponse(items=[_rule_out(row) for row in rows])


async def create_rule(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    request: WebhookRuleCreateRequest,
) -> WebhookRuleOut:
    await validate_target(request.url)
    existing = await session.scalar(
        select(func.count()).select_from(WebhookRule).where(
            WebhookRule.workspace_id == workspace_id))
    if (existing or 0) >= WEBHOOKS_PER_WORKSPACE:
        raise ApiError(402, "quota_exceeded", "a workspace has at most 10 webhooks",
                       details={"limit": WEBHOOKS_PER_WORKSPACE, "active": int(existing)})
    latest_event_id = await session.scalar(
        select(func.max(WorkspaceEvent.id)).where(
            WorkspaceEvent.workspace_id == workspace_id
        )
    )
    rule = WebhookRule(
        workspace_id=workspace_id,
        name=request.name,
        url=request.url,
        last_event_id=int(latest_event_id or 0),
        secret=request.secret or new_secret(),
        event_types=request.event_types,
        tables=request.tables,
        namespaces=request.namespaces,
        active=request.active,
    )
    session.add(rule)
    await session.commit()
    out = _rule_out(rule)
    if request.secret is None:
        # A server-generated secret is shown once (AG-EV-05 point 1).
        out = out.model_copy(update={"secret": rule.secret})
    return out


async def update_rule(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    rule_id: uuid.UUID,
    request: WebhookRuleUpdateRequest,
) -> WebhookRuleOut:
    rule = await session.get(WebhookRule, rule_id)
    if rule is None or rule.workspace_id != workspace_id:
        raise NotFound("webhook_rule")
    payload = request.model_dump(exclude_unset=True)
    if payload.get("url"):
        await validate_target(payload["url"])
    if "event_types" in payload and payload["event_types"] is not None:
        payload["event_types"] = [
            item.strip().upper() for item in payload["event_types"] if item.strip()
        ] or ["*"]
    for key, value in payload.items():
        setattr(rule, key, value)
    rule.updated_at = utcnow()
    await session.commit()
    return _rule_out(rule)


async def delete_rule(
    session: AsyncSession, workspace_id: uuid.UUID, rule_id: uuid.UUID
) -> None:
    rule = await session.get(WebhookRule, rule_id)
    if rule is None or rule.workspace_id != workspace_id:
        raise NotFound("webhook_rule")
    await session.delete(rule)
    await session.commit()


async def test_rule(
    session: AsyncSession, workspace_id: uuid.UUID, rule_id: uuid.UUID
) -> dict[str, Any]:
    rule = await session.get(WebhookRule, rule_id)
    if rule is None or rule.workspace_id != workspace_id:
        raise NotFound("webhook_rule")
    result = await send_json(
        rule.url,
        {
            "event": "webhook.test",
            "rule_id": str(rule.id),
            "workspace_id": str(workspace_id),
            "timestamp": utcnow().isoformat(),
        },
        secret=rule.secret,
        event_name="webhook.test",
    )
    return {
        "ok": result.ok,
        "status_code": result.status_code,
        "error": result.error,
        "duration_ms": result.duration_ms,
    }


async def list_deliveries(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    rule_id: uuid.UUID | None = None,
    limit: int = 100,
) -> WebhookDeliveryListResponse:
    query = select(WebhookDelivery).where(
        WebhookDelivery.workspace_id == workspace_id
    )
    if rule_id:
        query = query.where(WebhookDelivery.rule_id == rule_id)
    rows = (
        await session.execute(
            query.order_by(WebhookDelivery.created_at.desc()).limit(min(limit, 500))
        )
    ).scalars().all()
    return WebhookDeliveryListResponse(items=[_delivery_out(row) for row in rows])


def _event_dict(row: WorkspaceEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        **event_identity(row.id),
        "table": row.source_table,
        "action": row.action,
        "workspace_id": str(row.workspace_id),
        "record_id": str(row.record_id) if row.record_id else None,
        "actor": row.actor,
        "version": row.version,
        "status": row.status,
        "slot_key": row.slot_key,
        "task_key": row.task_key,
        "doc_key": row.doc_key,
        "namespace": row.namespace,
        "assigned_role": row.assigned_role,
        "locked_by_human": row.locked_by_human,
        "actor_stamp": (row.payload or {}).get("actor_stamp"),
        "event_type": domain_event_type(
            row.source_table, row.action, row.payload
        ),
        "payload": row.payload or {},
        "timestamp": row.created_at.isoformat(),
    }


async def enqueue_deliveries(session: AsyncSession) -> int:
    rules = (
        await session.execute(
            select(WebhookRule).where(WebhookRule.active.is_(True))
        )
    ).scalars().all()
    created = 0
    for rule in rules:
        rows = (
            await session.execute(
                select(WorkspaceEvent)
                .where(
                    WorkspaceEvent.workspace_id == rule.workspace_id,
                    WorkspaceEvent.id > rule.last_event_id,
                )
                .order_by(WorkspaceEvent.id.asc())
                .limit(500)
            )
        ).scalars().all()
        if not rows:
            continue
        for row in rows:
            event = _event_dict(row)
            if not _matches(rule, event):
                continue
            statement = (
                pg_insert(WebhookDelivery)
                .values(
                    workspace_id=rule.workspace_id,
                    rule_id=rule.id,
                    event_id=row.id,
                    status="PENDING",
                    attempts=0,
                    next_attempt_at=utcnow(),
                )
                .on_conflict_do_nothing(
                    index_elements=["rule_id", "event_id"]
                )
            )
            result = await session.execute(statement)
            if result.rowcount:
                created += 1
        rule.last_event_id = max(row.id for row in rows)
    await session.commit()
    return created


async def process_deliveries(session: AsyncSession) -> int:
    now = await db_now(session)
    rows = (
        await session.execute(
            select(WebhookDelivery)
            .where(
                WebhookDelivery.status == "PENDING",
                WebhookDelivery.next_attempt_at <= now,
            )
            .order_by(WebhookDelivery.created_at.asc())
            .with_for_update(skip_locked=True)
            .limit(settings.webhook_batch_size)
        )
    ).scalars().all()
    delivered = 0
    for delivery in rows:
        rule = await session.get(WebhookRule, delivery.rule_id)
        event = await session.get(WorkspaceEvent, delivery.event_id)
        if rule is None or event is None:
            delivery.status = "FAILED"
            delivery.last_error = "rule or event no longer exists"
            continue
        if not rule.active:
            delivery.status = "CANCELED"
            delivery.last_error = "rule is inactive"
            continue
        body = _event_dict(event)
        # AG-EV-05: the AG-EV-01 event shape travels with the legacy fields.
        body["event"] = event_out(event)
        result = await send_json(
            rule.url,
            body,
            secret=rule.secret,
            event_name=event_type(body),
            idempotency_key=f"ag-{delivery.id}",
            headers={"X-AgentGraph-Event-Seq": str(event.seq or "")},
        )
        delivery.attempts += 1
        delivery.response_status = result.status_code
        if result.ok:
            delivery.status = "SUCCEEDED"
            delivery.delivered_at = utcnow()
            delivery.last_error = None
            rule.consecutive_failures = 0
            rule.failing_since = None
            delivered += 1
        else:
            delivery.last_error = result.error or "delivery failed"
            if delivery.attempts >= settings.webhook_max_delivery_attempts:
                delivery.status = "FAILED"
            else:
                # One minute, doubling, at most six hours (AG-EV-05 point 3).
                delay = min(60 * (2 ** (delivery.attempts - 1)), 6 * 3_600)
                delivery.next_attempt_at = now + timedelta(seconds=delay)
            rule.consecutive_failures += 1
            rule.failing_since = rule.failing_since or now
            if now - rule.failing_since >= timedelta(hours=24) and rule.active:
                rule.active = False
                rule.disabled_at = now
                await append_event(
                    session, workspace_id=rule.workspace_id, agent="webhooks",
                    event_type="WEBHOOK_DISABLED",
                    payload={"details": {"webhook_id": str(rule.id), "url": rule.url,
                                         "failing_since": rule.failing_since.isoformat()}})
    await session.commit()
    return delivered


async def webhook_delivery_loop() -> None:
    import asyncio

    while True:
        try:
            async with async_session_factory() as session:
                created = await enqueue_deliveries(session)
                delivered = await process_deliveries(session)
                if created or delivered:
                    logger.info(
                        "webhooks: enqueued=%s delivered=%s", created, delivered
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - background resilience
            logger.exception("webhook dispatcher iteration failed")
        await asyncio.sleep(settings.outbound_dispatch_interval_seconds)
