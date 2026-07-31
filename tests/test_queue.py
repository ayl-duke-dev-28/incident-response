import asyncio

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


async def test_durable_queue_removes_alert_only_after_success(tmp_path):
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

    recovered: list[str] = []

    async def successful_handler(alert: Alert) -> None:
        recovered.append(alert.id)

    restarted = AlertQueue(handler=successful_handler, db_path=db_path)
    restarted.start()
    for _ in range(100):
        if recovered:
            break
        await asyncio.sleep(0.01)
    await restarted.stop()

    assert recovered == ["retry-after-restart"]
    assert restarted.qsize() == 0


async def test_durable_queue_coalesces_duplicate_pending_alert_ids(tmp_path):
    db_path = tmp_path / "incidents.db"
    queue = AlertQueue(handler=lambda alert: None, db_path=db_path)

    await queue.submit(_alert("duplicate"))
    await queue.submit(_alert("duplicate"))

    assert queue.qsize() == 1
