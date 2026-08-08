from datetime import datetime, timedelta, timezone

from incident_response.db import IncidentStore
from incident_response.models import Alert, Incident, IncidentStatus


def _alert(event_id: str, service: str, correlation_key: str, at: datetime) -> Alert:
    return Alert(
        id=f"generic:{event_id}",
        source="generic",
        provider_event_id=event_id,
        correlation_key=correlation_key,
        title=f"Alert {event_id}",
        service=service,
        triggered_at=at,
    )


def _candidate(alert: Alert, at: datetime) -> Incident:
    return Incident(
        id=f"inc-{alert.id}",
        alert=alert,
        status=IncidentStatus.INVESTIGATING,
        created_at=at,
        timeline=[{"timestamp": at.isoformat(), "event": f"Alert fired: {alert.title}"}],
    )


def test_cross_service_alerts_merge_atomically_and_retries_are_idempotent(tmp_path):
    store = IncidentStore(tmp_path / "incidents.db")
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    first = _alert("event-1", "checkout", "customer-flow-prod", now)
    second = _alert("event-2", "payments", "customer-flow-prod", now + timedelta(minutes=2))

    opened = store.correlate_alert(first, _candidate(first, now), merge_window_minutes=15)
    merged = store.correlate_alert(
        second,
        _candidate(second, now + timedelta(minutes=2)),
        merge_window_minutes=15,
    )
    retried = store.correlate_alert(
        second,
        _candidate(second, now + timedelta(minutes=2)),
        merge_window_minutes=15,
    )

    assert opened.created is True
    assert merged.created is False
    assert merged.duplicate is False
    assert merged.incident.id == opened.incident.id
    assert merged.incident.related_alerts == [second]
    assert retried.duplicate is True
    assert retried.incident.related_alerts == [second]


def test_resolved_or_expired_incident_is_not_reopened_by_correlation(tmp_path):
    store = IncidentStore(tmp_path / "incidents.db")
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    first = _alert("event-1", "checkout", "same-key", now)
    opened = store.correlate_alert(first, _candidate(first, now), merge_window_minutes=15)
    store.save(opened.incident.model_copy(update={"status": IncidentStatus.RESOLVED}))
    later = _alert("event-2", "checkout", "same-key", now + timedelta(minutes=1))

    result = store.correlate_alert(
        later,
        _candidate(later, now + timedelta(minutes=1)),
        merge_window_minutes=15,
    )

    assert result.created is True
    assert result.incident.id != opened.incident.id
