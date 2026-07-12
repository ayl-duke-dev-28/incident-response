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


def test_list_incidents_empty(tmp_path, runbooks_dir):
    app = create_app(settings=_settings(tmp_path, runbooks_dir), llm=FakeLLM([]))
    with TestClient(app) as client:
        response = client.get("/incidents")

    assert response.status_code == 200
    assert response.json() == []


def _seed_incidents(settings: Settings, alert: Alert) -> None:
    store = IncidentStore(settings.db_path)
    store.save(
        _incident(
            incident_id="inc-oldest",
            alert=alert,
            status=IncidentStatus.RESOLVED,
            created_at=datetime(2026, 7, 2, 21, 5, tzinfo=timezone.utc),
        )
    )
    store.save(
        _incident(
            incident_id="inc-newest",
            alert=alert,
            status=IncidentStatus.INVESTIGATING,
            created_at=datetime(2026, 7, 2, 21, 7, tzinfo=timezone.utc),
        )
    )
    store.save(
        _incident(
            incident_id="inc-middle",
            alert=alert,
            status=IncidentStatus.MITIGATED,
            created_at=datetime(2026, 7, 2, 21, 6, tzinfo=timezone.utc),
        )
    )


def test_list_incidents_orders_newest_first(tmp_path, runbooks_dir, alert):
    settings = _settings(tmp_path, runbooks_dir)
    _seed_incidents(settings, alert)
    app = create_app(settings=settings, llm=FakeLLM([]))

    with TestClient(app) as client:
        response = client.get("/incidents")

    assert response.status_code == 200
    assert [incident["id"] for incident in response.json()] == [
        "inc-newest",
        "inc-middle",
        "inc-oldest",
    ]


def test_list_incidents_filters_by_status(tmp_path, runbooks_dir, alert):
    settings = _settings(tmp_path, runbooks_dir)
    _seed_incidents(settings, alert)
    app = create_app(settings=settings, llm=FakeLLM([]))

    with TestClient(app) as client:
        response = client.get("/incidents?status=resolved")

    assert response.status_code == 200
    assert [incident["id"] for incident in response.json()] == ["inc-oldest"]


def test_list_incidents_applies_limit(tmp_path, runbooks_dir, alert):
    settings = _settings(tmp_path, runbooks_dir)
    _seed_incidents(settings, alert)
    app = create_app(settings=settings, llm=FakeLLM([]))

    with TestClient(app) as client:
        response = client.get("/incidents?limit=2")

    assert response.status_code == 200
    assert [incident["id"] for incident in response.json()] == [
        "inc-newest",
        "inc-middle",
    ]
