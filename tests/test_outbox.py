from datetime import datetime, timezone

from incident_response.db import IncidentStore
from incident_response.models import Alert, ExternalReference, Incident
from incident_response.outbox import OutboxDispatcher


def _incident() -> Incident:
    alert = Alert(
        id="event-1",
        title="Checkout failures",
        service="checkout",
        triggered_at=datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc),
    )
    return Incident(id="inc-event-1", alert=alert, created_at=alert.triggered_at)


def test_incident_creation_atomically_enqueues_one_message_per_ticket_provider(tmp_path):
    store = IncidentStore(tmp_path / "incidents.db", outbox_destinations=("jira", "linear"))
    incident = _incident()

    store.correlate_alert(incident.alert, incident, merge_window_minutes=15)
    messages = store.list_outbox()

    assert [message.destination for message in messages] == ["jira", "linear"]
    assert {message.idempotency_key for message in messages} == {
        "incident:inc-event-1:ticket:jira",
        "incident:inc-event-1:ticket:linear",
    }


def test_outbox_claim_completion_persists_external_reference_exactly_once(tmp_path):
    store = IncidentStore(tmp_path / "incidents.db", outbox_destinations=("jira",))
    incident = _incident()
    store.correlate_alert(incident.alert, incident, merge_window_minutes=15)

    claimed = store.claim_outbox(now=100, lease_seconds=30)
    assert claimed is not None
    assert store.claim_outbox(now=100, lease_seconds=30) is None
    reference = ExternalReference(
        provider="jira",
        external_id="OPS-42",
        url="https://acme.atlassian.net/browse/OPS-42",
    )

    assert store.complete_outbox(claimed, reference) is True
    assert store.complete_outbox(claimed, reference) is False
    saved = store.get(incident.id)
    assert saved is not None
    assert saved.external_references == [reference]
    assert store.list_outbox()[0].status == "delivered"


async def test_dispatcher_delivers_stable_key_and_bounds_retries(tmp_path):
    store = IncidentStore(tmp_path / "incidents.db", outbox_destinations=("jira",))
    incident = _incident()
    store.correlate_alert(incident.alert, incident, merge_window_minutes=15)

    class FailingTickets:
        def __init__(self):
            self.keys = []

        async def create(self, incident, *, idempotency_key):
            self.keys.append(idempotency_key)
            raise RuntimeError("provider unavailable")

    tickets = FailingTickets()
    dispatcher = OutboxDispatcher(
        store=store,
        ticket_clients={"jira": tickets},
        max_attempts=2,
        retry_base_seconds=1,
        retry_max_seconds=10,
        lease_seconds=30,
    )

    assert await dispatcher.dispatch_once(now=100) is True
    assert await dispatcher.dispatch_once(now=100) is False
    assert await dispatcher.dispatch_once(now=101) is True

    message = store.list_outbox()[0]
    assert tickets.keys == [
        "incident:inc-event-1:ticket:jira",
        "incident:inc-event-1:ticket:jira",
    ]
    assert message.attempt_count == 2
    assert message.status == "dead"
