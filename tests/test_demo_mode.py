from pathlib import Path

from fastapi.testclient import TestClient

from incident_response.config import Settings
from incident_response.main import create_app


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        llm_mode="mock",
        github_mode="mock",
        slack_mode="mock",
        metrics_mode="mock",
        runbooks_dir=Path(__file__).parent.parent / "runbooks",
        postmortem_dir=tmp_path / "postmortems",
        db_path=tmp_path / "incidents.db",
        webhook_token="secret",
        verification_enabled=False,
    )


def _alert_payload() -> dict[str, object]:
    return {
        "id": "demo-checkout-001",
        "title": "Checkout 5xx > 5%",
        "description": "checkout service error rate at 18%",
        "service": "checkout",
        "severity": "sev2",
        "triggered_at": "2026-07-02T21:05:00+00:00",
        "metric": "http.error_rate",
        "threshold": 0.05,
        "value": 0.184,
        "tags": {"env": "demo"},
    }


def _wait_for_triage(client: TestClient, incident_id: str) -> dict[str, object]:
    import time

    for _ in range(50):
        resp = client.get(f"/incidents/{incident_id}")
        if resp.status_code == 200 and resp.json().get("triage"):
            return resp.json()
        time.sleep(0.05)
    raise AssertionError(f"triage did not complete for {incident_id}")


def test_mock_llm_mode_runs_full_incident_flow_without_anthropic_key(tmp_path):
    app = create_app(settings=_settings(tmp_path))

    with TestClient(app) as client:
        accepted = client.post(
            "/alerts", json=_alert_payload(), headers={"x-webhook-token": "secret"}
        )
        assert accepted.status_code == 202, accepted.text
        incident_id = accepted.json()["incident_id"]

        incident = _wait_for_triage(client, incident_id)
        assert incident["triage"]["suspects"][0]["commit"]["sha"] == "a1b2c3d"
        assert incident["triage"]["runbook"]["runbook"]["slug"] == "checkout-error-rate"

        resolved = client.post(
            f"/alerts/{incident_id}/resolve",
            json={"resolution_note": "demo rollback complete"},
            headers={"x-webhook-token": "secret"},
        )

    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["status"] == "resolved"
    assert body["postmortem_path"]
    assert Path(body["postmortem_path"]).exists()


def test_demo_cli_runs_full_flow_without_anthropic_key(tmp_path, capsys):
    from incident_response.cli import main

    exit_code = main(
        [
            "demo",
            "--db-path",
            str(tmp_path / "demo.db"),
            "--postmortem-dir",
            str(tmp_path / "postmortems"),
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "accepted inc-demo-checkout-001" in output
    assert "triaged checkout-error-rate" in output
    assert "resolved inc-demo-checkout-001" in output
    assert "postmortem " in output
