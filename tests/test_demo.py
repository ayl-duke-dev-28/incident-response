from datetime import datetime, timezone


def test_build_demo_alert_returns_the_deterministic_cli_scenario():
    from incident_response.demo import build_demo_alert

    alert = build_demo_alert()

    assert alert.model_dump(mode="json") == {
        "id": "demo-checkout-001",
        "source": "generic",
        "provider_event_id": "",
        "provider_incident_key": None,
        "correlation_key": "",
        "title": "Checkout 5xx > 5%",
        "description": "checkout service error rate at 18%",
        "service": "checkout",
        "severity": "sev2",
        "triggered_at": "2026-07-02T21:05:00Z",
        "metric": "http.error_rate",
        "environment": "",
        "threshold": 0.05,
        "value": 0.184,
        "tags": {"env": "demo"},
        "raw": {},
    }


def test_build_demo_alert_accepts_collision_safe_console_overrides():
    from incident_response.demo import build_demo_alert

    triggered_at = datetime(2026, 7, 24, 12, 30, tzinfo=timezone.utc)
    alert = build_demo_alert(
        alert_id="demo-checkout-unique",
        triggered_at=triggered_at,
        metric="http.error_rate.demo-unique",
        source="console",
    )

    assert alert.id == "demo-checkout-unique"
    assert alert.triggered_at == triggered_at
    assert alert.metric == "http.error_rate.demo-unique"
    assert alert.tags == {"env": "demo", "source": "console"}
    assert alert.title == "Checkout 5xx > 5%"
    assert alert.service == "checkout"
    assert alert.value == 0.184
