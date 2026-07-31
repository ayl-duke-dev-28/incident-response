"""Async alert worker with optional SQLite durability.

The FastAPI app supplies its existing SQLite path, so submit persists an alert
before returning and a restarted worker can recover unfinished work. Direct unit
users may omit ``db_path`` to retain a purely in-memory queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Awaitable, Callable, Iterator

from .models import Alert

logger = logging.getLogger(__name__)


AlertHandler = Callable[[Alert], Awaitable[None]]

_QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_queue (
    alert_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS alert_queue_created_idx
ON alert_queue(created_at, alert_id);
"""


class _DurableAlertStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_QUEUE_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def enqueue(self, alert: Alert) -> bool:
        with self._conn() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO alert_queue (alert_id, payload)
                VALUES (?, ?)
                """,
                (alert.id, alert.model_dump_json()),
            )
            return cursor.rowcount == 1

    def get(self, alert_id: str) -> Alert | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload FROM alert_queue WHERE alert_id = ?",
                (alert_id,),
            ).fetchone()
        if row is None:
            return None
        return Alert.model_validate(json.loads(row["payload"]))

    def delete(self, alert_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM alert_queue WHERE alert_id = ?",
                (alert_id,),
            )

    def pending_ids(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT alert_id
                FROM alert_queue
                ORDER BY created_at ASC, alert_id ASC
                """
            ).fetchall()
        return [str(row["alert_id"]) for row in rows]

    def count(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM alert_queue").fetchone()
        return int(row["count"])


class AlertQueue:
    def __init__(
        self,
        handler: AlertHandler,
        maxsize: int = 1000,
        db_path: Path | None = None,
    ) -> None:
        self._handler = handler
        self._queue: asyncio.Queue[Alert | str] = asyncio.Queue(maxsize=maxsize)
        self._store = _DurableAlertStore(db_path) if db_path is not None else None
        self._worker: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._attempted: set[str] = set()

    async def submit(self, alert: Alert) -> None:
        if self._store is None:
            await self._queue.put(alert)
            return
        if self._store.enqueue(alert):
            await self._queue.put(alert.id)

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._stopping.clear()
            self._attempted.clear()
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
            queued_item = False
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                queued_item = True
            except asyncio.TimeoutError:
                item = self._next_recovered_id()
                if item is None:
                    continue

            alert_id: str | None = None
            if self._store is None:
                assert isinstance(item, Alert)
                alert = item
            else:
                assert isinstance(item, str)
                alert_id = item
                alert = self._store.get(alert_id)
                if alert is None:
                    if queued_item:
                        self._queue.task_done()
                    continue
            try:
                await self._handler(alert)
            except Exception:
                logger.exception("worker_handler_failed", extra={"alert_id": alert.id})
                if alert_id is not None:
                    self._attempted.add(alert_id)
            else:
                if alert_id is not None:
                    self._store.delete(alert_id)
            finally:
                if queued_item:
                    self._queue.task_done()

    def _next_recovered_id(self) -> str | None:
        if self._store is None:
            return None
        return next(
            (
                alert_id
                for alert_id in self._store.pending_ids()
                if alert_id not in self._attempted
            ),
            None,
        )

    def qsize(self) -> int:
        if self._store is not None:
            return self._store.count()
        return self._queue.qsize()
