from incident_response.models import Severity
from incident_response.normalization import (
    normalize_datadog_alert,
    normalize_generic_alert,
    normalize_pagerduty_alert,
)


def test_datadog_payload_normalizes_provider_identity_and_correlation():
    alert = normalize_datadog_alert(
        {
            "id": "evt-123",
            "title": "Checkout error rate",
            "body": "Errors exceeded 5%",
            "date": 1_722_528_000,
            "service": "checkout-api",
            "metric": "http.errors",
            "priority": "P1",
            "aggregation_key": "monitor-77:prod",
            "tags": "env:prod,team:payments",
        }
    )

    assert alert.source == "datadog"
    assert alert.provider_event_id == "evt-123"
    assert alert.provider_incident_key == "monitor-77:prod"
    assert alert.correlation_key == "monitor-77:prod"
    assert alert.service == "checkout-api"
    assert alert.environment == "prod"
    assert alert.severity is Severity.SEV1


def test_pagerduty_v3_payload_normalizes_nested_incident():
    alert = normalize_pagerduty_alert(
        {
            "event": {
                "id": "webhook-event-1",
                "event_type": "incident.triggered",
                "occurred_at": "2026-08-08T12:30:00Z",
                "data": {
                    "id": "PINCIDENT1",
                    "title": "Database unavailable",
                    "description": "Primary is not accepting connections",
                    "urgency": "high",
                    "service": {"id": "PSERVICE1", "summary": "database"},
                    "priority": {"summary": "P2"},
                },
            }
        }
    )

    assert alert.source == "pagerduty"
    assert alert.provider_event_id == "webhook-event-1"
    assert alert.provider_incident_key == "PINCIDENT1"
    assert alert.correlation_key == "PINCIDENT1"
    assert alert.service == "database"
    assert alert.severity is Severity.SEV2


def test_generic_payload_uses_explicit_key_then_service_metric_environment():
    explicit = normalize_generic_alert(
        {
            "id": "generic-1",
            "title": "Latency",
            "service": "checkout",
            "triggered_at": "2026-08-08T12:30:00Z",
            "correlation_key": "customer-checkout-prod",
        }
    )
    derived = normalize_generic_alert(
        {
            "id": "generic-2",
            "title": "Latency",
            "service": "checkout",
            "metric": "latency.p99",
            "environment": "prod",
            "triggered_at": "2026-08-08T12:30:00Z",
        }
    )

    assert explicit.correlation_key == "customer-checkout-prod"
    assert derived.correlation_key == "checkout|latency.p99|prod"
