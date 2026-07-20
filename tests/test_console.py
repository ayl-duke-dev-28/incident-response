from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from incident_response.agents.llm import FakeLLM
from incident_response.config import Settings
from incident_response.db import IncidentStore
from incident_response.main import create_app
from incident_response.models import (
    Alert,
    Commit,
    ImpactEstimate,
    Incident,
    IncidentStatus,
    Runbook,
    RunbookMatch,
    SuspectCommit,
    TriageReport,
    VerificationOutcome,
)


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


def _triage() -> TriageReport:
    return TriageReport(
        suspects=[
            SuspectCommit(
                commit=Commit(
                    sha="a1b2c3d4e5f6",
                    author="jamie",
                    message="perf: switch pricing to new cache",
                    timestamp=datetime(2026, 7, 2, 20, 57, tzinfo=timezone.utc),
                    files_changed=["services/checkout/pricing.py"],
                    pr_number=4821,
                    pr_url="https://github.example/pull/4821",
                ),
                confidence=0.87,
                reasoning="deployed 8m before alert",
            )
        ],
        runbook=RunbookMatch(
            runbook=Runbook(
                slug="checkout-error-rate",
                title="Checkout elevated error rate",
                tags=["checkout"],
                content="body",
                path="runbooks/checkout-error-rate.md",
            ),
            confidence=0.92,
            reasoning="matches symptoms",
        ),
        impact=ImpactEstimate(
            affected_users=2232,
            affected_percent=18.0,
            error_rate=0.184,
            reasoning="18% of 12400 active users",
        ),
        summary="Top suspect is the pricing cache rollout.",
    )


def test_console_incident_detail_renders_triage_remediation_and_resolution(
    tmp_path, runbooks_dir, alert
):
    settings = _settings(tmp_path, runbooks_dir)
    store = IncidentStore(settings.db_path)
    store.save(
        Incident(
            id="inc-ddg-9273",
            alert=alert,
            status=IncidentStatus.RESOLVED,
            created_at=datetime(2026, 7, 2, 21, 5, tzinfo=timezone.utc),
            resolved_at=datetime(2026, 7, 2, 21, 20, tzinfo=timezone.utc),
            triage=_triage(),
            timeline=[
                {
                    "timestamp": "2026-07-02T21:10:00+00:00",
                    "event": "Remediation attempted: rollback=executed",
                },
                {
                    "timestamp": "2026-07-02T21:20:00+00:00",
                    "event": "Resolved. rolled back a1b2c3d4",
                },
            ],
            verification_outcome=VerificationOutcome(
                status="recovered",
                baseline_peak=0.184,
                final_peak=0.012,
                minutes_elapsed=10,
                message="Error rate returned below threshold.",
                runbook_slug="checkout-error-rate",
            ),
            postmortem_path="postmortems/2026-07-02-inc-ddg-9273.md",
        )
    )
    app = create_app(settings=settings, llm=FakeLLM([]))

    with TestClient(app) as client:
        response = client.get("/console/incidents/inc-ddg-9273")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    for expected in (
        "Checkout 5xx &gt; 5%",
        "inc-ddg-9273",
        "checkout service error rate at 18%",
        "Top suspect is the pricing cache rollout.",
        "a1b2c3d4e5f6",
        "87%",
        "deployed 8m before alert",
        "2,232",
        "18%",
        "Checkout elevated error rate",
        "92%",
        "Remediation attempted: rollback=executed",
        "Resolved. rolled back a1b2c3d4",
        "recovered",
        "Error rate returned below threshold.",
        "postmortems/2026-07-02-inc-ddg-9273.md",
    ):
        assert expected in response.text
    assert 'href="/console"' in response.text


def test_console_incident_detail_handles_triage_in_progress(tmp_path, runbooks_dir, alert):
    settings = _settings(tmp_path, runbooks_dir)
    store = IncidentStore(settings.db_path)
    store.save(
        _incident(
            incident_id="inc-in-progress",
            alert=alert,
            status=IncidentStatus.INVESTIGATING,
            created_at=datetime(2026, 7, 2, 21, 5, tzinfo=timezone.utc),
        )
    )
    app = create_app(settings=settings, llm=FakeLLM([]))

    with TestClient(app) as client:
        response = client.get("/console/incidents/inc-in-progress")

    assert response.status_code == 200
    assert "Triage in progress" in response.text
    assert "No timeline events recorded" in response.text


def test_console_incident_detail_returns_html_404(tmp_path, runbooks_dir):
    app = create_app(settings=_settings(tmp_path, runbooks_dir), llm=FakeLLM([]))

    with TestClient(app) as client:
        response = client.get("/console/incidents/inc-missing")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "Incident not found" in response.text
    assert "inc-missing" in response.text
    assert 'href="/console"' in response.text


def test_console_incident_detail_escapes_untrusted_content(tmp_path, runbooks_dir, alert):
    settings = _settings(tmp_path, runbooks_dir)
    store = IncidentStore(settings.db_path)
    store.save(
        Incident(
            id="inc-xss-detail",
            alert=alert.model_copy(
                update={
                    "description": '<img src=x onerror="alert(1)">',
                    "tags": {"<script>": "danger&value"},
                }
            ),
            status=IncidentStatus.INVESTIGATING,
            created_at=datetime(2026, 7, 2, 21, 5, tzinfo=timezone.utc),
            timeline=[{"timestamp": "now", "event": "<script>alert(2)</script>"}],
        )
    )
    app = create_app(settings=settings, llm=FakeLLM([]))

    with TestClient(app) as client:
        response = client.get("/console/incidents/inc-xss-detail")

    assert response.status_code == 200
    assert "<script>" not in response.text
    assert "<img src=x" not in response.text
    assert "&lt;script&gt;" in response.text
    assert "danger&amp;value" in response.text
