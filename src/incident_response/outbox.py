"""Durable ticket outbox dispatcher with leased, bounded retry delivery."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

from .models import ExternalReference, Incident, OutboxMessage

logger = logging.getLogger(__name__)


class TicketClient(Protocol):
    async def create(
        self,
        incident: Incident,
        *,
        idempotency_key: str,
    ) -> ExternalReference:
        ...


class OutboxStore(Protocol):
    def get(self, incident_id: str) -> Incident | None:
        ...

    def claim_outbox(self, *, now: float, lease_seconds: float) -> OutboxMessage | None:
        ...

    def complete_outbox(
        self,
        message: OutboxMessage,
        reference: ExternalReference,
    ) -> bool:
        ...

    def record_outbox_failure(
        self,
        message: OutboxMessage,
        *,
        error: str,
        now: float,
        max_attempts: int,
        retry_base_seconds: float,
        retry_max_seconds: float,
    ) -> bool:
        ...


class OutboxDispatcher:
    def __init__(
        self,
        *,
        store: OutboxStore,
        ticket_clients: Mapping[str, TicketClient],
        max_attempts: int = 5,
        retry_base_seconds: float = 1,
        retry_max_seconds: float = 60,
        lease_seconds: float = 60,
        poll_seconds: float = 0.5,
        notifier: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._store = store
        self._ticket_clients = dict(ticket_clients)
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._lease_seconds = lease_seconds
        self._poll_seconds = poll_seconds
        self._notifier = notifier
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def dispatch_once(self, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        message = self._store.claim_outbox(now=now, lease_seconds=self._lease_seconds)
        if message is None:
            return False
        try:
            incident = self._store.get(message.aggregate_id)
            if incident is None:
                raise RuntimeError("Outbox incident does not exist")
            client = self._ticket_clients.get(message.destination)
            if client is None:
                raise RuntimeError(f"No ticket client for {message.destination}")
            reference = await client.create(
                incident,
                idempotency_key=message.idempotency_key,
            )
            if not self._store.complete_outbox(message, reference):
                logger.warning("outbox_completion_lease_lost", extra={"outbox_id": message.id})
            elif self._notifier is not None:
                await self._notifier(message.aggregate_id)
        except Exception as exc:
            self._store.record_outbox_failure(
                message,
                error=str(exc),
                now=now,
                max_attempts=self._max_attempts,
                retry_base_seconds=self._retry_base_seconds,
                retry_max_seconds=self._retry_max_seconds,
            )
            logger.warning(
                "outbox_delivery_failed",
                extra={"outbox_id": message.id, "destination": message.destination},
            )
        return True

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            worked = await self.dispatch_once()
            if not worked:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_seconds)
                except TimeoutError:
                    pass
