from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
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
        llm_mode="mock",
        github_mode="mock",
        slack_mode="mock",
        metrics_mode="mock",
        runbooks_dir=runbooks_dir,
        postmortem_dir=tmp_path / "postmortems",
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


def test_console_demo_alert_enqueues_and_redirects_to_detail(tmp_path, runbooks_dir):
    app = create_app(settings=_settings(tmp_path, runbooks_dir))

    with TestClient(app) as client:
        response = client.post("/console/demo-alert", follow_redirects=False)

        assert response.status_code == 303
        location = response.headers["location"]
        assert location.startswith("/console/incidents/inc-demo-checkout-")
        detail = client.get(location)

    assert detail.status_code == 200
    assert "Checkout 5xx &gt; 5%" in detail.text
    assert "checkout service error rate at 18%" in detail.text


def test_console_demo_alert_uses_collision_safe_incident_ids(tmp_path, runbooks_dir):
    app = create_app(settings=_settings(tmp_path, runbooks_dir))

    with TestClient(app) as client:
        first = client.post("/console/demo-alert", follow_redirects=False)
        second = client.post("/console/demo-alert", follow_redirects=False)

    assert first.status_code == 303
    assert second.status_code == 303
    assert first.headers["location"] != second.headers["location"]


@pytest.mark.parametrize(
    ("setting_name", "unsafe_value"),
    [
        ("llm_mode", "anthropic"),
        ("github_mode", "rest"),
        ("slack_mode", "webhook"),
        ("metrics_mode", "datadog"),
        ("remediation_mode", "shell"),
    ],
)
def test_console_demo_alert_is_hidden_and_forbidden_outside_mock_mode(
    tmp_path, runbooks_dir, setting_name, unsafe_value
):
    settings = _settings(tmp_path, runbooks_dir).model_copy(
        update={setting_name: unsafe_value}
    )
    app = create_app(settings=settings, llm=FakeLLM([]))
    app.state.queue.submit = AsyncMock()

    with TestClient(app) as client:
        console = client.get("/console")
        response = client.post("/console/demo-alert")

    assert "Trigger demo incident" not in console.text
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("text/html")
    assert "Demo mode unavailable" in response.text
    app.state.queue.submit.assert_not_awaited()


@pytest.mark.parametrize(
    "headers",
    [
        {"sec-fetch-site": "cross-site"},
        {"origin": "https://attacker.example"},
    ],
)
def test_console_demo_alert_rejects_cross_site_browser_posts(
    tmp_path, runbooks_dir, headers
):
    app = create_app(settings=_settings(tmp_path, runbooks_dir))
    app.state.queue.submit = AsyncMock()

    with TestClient(app) as client:
        response = client.post(
            "/console/demo-alert",
            headers=headers,
        )

    assert response.status_code == 403
    assert "Cross-site request rejected" in response.text
    app.state.queue.submit.assert_not_awaited()


def test_console_demo_alert_returns_safe_html_when_queue_submit_fails(
    tmp_path, runbooks_dir
):
    app = create_app(settings=_settings(tmp_path, runbooks_dir))
    app.state.queue.submit = AsyncMock(side_effect=RuntimeError("database password leaked"))

    with TestClient(app) as client:
        response = client.post("/console/demo-alert")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("text/html")
    assert "Could not queue demo incident" in response.text
    assert "database password leaked" not in response.text


def test_console_demo_alert_handles_slow_worker_without_broken_redirect(
    tmp_path, runbooks_dir, monkeypatch
):
    monkeypatch.setattr("incident_response.console._DEMO_PERSIST_TIMEOUT_SECONDS", 0)
    app = create_app(settings=_settings(tmp_path, runbooks_dir))
    app.state.queue.submit = AsyncMock()

    with TestClient(app) as client:
        response = client.post("/console/demo-alert", follow_redirects=False)

    assert response.status_code == 202
    assert "Demo incident is still queued" in response.text
    assert 'href="/console"' in response.text


def test_console_incident_detail_shows_resolve_form_only_for_open_mock_incidents(
    tmp_path, runbooks_dir, alert
):
    settings = _settings(tmp_path, runbooks_dir)
    store = IncidentStore(settings.db_path)
    store.save(
        _incident(
            incident_id="inc-open-resolve",
            alert=alert,
            status=IncidentStatus.INVESTIGATING,
            created_at=datetime(2026, 7, 2, 21, 5, tzinfo=timezone.utc),
        ).model_copy(update={"triage": _triage()})
    )
    store.save(
        _incident(
            incident_id="inc-already-resolved",
            alert=alert,
            status=IncidentStatus.RESOLVED,
            created_at=datetime(2026, 7, 2, 21, 4, tzinfo=timezone.utc),
        )
    )
    app = create_app(settings=settings, llm=FakeLLM([]))

    with TestClient(app) as client:
        open_detail = client.get("/console/incidents/inc-open-resolve")
        resolved_detail = client.get("/console/incidents/inc-already-resolved")

    assert 'action="/console/incidents/inc-open-resolve/resolve"' in open_detail.text
    assert 'name="resolution_note"' in open_detail.text
    assert 'maxlength="500"' in open_detail.text
    assert "Resolve incident" in open_detail.text
    assert "Resolve incident" not in resolved_detail.text


def test_console_resolve_updates_incident_generates_postmortem_and_redirects(
    tmp_path, runbooks_dir, alert
):
    settings = _settings(tmp_path, runbooks_dir)
    store = IncidentStore(settings.db_path)
    store.save(
        _incident(
            incident_id="inc-console-resolve",
            alert=alert,
            status=IncidentStatus.INVESTIGATING,
            created_at=datetime(2026, 7, 2, 21, 5, tzinfo=timezone.utc),
        ).model_copy(update={"triage": _triage()})
    )
    app = create_app(
        settings=settings,
        llm=FakeLLM([{"markdown": "# Console resolution post-mortem"}]),
    )

    with TestClient(app) as client:
        response = client.post(
            "/console/incidents/inc-console-resolve/resolve",
            data={"resolution_note": "Rolled back <cache-v2>"},
            follow_redirects=False,
        )
        detail = client.get(response.headers["location"])

    assert response.status_code == 303
    assert response.headers["location"] == "/console/incidents/inc-console-resolve"
    resolved = store.get("inc-console-resolve")
    assert resolved is not None
    assert resolved.status == IncidentStatus.RESOLVED
    assert resolved.resolved_at is not None
    assert resolved.timeline[-1]["event"] == "Resolved. Rolled back <cache-v2>"
    assert resolved.postmortem_path is not None
    assert Path(resolved.postmortem_path).read_text(encoding="utf-8").startswith(
        "# Console resolution post-mortem"
    )
    assert detail.status_code == 200
    assert "Resolved. Rolled back &lt;cache-v2&gt;" in detail.text
    assert "Resolve incident" not in detail.text


def test_console_resolve_returns_html_404_for_unknown_incident(tmp_path, runbooks_dir):
    app = create_app(settings=_settings(tmp_path, runbooks_dir), llm=FakeLLM([]))

    with TestClient(app) as client:
        response = client.post(
            "/console/incidents/inc-missing/resolve",
            data={"resolution_note": "not found"},
        )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "Incident not found" in response.text


def test_console_resolve_waits_for_triage_to_finish(tmp_path, runbooks_dir, alert):
    settings = _settings(tmp_path, runbooks_dir)
    store = IncidentStore(settings.db_path)
    store.save(
        _incident(
            incident_id="inc-triage-running",
            alert=alert,
            status=IncidentStatus.INVESTIGATING,
            created_at=datetime(2026, 7, 2, 21, 5, tzinfo=timezone.utc),
        )
    )
    app = create_app(settings=settings, llm=FakeLLM([]))
    app.state.orchestrator.resolve = AsyncMock()

    with TestClient(app) as client:
        detail = client.get("/console/incidents/inc-triage-running")
        response = client.post(
            "/console/incidents/inc-triage-running/resolve",
            data={"resolution_note": "too early"},
        )

    assert "Resolve incident" not in detail.text
    assert response.status_code == 409
    assert "Triage still in progress" in response.text
    app.state.orchestrator.resolve.assert_not_awaited()


def test_console_resolve_rejects_duplicate_resolution(tmp_path, runbooks_dir, alert):
    settings = _settings(tmp_path, runbooks_dir)
    store = IncidentStore(settings.db_path)
    store.save(
        _incident(
            incident_id="inc-resolved-once",
            alert=alert,
            status=IncidentStatus.RESOLVED,
            created_at=datetime(2026, 7, 2, 21, 5, tzinfo=timezone.utc),
        )
    )
    llm = FakeLLM([])
    app = create_app(settings=settings, llm=llm)

    with TestClient(app) as client:
        response = client.post(
            "/console/incidents/inc-resolved-once/resolve",
            data={"resolution_note": "resolve twice"},
        )

    assert response.status_code == 409
    assert "Incident already resolved" in response.text
    assert llm.calls == []


def test_console_resolve_validates_content_type_and_note_length(
    tmp_path, runbooks_dir, alert
):
    settings = _settings(tmp_path, runbooks_dir)
    store = IncidentStore(settings.db_path)
    store.save(
        _incident(
            incident_id="inc-invalid-resolution",
            alert=alert,
            status=IncidentStatus.INVESTIGATING,
            created_at=datetime(2026, 7, 2, 21, 5, tzinfo=timezone.utc),
        )
    )
    app = create_app(settings=settings, llm=FakeLLM([]))

    with TestClient(app) as client:
        wrong_type = client.post(
            "/console/incidents/inc-invalid-resolution/resolve",
            content=b'{"resolution_note":"json is not accepted"}',
            headers={"content-type": "application/json"},
        )
        too_long = client.post(
            "/console/incidents/inc-invalid-resolution/resolve",
            data={"resolution_note": "x" * 501},
        )

    assert wrong_type.status_code == 415
    assert "Expected form data" in wrong_type.text
    assert too_long.status_code == 422
    assert "500 characters or fewer" in too_long.text
    assert store.get("inc-invalid-resolution").status == IncidentStatus.INVESTIGATING


@pytest.mark.parametrize(
    "headers",
    [
        {"sec-fetch-site": "cross-site"},
        {"origin": "https://attacker.example"},
    ],
)
def test_console_resolve_rejects_cross_site_browser_posts(
    tmp_path, runbooks_dir, alert, headers
):
    settings = _settings(tmp_path, runbooks_dir)
    store = IncidentStore(settings.db_path)
    store.save(
        _incident(
            incident_id="inc-cross-site-resolve",
            alert=alert,
            status=IncidentStatus.INVESTIGATING,
            created_at=datetime(2026, 7, 2, 21, 5, tzinfo=timezone.utc),
        )
    )
    app = create_app(settings=settings, llm=FakeLLM([]))
    app.state.orchestrator.resolve = AsyncMock()

    with TestClient(app) as client:
        response = client.post(
            "/console/incidents/inc-cross-site-resolve/resolve",
            data={"resolution_note": "malicious"},
            headers=headers,
        )

    assert response.status_code == 403
    assert "Cross-site request rejected" in response.text
    app.state.orchestrator.resolve.assert_not_awaited()


@pytest.mark.parametrize(
    ("setting_name", "unsafe_value"),
    [
        ("llm_mode", "anthropic"),
        ("github_mode", "rest"),
        ("slack_mode", "webhook"),
        ("metrics_mode", "datadog"),
        ("remediation_mode", "shell"),
    ],
)
def test_console_resolve_is_hidden_and_forbidden_outside_mock_mode(
    tmp_path, runbooks_dir, alert, setting_name, unsafe_value
):
    settings = _settings(tmp_path, runbooks_dir).model_copy(
        update={setting_name: unsafe_value}
    )
    store = IncidentStore(settings.db_path)
    store.save(
        _incident(
            incident_id="inc-real-mode-resolve",
            alert=alert,
            status=IncidentStatus.INVESTIGATING,
            created_at=datetime(2026, 7, 2, 21, 5, tzinfo=timezone.utc),
        )
    )
    app = create_app(settings=settings, llm=FakeLLM([]))
    app.state.orchestrator.resolve = AsyncMock()

    with TestClient(app) as client:
        detail = client.get("/console/incidents/inc-real-mode-resolve")
        response = client.post(
            "/console/incidents/inc-real-mode-resolve/resolve",
            data={"resolution_note": "unsafe"},
        )

    assert "Resolve incident" not in detail.text
    assert response.status_code == 403
    assert "Console writes unavailable" in response.text
    app.state.orchestrator.resolve.assert_not_awaited()


def test_console_resolve_returns_safe_html_when_resolution_fails(
    tmp_path, runbooks_dir, alert
):
    settings = _settings(tmp_path, runbooks_dir)
    store = IncidentStore(settings.db_path)
    store.save(
        _incident(
            incident_id="inc-failed-resolve",
            alert=alert,
            status=IncidentStatus.INVESTIGATING,
            created_at=datetime(2026, 7, 2, 21, 5, tzinfo=timezone.utc),
        ).model_copy(update={"triage": _triage()})
    )
    app = create_app(settings=settings, llm=FakeLLM([]))
    app.state.orchestrator.resolve = AsyncMock(
        side_effect=RuntimeError("secret postmortem filesystem path")
    )

    with TestClient(app) as client:
        response = client.post(
            "/console/incidents/inc-failed-resolve/resolve",
            data={"resolution_note": "safe note"},
        )

    assert response.status_code == 500
    assert "Could not resolve incident" in response.text
    assert "secret postmortem filesystem path" not in response.text
