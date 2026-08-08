import asyncio
import sqlite3
import time
from contextlib import closing

import pytest

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

    with closing(sqlite3.connect(db_path)) as conn:
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
    with closing(sqlite3.connect(db_path)) as conn:
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

    with closing(sqlite3.connect(db_path)) as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(alert_queue)").fetchall()
        }
    assert {
        "attempt_count",
        "next_attempt_at",
        "last_error",
        "lease_token",
        "lease_expires_at",
    } <= columns


async def test_two_workers_claim_one_alert_only_once(tmp_path):
    db_path = tmp_path / "incidents.db"
    entered = asyncio.Event()
    release = asyncio.Event()
    attempts: list[str] = []

    async def handler(alert: Alert) -> None:
        attempts.append(alert.id)
        entered.set()
        await release.wait()

    first = AlertQueue(handler=handler, db_path=db_path, lease_seconds=1)
    second = AlertQueue(handler=handler, db_path=db_path, lease_seconds=1)
    await first.submit(_alert("claimed-once"))
    first.start()
    await asyncio.wait_for(entered.wait(), timeout=1)
    second.start()

    await asyncio.sleep(0.2)
    assert attempts == ["claimed-once"]

    release.set()
    for _ in range(100):
        if first.qsize() == 0:
            break
        await asyncio.sleep(0.01)
    await first.stop()
    await second.stop()
    assert first.qsize() == 0


async def test_active_worker_renews_lease_during_long_handler(tmp_path):
    db_path = tmp_path / "incidents.db"
    entered = asyncio.Event()
    release = asyncio.Event()
    attempts: list[str] = []

    async def handler(alert: Alert) -> None:
        attempts.append(alert.id)
        entered.set()
        await release.wait()

    first = AlertQueue(handler=handler, db_path=db_path, lease_seconds=0.15)
    second = AlertQueue(handler=handler, db_path=db_path, lease_seconds=0.15)
    await first.submit(_alert("long-running"))
    first.start()
    await asyncio.wait_for(entered.wait(), timeout=1)
    second.start()

    await asyncio.sleep(0.4)
    release.set()
    for _ in range(100):
        if first.qsize() == 0:
            break
        await asyncio.sleep(0.01)
    await first.stop()
    await second.stop()

    assert attempts == ["long-running"]
    assert first.qsize() == 0


async def test_expired_lease_recovers_work_abandoned_by_crashed_worker(tmp_path):
    db_path = tmp_path / "incidents.db"
    abandoned = asyncio.Event()

    async def crashing_handler(alert: Alert) -> None:
        abandoned.set()
        raise asyncio.CancelledError

    first = AlertQueue(
        handler=crashing_handler,
        db_path=db_path,
        lease_seconds=0.25,
    )
    await first.submit(_alert("recover-expired-lease"))
    first.start()
    await asyncio.wait_for(abandoned.wait(), timeout=1)

    recovered: list[str] = []

    async def recovery_handler(alert: Alert) -> None:
        recovered.append(alert.id)

    second = AlertQueue(
        handler=recovery_handler,
        db_path=db_path,
        lease_seconds=1,
    )
    second.start()
    await asyncio.sleep(0.1)
    assert recovered == []

    for _ in range(100):
        if recovered:
            break
        await asyncio.sleep(0.01)
    await first.stop()
    await second.stop()

    assert recovered == ["recover-expired-lease"]
    assert second.qsize() == 0


@pytest.mark.parametrize("stale_worker_fails", [False, True])
def test_stale_worker_cannot_mutate_alert_reassigned_after_lease_expiry(
    tmp_path, stale_worker_fails
):
    db_path = tmp_path / "incidents.db"
    queue = AlertQueue(handler=lambda alert: None, db_path=db_path)
    store = queue._store
    assert store is not None
    assert store.enqueue(_alert("reassigned"))
    first_claim = store.claim("reassigned", now=time.time(), lease_seconds=0.05)
    assert first_claim is not None
    second_claim = store.claim(
        "reassigned",
        now=time.time() + 1,
        lease_seconds=1,
    )
    assert second_claim is not None

    if stale_worker_fails:
        store.record_failure(
            "reassigned",
            lease_token=first_claim.lease_token,
            error="stale failure",
            base_seconds=0,
            max_seconds=0,
            max_attempts=5,
        )
    else:
        store.delete("reassigned", lease_token=first_claim.lease_token)

    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            """
            SELECT attempt_count, last_error, lease_token, lease_expires_at
            FROM alert_queue
            WHERE alert_id = ?
            """,
            ("reassigned",),
        ).fetchone()
    assert row is not None
    assert row[0:2] == (0, None)
    assert row[2] == second_claim.lease_token
    assert row[3] > 0

    assert store.delete("reassigned", lease_token=second_claim.lease_token)


async def test_durable_queue_dead_letters_after_maximum_attempts(tmp_path):
    db_path = tmp_path / "incidents.db"
    attempts: list[str] = []

    async def permanently_failing_handler(alert: Alert) -> None:
        attempts.append(alert.id)
        raise RuntimeError("invalid alert payload")

    queue = AlertQueue(
        handler=permanently_failing_handler,
        db_path=db_path,
        retry_base_seconds=0,
        retry_max_seconds=0,
        max_attempts=3,
    )
    queue.start()
    await queue.submit(_alert("poison-alert"))
    for _ in range(100):
        if queue.qsize() == 0:
            break
        await asyncio.sleep(0.01)
    await queue.stop()

    assert attempts == ["poison-alert"] * 3
    assert queue.qsize() == 0
    with closing(sqlite3.connect(db_path)) as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM alert_queue WHERE alert_id = ?",
            ("poison-alert",),
        ).fetchone()
        dead_letter = conn.execute(
            """
            SELECT payload, attempt_count, last_error, failed_at
            FROM alert_dead_letters
            WHERE alert_id = ?
            """,
            ("poison-alert",),
        ).fetchone()

    assert pending == (0,)
    assert dead_letter is not None
    assert Alert.model_validate_json(dead_letter[0]).id == "poison-alert"
    assert dead_letter[1] == 3
    assert dead_letter[2] == "invalid alert payload"
    assert dead_letter[3]


async def test_replay_dead_letter_resets_retry_state_and_wakes_worker(tmp_path):
    db_path = tmp_path / "incidents.db"
    seen: list[str] = []
    prepared: list[str] = []

    async def handler(alert: Alert) -> None:
        assert prepared == [alert.id]
        seen.append(alert.id)

    queue = AlertQueue(handler=handler, db_path=db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO alert_dead_letters
                (alert_id, payload, attempt_count, last_error, failed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "replay-me",
                _alert("replay-me").model_dump_json(),
                5,
                "permanent failure",
                "2026-08-07 12:00:00",
            ),
        )
        conn.commit()

    def prepare(alert: Alert) -> None:
        with closing(sqlite3.connect(db_path)) as conn:
            active = conn.execute(
                """
                SELECT attempt_count, next_attempt_at, last_error
                FROM alert_queue
                WHERE alert_id = ?
                """,
                (alert.id,),
            ).fetchone()
            dead = conn.execute(
                "SELECT COUNT(*) FROM alert_dead_letters WHERE alert_id = ?",
                (alert.id,),
            ).fetchone()
        assert active == (0, 0, None)
        assert dead == (0,)
        prepared.append(alert.id)

    replayed = await queue.replay_dead_letter("replay-me", before_wake=prepare)
    queue.start()
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(0.01)
    await queue.stop()

    assert replayed.id == "replay-me"
    assert seen == ["replay-me"]
    assert queue.qsize() == 0


def test_dead_letters_survive_queue_restart_and_stay_out_of_depth(tmp_path):
    db_path = tmp_path / "incidents.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE alert_queue (
                alert_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_error TEXT
            );
            CREATE TABLE alert_dead_letters (
                alert_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                attempt_count INTEGER NOT NULL,
                last_error TEXT NOT NULL,
                failed_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        conn.execute(
            """
            INSERT INTO alert_dead_letters
                (alert_id, payload, attempt_count, last_error)
            VALUES (?, ?, ?, ?)
            """,
            ("already-dead", _alert("already-dead").model_dump_json(), 5, "boom"),
        )
        conn.commit()

    restarted = AlertQueue(handler=lambda alert: None, db_path=db_path)

    assert restarted.qsize() == 0
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT attempt_count, last_error FROM alert_dead_letters"
        ).fetchone()
    assert row == (5, "boom")


def test_max_attempts_must_be_positive():
    with pytest.raises(ValueError, match="max_attempts must be positive"):
        AlertQueue(handler=lambda alert: None, max_attempts=0)


def test_lease_seconds_must_be_positive():
    with pytest.raises(ValueError, match="lease_seconds must be positive"):
        AlertQueue(handler=lambda alert: None, lease_seconds=0)
