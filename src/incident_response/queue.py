"""Async alert worker with optional SQLite durability.

The FastAPI app supplies its existing SQLite path, so submit persists an alert
before returning and a restarted worker can recover unfinished work. Direct unit
users may omit ``db_path`` to retain a purely in-memory queue.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Iterator, Protocol
from uuid import uuid4

from pydantic import BaseModel

from .models import Alert

logger = logging.getLogger(__name__)


AlertHandler = Callable[[Alert], Awaitable[None]]


class DeadLetterNotFoundError(LookupError):
    pass


class AlertAlreadyQueuedError(RuntimeError):
    pass


class DeadLetter(BaseModel):
    alert: Alert
    attempt_count: int
    last_error: str
    failed_at: datetime


@dataclass(frozen=True)
class _ClaimedAlert:
    alert: Alert
    lease_token: str


class DurableAlertStore(Protocol):
    def enqueue(self, alert: Alert) -> bool: ...

    def claim(
        self, alert_id: str, *, now: float, lease_seconds: float
    ) -> _ClaimedAlert | None: ...

    def claim_next(self, *, now: float, lease_seconds: float) -> _ClaimedAlert | None: ...

    def delete(self, alert_id: str, *, lease_token: str) -> bool: ...

    def renew_lease(
        self,
        alert_id: str,
        *,
        lease_token: str,
        now: float,
        lease_seconds: float,
    ) -> bool: ...

    def record_failure(
        self,
        alert_id: str,
        *,
        lease_token: str,
        error: str,
        base_seconds: float,
        max_seconds: float,
        max_attempts: int,
    ) -> bool: ...

    def count(self) -> int: ...

    def list_dead_letters(self, limit: int) -> list[DeadLetter]: ...

    def replay_dead_letter(self, alert_id: str) -> Alert: ...

_QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_queue (
    alert_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    lease_token TEXT,
    lease_expires_at REAL
);
CREATE INDEX IF NOT EXISTS alert_queue_created_idx
ON alert_queue(created_at, alert_id);
CREATE TABLE IF NOT EXISTS alert_dead_letters (
    alert_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    attempt_count INTEGER NOT NULL,
    last_error TEXT NOT NULL,
    failed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _retry_delay(
    attempt_number: int,
    *,
    base_seconds: float,
    max_seconds: float,
) -> float:
    return min(max_seconds, base_seconds * (2 ** max(attempt_number - 1, 0)))


class _DurableAlertStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_QUEUE_SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(alert_queue)").fetchall()
        }
        migrations = {
            "attempt_count": (
                "ALTER TABLE alert_queue "
                "ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"
            ),
            "next_attempt_at": (
                "ALTER TABLE alert_queue "
                "ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0"
            ),
            "last_error": "ALTER TABLE alert_queue ADD COLUMN last_error TEXT",
            "lease_token": "ALTER TABLE alert_queue ADD COLUMN lease_token TEXT",
            "lease_expires_at": (
                "ALTER TABLE alert_queue ADD COLUMN lease_expires_at REAL"
            ),
        }
        for column, statement in migrations.items():
            if column not in columns:
                conn.execute(statement)

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

    def claim(
        self,
        alert_id: str,
        *,
        now: float,
        lease_seconds: float,
    ) -> _ClaimedAlert | None:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT payload
                FROM alert_queue
                WHERE alert_id = ?
                  AND next_attempt_at <= ?
                  AND (lease_token IS NULL OR lease_expires_at <= ?)
                """,
                (alert_id, now, now),
            ).fetchone()
            if row is None:
                return None
            lease_token = uuid4().hex
            conn.execute(
                """
                UPDATE alert_queue
                SET lease_token = ?, lease_expires_at = ?
                WHERE alert_id = ?
                """,
                (lease_token, now + lease_seconds, alert_id),
            )
        return _ClaimedAlert(
            alert=Alert.model_validate(json.loads(row["payload"])),
            lease_token=lease_token,
        )

    def claim_next(self, *, now: float, lease_seconds: float) -> _ClaimedAlert | None:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT alert_id, payload
                FROM alert_queue
                WHERE next_attempt_at <= ?
                  AND (lease_token IS NULL OR lease_expires_at <= ?)
                ORDER BY created_at ASC, alert_id ASC
                LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if row is None:
                return None
            lease_token = uuid4().hex
            conn.execute(
                """
                UPDATE alert_queue
                SET lease_token = ?, lease_expires_at = ?
                WHERE alert_id = ?
                """,
                (lease_token, now + lease_seconds, row["alert_id"]),
            )
        return _ClaimedAlert(
            alert=Alert.model_validate(json.loads(row["payload"])),
            lease_token=lease_token,
        )

    def delete(self, alert_id: str, *, lease_token: str) -> bool:
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM alert_queue WHERE alert_id = ? AND lease_token = ?",
                (alert_id, lease_token),
            )
            return cursor.rowcount == 1

    def renew_lease(
        self,
        alert_id: str,
        *,
        lease_token: str,
        now: float,
        lease_seconds: float,
    ) -> bool:
        with self._conn() as conn:
            cursor = conn.execute(
                """
                UPDATE alert_queue
                SET lease_expires_at = ?
                WHERE alert_id = ? AND lease_token = ?
                """,
                (now + lease_seconds, alert_id, lease_token),
            )
            return cursor.rowcount == 1

    def record_failure(
        self,
        alert_id: str,
        *,
        lease_token: str,
        error: str,
        base_seconds: float,
        max_seconds: float,
        max_attempts: int,
    ) -> bool:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT payload, attempt_count
                FROM alert_queue
                WHERE alert_id = ? AND lease_token = ?
                """,
                (alert_id, lease_token),
            ).fetchone()
            if row is None:
                return False
            attempt_count = int(row["attempt_count"]) + 1
            truncated_error = error[:500]
            if attempt_count >= max_attempts:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO alert_dead_letters
                        (alert_id, payload, attempt_count, last_error, failed_at)
                    VALUES (?, ?, ?, ?, datetime('now'))
                    """,
                    (alert_id, row["payload"], attempt_count, truncated_error),
                )
                conn.execute(
                    """
                    DELETE FROM alert_queue
                    WHERE alert_id = ? AND lease_token = ?
                    """,
                    (alert_id, lease_token),
                )
                return True
            delay = _retry_delay(
                attempt_count,
                base_seconds=base_seconds,
                max_seconds=max_seconds,
            )
            conn.execute(
                """
                UPDATE alert_queue
                SET attempt_count = ?,
                    next_attempt_at = ?,
                    last_error = ?,
                    lease_token = NULL,
                    lease_expires_at = NULL
                WHERE alert_id = ? AND lease_token = ?
                """,
                (
                    attempt_count,
                    time.time() + delay,
                    truncated_error,
                    alert_id,
                    lease_token,
                ),
            )
            return False

    def count(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM alert_queue").fetchone()
        return int(row["count"])

    def list_dead_letters(self, limit: int) -> list[DeadLetter]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT payload, attempt_count, last_error, failed_at
                FROM alert_dead_letters
                ORDER BY failed_at DESC, alert_id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            DeadLetter(
                alert=Alert.model_validate_json(row["payload"]),
                attempt_count=int(row["attempt_count"]),
                last_error=str(row["last_error"]),
                failed_at=row["failed_at"],
            )
            for row in rows
        ]

    def replay_dead_letter(self, alert_id: str) -> Alert:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload FROM alert_dead_letters WHERE alert_id = ?",
                (alert_id,),
            ).fetchone()
            if row is None:
                raise DeadLetterNotFoundError(alert_id)
            active = conn.execute(
                "SELECT 1 FROM alert_queue WHERE alert_id = ?",
                (alert_id,),
            ).fetchone()
            if active is not None:
                raise AlertAlreadyQueuedError(alert_id)
            conn.execute(
                """
                INSERT INTO alert_queue
                    (alert_id, payload, attempt_count, next_attempt_at, last_error)
                VALUES (?, ?, 0, 0, NULL)
                """,
                (alert_id, row["payload"]),
            )
            conn.execute(
                "DELETE FROM alert_dead_letters WHERE alert_id = ?",
                (alert_id,),
            )
        return Alert.model_validate_json(row["payload"])


class AlertQueue:
    def __init__(
        self,
        handler: AlertHandler,
        maxsize: int = 1000,
        db_path: Path | None = None,
        store: DurableAlertStore | None = None,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 60.0,
        max_attempts: int = 5,
        lease_seconds: float = 300.0,
    ) -> None:
        if retry_base_seconds < 0:
            raise ValueError("retry_base_seconds must be non-negative")
        if retry_max_seconds < retry_base_seconds:
            raise ValueError("retry_max_seconds must be at least retry_base_seconds")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if db_path is not None and store is not None:
            raise ValueError("db_path and store are mutually exclusive")
        self._handler = handler
        self._queue: asyncio.Queue[Alert | str] = asyncio.Queue(maxsize=maxsize)
        self._store = store or (_DurableAlertStore(db_path) if db_path is not None else None)
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._max_attempts = max_attempts
        self._lease_seconds = lease_seconds
        self._worker: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    _retry_delay = staticmethod(_retry_delay)

    async def submit(self, alert: Alert) -> None:
        if self._store is None:
            await self._queue.put(alert)
            return
        if self._store.enqueue(alert):
            await self._queue.put(alert.id)

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
            queued_item = False
            claimed: _ClaimedAlert | None = None
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                queued_item = True
            except asyncio.TimeoutError:
                if self._store is None:
                    continue
                claimed = self._store.claim_next(
                    now=time.time(),
                    lease_seconds=self._lease_seconds,
                )
                if claimed is None:
                    continue

            alert_id: str | None = None
            lease_token: str | None = None
            if self._store is None:
                assert isinstance(item, Alert)
                alert = item
            else:
                if claimed is None:
                    assert isinstance(item, str)
                    claimed = self._store.claim(
                        item,
                        now=time.time(),
                        lease_seconds=self._lease_seconds,
                    )
                if claimed is None:
                    if queued_item:
                        self._queue.task_done()
                    continue
                alert = claimed.alert
                alert_id = alert.id
                lease_token = claimed.lease_token
            heartbeat: asyncio.Task[None] | None = None
            if alert_id is not None and lease_token is not None:
                heartbeat = asyncio.create_task(
                    self._renew_lease(alert_id, lease_token),
                    name=f"lease-heartbeat-{alert_id}",
                )
            try:
                try:
                    await self._handler(alert)
                finally:
                    if heartbeat is not None:
                        heartbeat.cancel()
                        try:
                            await heartbeat
                        except asyncio.CancelledError:
                            pass
            except Exception as exc:
                logger.exception("worker_handler_failed", extra={"alert_id": alert.id})
                if alert_id is not None and lease_token is not None:
                    dead_lettered = self._store.record_failure(
                        alert_id,
                        lease_token=lease_token,
                        error=str(exc),
                        base_seconds=self._retry_base_seconds,
                        max_seconds=self._retry_max_seconds,
                        max_attempts=self._max_attempts,
                    )
                    if dead_lettered:
                        logger.error(
                            "alert_dead_lettered",
                            extra={
                                "alert_id": alert.id,
                                "max_attempts": self._max_attempts,
                            },
                        )
            else:
                if alert_id is not None and lease_token is not None:
                    self._store.delete(alert_id, lease_token=lease_token)
            finally:
                if queued_item:
                    self._queue.task_done()

    async def _renew_lease(self, alert_id: str, lease_token: str) -> None:
        assert self._store is not None
        interval = self._lease_seconds / 3
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = self._store.renew_lease(
                    alert_id,
                    lease_token=lease_token,
                    now=time.time(),
                    lease_seconds=self._lease_seconds,
                )
            except Exception:
                logger.exception(
                    "alert_lease_renewal_failed",
                    extra={"alert_id": alert_id},
                )
                return
            if not renewed:
                logger.warning(
                    "alert_lease_ownership_lost",
                    extra={"alert_id": alert_id},
                )
                return

    def qsize(self) -> int:
        if self._store is not None:
            return self._store.count()
        return self._queue.qsize()

    def list_dead_letters(self, limit: int = 50) -> list[DeadLetter]:
        if self._store is None:
            return []
        return self._store.list_dead_letters(limit)

    async def replay_dead_letter(
        self,
        alert_id: str,
        *,
        before_wake: Callable[[Alert], object] | None = None,
    ) -> Alert:
        if self._store is None:
            raise DeadLetterNotFoundError(alert_id)
        alert = self._store.replay_dead_letter(alert_id)
        if before_wake is not None:
            prepared = before_wake(alert)
            if inspect.isawaitable(prepared):
                await prepared
        await self._queue.put(alert.id)
        return alert
