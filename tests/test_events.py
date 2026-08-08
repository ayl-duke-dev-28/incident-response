import asyncio
from datetime import datetime, timezone

from incident_response.events import IncidentEventBroker, incident_version
from incident_response.models import Alert, Incident


def _incident(title: str) -> Incident:
    alert = Alert(
        id="event-1",
        title=title,
        service="checkout",
        triggered_at=datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc),
    )
    return Incident(id="inc-event-1", alert=alert, created_at=alert.triggered_at)


async def test_local_broker_pushes_changes_and_versions_change_with_incident():
    broker = IncidentEventBroker(heartbeat_seconds=1)
    events = broker.events("inc-event-1")
    waiting = asyncio.create_task(anext(events))
    await asyncio.sleep(0)

    await broker.publish("inc-event-1")

    assert await waiting is True
    assert incident_version(_incident("one")) != incident_version(_incident("two"))
    await events.aclose()
