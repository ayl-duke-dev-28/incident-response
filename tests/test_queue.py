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
