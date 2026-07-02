"""In-process async worker queue.

The webhook enqueues an alert and returns 202 immediately. A background worker task
started at app startup drains the queue and runs full triage. This keeps webhook
latency in single-digit ms even when Claude takes 3s to respond.

Multi-process deployments should swap this for Redis + arq / Celery / Dramatiq —
the `AlertQueue` interface has one method (`submit`) so the swap is mechanical.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from .models import Alert

logger = logging.getLogger(__name__)


AlertHandler = Callable[[Alert], Awaitable[None]]


class AlertQueue:
    def __init__(self, handler: AlertHandler, maxsize: int = 1000) -> None:
        self._handler = handler
        self._queue: asyncio.Queue[Alert] = asyncio.Queue(maxsize=maxsize)
        self._worker: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def submit(self, alert: Alert) -> None:
        await self._queue.put(alert)

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._stopping.clear()
            self._worker = asyncio.create_task(self._run(), name="incident-worker")

    async def stop(self) -> None:
        self._stopping.set()
        if self._worker:
            await self._queue.join()
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):
                pass
            self._worker = None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                alert = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self._handler(alert)
            except Exception:
                logger.exception("worker_handler_failed", extra={"alert_id": alert.id})
            finally:
                self._queue.task_done()

    def qsize(self) -> int:
        return self._queue.qsize()
