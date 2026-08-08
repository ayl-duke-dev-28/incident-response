"""Local and Redis-backed incident change notifications for SSE consumers."""

from __future__ import annotations

import asyncio
import hashlib
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from .models import Incident


def incident_version(incident: Incident) -> str:
    return hashlib.sha256(incident.model_dump_json().encode()).hexdigest()[:20]


class IncidentEventBroker:
    def __init__(
        self,
        redis: Any | None = None,
        *,
        namespace: str = "incident-response",
        heartbeat_seconds: float = 15,
    ) -> None:
        self._redis = redis
        self._channel_prefix = f"{namespace}:incident-events:"
        self._heartbeat_seconds = heartbeat_seconds
        self._subscribers: dict[str, set[asyncio.Queue[bool]]] = defaultdict(set)

    def _channel(self, incident_id: str) -> str:
        return f"{self._channel_prefix}{incident_id}"

    async def publish(self, incident_id: str) -> None:
        for queue in tuple(self._subscribers.get(incident_id, ())):
            if queue.empty():
                queue.put_nowait(True)
        publish = getattr(self._redis, "publish", None)
        if callable(publish):
            await publish(self._channel(incident_id), incident_id)

    async def events(self, incident_id: str) -> AsyncIterator[bool]:
        queue: asyncio.Queue[bool] = asyncio.Queue(maxsize=1)
        self._subscribers[incident_id].add(queue)
        pubsub = None
        pubsub_factory = getattr(self._redis, "pubsub", None)
        if callable(pubsub_factory):
            pubsub = pubsub_factory()
            await pubsub.subscribe(self._channel(incident_id))
        try:
            while True:
                local_task = asyncio.create_task(queue.get())
                tasks: set[asyncio.Task[Any]] = {local_task}
                if pubsub is not None:
                    tasks.add(
                        asyncio.create_task(
                            pubsub.get_message(
                                ignore_subscribe_messages=True,
                                timeout=self._heartbeat_seconds,
                            )
                        )
                    )
                done, pending = await asyncio.wait(
                    tasks,
                    timeout=self._heartbeat_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                yield any(task.result() is not None for task in done)
        finally:
            self._subscribers[incident_id].discard(queue)
            if not self._subscribers[incident_id]:
                self._subscribers.pop(incident_id, None)
            if pubsub is not None:
                await pubsub.unsubscribe(self._channel(incident_id))
                await pubsub.aclose()
