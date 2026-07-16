from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from incident_response.agents.llm import FakeLLM
from incident_response.config import Settings
from incident_response.db import IncidentStore
from incident_response.main import create_app
from incident_response.models import Alert, Incident, IncidentStatus


def _settings(tmp_path: Path, runbooks_dir: Path) -> Settings:
    return Settings(
        anthropic_api_key="test",
        github_mode="mock",
        slack_mode="mock",
        metrics_mode="mock",
        runbooks_dir=runbooks_dir,
        db_path=tmp_path / "incidents.db",
        webhook_token="secret",
    )


def _incident(
    *,
    incident_id: str,
    alert: Alert,
    status: IncidentStatus,
    created_at: datetime,
) -> Incident:
    return Incident(
        id=incident_id,
        alert=alert.model_copy(update={"id": incident_id.removeprefix("inc-")}),
        status=status,
        created_at=created_at,
    )


def test_console_empty_state_offers_demo_action(tmp_path, runbooks_dir):
    app = create_app(settings=_settings(tmp_path, runbooks_dir), llm=FakeLLM([]))

    with TestClient(app) as client:
        response = client.get("/console")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "No active incidents" in response.text
    assert "Trigger demo incident" in response.text


def test_console_renders_stored_incident_with_link_to_detail(
    tmp_path, runbooks_dir, alert
):
    settings = _settings(tmp_path, runbooks_dir)
    store = IncidentStore(settings.db_path)
    store.save(
        _incident(
            incident_id="inc-ddg-9273",
            alert=alert,
            status=IncidentStatus.INVESTIGATING,
            created_at=datetime(2026, 7, 2, 21, 5, tzinfo=timezone.utc),
        )
    )
    app = create_app(settings=settings, llm=FakeLLM([]))

    with TestClient(app) as client:
        response = client.get("/console")

    assert response.status_code == 200
    assert "Checkout 5xx &gt; 5%" in response.text
    assert "checkout" in response.text
    assert "sev2" in response.text
    assert "investigating" in response.text
    assert "/console/incidents/inc-ddg-9273" in response.text
    assert "No active incidents" not in response.text


def test_console_separates_open_from_resolved(tmp_path, runbooks_dir, alert):
    settings = _settings(tmp_path, runbooks_dir)
    store = IncidentStore(settings.db_path)
    store.save(
        _incident(
            incident_id="inc-open-one",
            alert=alert.model_copy(update={"title": "Open incident"}),
            status=IncidentStatus.INVESTIGATING,
            created_at=datetime(2026, 7, 2, 21, 6, tzinfo=timezone.utc),
        )
    )
    store.save(
        _incident(
            incident_id="inc-resolved-one",
            alert=alert.model_copy(update={"title": "Resolved incident"}),
            status=IncidentStatus.RESOLVED,
            created_at=datetime(2026, 7, 2, 21, 5, tzinfo=timezone.utc),
        )
    )
    app = create_app(settings=settings, llm=FakeLLM([]))

    with TestClient(app) as client:
        response = client.get("/console")

    assert response.status_code == 200
    body = response.text
    assert "Open incidents" in body
    assert "Recently resolved" in body
    # Open incidents render before resolved ones regardless of created_at order.
    assert body.index("Open incident") < body.index("Resolved incident")


def test_console_escapes_incident_text(tmp_path, runbooks_dir, alert):
    settings = _settings(tmp_path, runbooks_dir)
    store = IncidentStore(settings.db_path)
    store.save(
        _incident(
            incident_id="inc-xss",
            alert=alert.model_copy(
                update={"title": "<script>alert('xss')</script>"}
            ),
            status=IncidentStatus.OPEN,
            created_at=datetime(2026, 7, 2, 21, 5, tzinfo=timezone.utc),
        )
    )
    app = create_app(settings=settings, llm=FakeLLM([]))

    with TestClient(app) as client:
        response = client.get("/console")

    assert response.status_code == 200
    assert "<script>alert('xss')</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_console_shows_environment_and_queue_depth(tmp_path, runbooks_dir):
    app = create_app(settings=_settings(tmp_path, runbooks_dir), llm=FakeLLM([]))

    with TestClient(app) as client:
        response = client.get("/console")

    assert response.status_code == 200
    assert "Autonomous Incident Response" in response.text
    assert "mock" in response.text
    assert "Queue depth" in response.text


def test_console_serves_stylesheet(tmp_path, runbooks_dir):
    app = create_app(settings=_settings(tmp_path, runbooks_dir), llm=FakeLLM([]))

    with TestClient(app) as client:
        page = client.get("/console")
        stylesheet = client.get("/static/console.css")

    assert "/static/console.css" in page.text
    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
