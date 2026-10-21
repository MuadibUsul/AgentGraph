"""Single-process fan-out for PostgreSQL workspace_events."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from app.config import get_settings
from app.database import asyncpg_dsn

logger = logging.getLogger("agentgraph.events")
settings = get_settings()


class EventBus:
    """One LISTEN connection per process; subscribe() gives isolated queues."""

    def __init__(self) -> None:
        self._connection: asyncpg.Connection | None = None
        self._subscribers: dict[uuid.UUID, asyncio.Queue[str]] = {}

    async def start(self) -> None:
        if self._connection is not None:
            return
        self._connection = await asyncpg.connect(
            asyncpg_dsn(), timeout=settings.db_pool_timeout
        )
        await self._connection.add_listener(
            "workspace_events", self._listener
        )
        logger.info("workspace event bus listening")

    def _listener(
        self,
        connection: asyncpg.Connection,
        pid: int,
        channel: str,
        payload: str,
    ) -> None:
        for queue in list(self._subscribers.values()):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Outbox replay is the recovery path; never block PostgreSQL.
                try:
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[str]]:
        token = uuid.uuid4()
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1_000)
        self._subscribers[token] = queue
        try:
            yield queue
        finally:
            self._subscribers.pop(token, None)

    async def stop(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                await connection.remove_listener(
                    "workspace_events", self._listener
                )
            finally:
                await connection.close()


event_bus = EventBus()
