import asyncio
import sqlite3

from incident_response.models import Alert, Severity
from incident_response.queue import AlertQueue


def _alert(id: str) -> Alert:
    from datetime import datetime, timezone
    return Alert(
        id=id,
        title="x",
        service="checkout",
        severity=Severity.SEV3,
        triggered_at=datetime.now(timezone.utc),
    )


async def test_worker_processes_submitted_alerts():
    seen: list[str] = []

    async def handler(alert: Alert) -> None:
        seen.append(alert.id)

    q = AlertQueue(handler=handler)
    q.start()
    await q.submit(_alert("a"))
    await q.submit(_alert("b"))
    # Wait for both to drain
    for _ in range(50):
        if len(seen) == 2:
            break
        await asyncio.sleep(0.02)
    await q.stop()
    assert seen == ["a", "b"]


async def test_worker_survives_handler_exception():
    seen: list[str] = []

    async def handler(alert: Alert) -> None:
        if alert.id == "bad":
            raise RuntimeError("boom")
        seen.append(alert.id)

    q = AlertQueue(handler=handler)
    q.start()
    await q.submit(_alert("bad"))
    await q.submit(_alert("good"))
    for _ in range(50):
        if seen == ["good"]:
            break
        await asyncio.sleep(0.02)
    await q.stop()
    assert seen == ["good"]


async def test_durable_queue_recovers_alert_submitted_before_worker_start(tmp_path):
    db_path = tmp_path / "incidents.db"
    first = AlertQueue(handler=lambda alert: None, db_path=db_path)

    await first.submit(_alert("persisted-before-start"))

    assert first.qsize() == 1
    seen: list[str] = []

    async def handler(alert: Alert) -> None:
        seen.append(alert.id)

    restarted = AlertQueue(handler=handler, db_path=db_path)
    restarted.start()
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(0.01)
    await restarted.stop()

    assert seen == ["persisted-before-start"]
    assert restarted.qsize() == 0


async def test_durable_queue_retains_alert_after_failure(tmp_path):
    db_path = tmp_path / "incidents.db"
    attempts: list[str] = []

    async def failing_handler(alert: Alert) -> None:
        attempts.append(alert.id)
        raise RuntimeError("temporary failure")

    first = AlertQueue(handler=failing_handler, db_path=db_path)
    first.start()
    await first.submit(_alert("retry-after-restart"))
    for _ in range(100):
        if attempts:
            break
        await asyncio.sleep(0.01)
    await first.stop()

    assert attempts == ["retry-after-restart"]
    assert first.qsize() == 1


def test_retry_delay_is_exponential_and_capped():
    assert AlertQueue._retry_delay(1, base_seconds=1, max_seconds=5) == 1
    assert AlertQueue._retry_delay(2, base_seconds=1, max_seconds=5) == 2
    assert AlertQueue._retry_delay(3, base_seconds=1, max_seconds=5) == 4
    assert AlertQueue._retry_delay(4, base_seconds=1, max_seconds=5) == 5


async def test_durable_queue_retries_automatically_after_persisted_backoff(tmp_path):
    db_path = tmp_path / "incidents.db"
    attempts: list[float] = []

    async def flaky_handler(alert: Alert) -> None:
        attempts.append(asyncio.get_running_loop().time())
        if len(attempts) < 3:
            raise RuntimeError(f"temporary failure {len(attempts)}")

    queue = AlertQueue(
        handler=flaky_handler,
        db_path=db_path,
        retry_base_seconds=0.02,
        retry_max_seconds=0.04,
    )
    queue.start()
    await queue.submit(_alert("automatic-retry"))
    for _ in range(100):
        if len(attempts) == 3:
            break
        await asyncio.sleep(0.01)
    await queue.stop()

    assert len(attempts) == 3
    assert attempts[1] - attempts[0] >= 0.015
    assert attempts[2] - attempts[1] >= 0.03
    assert queue.qsize() == 0


async def test_retry_schedule_and_attempt_count_survive_restart(tmp_path):
    db_path = tmp_path / "incidents.db"
    first_attempted = asyncio.Event()

    async def failing_handler(alert: Alert) -> None:
        first_attempted.set()
        raise RuntimeError("database temporarily unavailable")

    first = AlertQueue(
        handler=failing_handler,
        db_path=db_path,
        retry_base_seconds=0.2,
        retry_max_seconds=0.2,
    )
    first.start()
    await first.submit(_alert("persisted-retry"))
    await asyncio.wait_for(first_attempted.wait(), timeout=1)
    await first.stop()

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT attempt_count, next_attempt_at, last_error
            FROM alert_queue
            WHERE alert_id = ?
            """,
            ("persisted-retry",),
        ).fetchone()
    assert row is not None
    assert row[0] == 1
    assert row[1] > 0
    assert row[2] == "database temporarily unavailable"

    recovered: list[str] = []

    async def successful_handler(alert: Alert) -> None:
        recovered.append(alert.id)

    restarted = AlertQueue(
        handler=successful_handler,
        db_path=db_path,
        retry_base_seconds=0.2,
        retry_max_seconds=0.2,
    )
    restarted.start()
    await asyncio.sleep(0.05)
    assert recovered == []
    for _ in range(50):
        if recovered:
            break
        await asyncio.sleep(0.01)
    await restarted.stop()

    assert recovered == ["persisted-retry"]
    assert restarted.qsize() == 0


async def test_durable_queue_coalesces_duplicate_pending_alert_ids(tmp_path):
    db_path = tmp_path / "incidents.db"
    queue = AlertQueue(handler=lambda alert: None, db_path=db_path)

    await queue.submit(_alert("duplicate"))
    await queue.submit(_alert("duplicate"))

    assert queue.qsize() == 1


def test_durable_queue_migrates_slice_one_schema(tmp_path):
    db_path = tmp_path / "incidents.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE alert_queue (
                alert_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )

    AlertQueue(handler=lambda alert: None, db_path=db_path)

    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(alert_queue)").fetchall()
        }
    assert {"attempt_count", "next_attempt_at", "last_error"} <= columns
