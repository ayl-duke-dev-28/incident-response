"""End-to-end webhook test using FastAPI's TestClient with a FakeLLM injected."""

from pathlib import Path

from fastapi.testclient import TestClient

from incident_response.agents.llm import FakeLLM
from incident_response.config import Settings
from incident_response.main import create_app


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        anthropic_api_key="test",
        github_mode="mock",
        slack_mode="mock",
        metrics_mode="mock",
        runbooks_dir=Path(__file__).parent.parent / "runbooks",
        db_path=tmp_path / "incidents.db",
        webhook_token="secret",
    )


def _alert_payload():
    return {
        "id": "ddg-9273",
        "title": "Checkout 5xx > 5%",
        "description": "checkout service error rate at 18%",
        "service": "checkout",
        "severity": "sev2",
        "triggered_at": "2026-07-02T21:05:00+00:00",
        "metric": "http.error_rate",
        "threshold": 0.05,
        "value": 0.184,
        "tags": {"env": "prod"},
    }


def test_fire_alert_returns_202_and_processes_async(tmp_path):
    llm = FakeLLM(
        [
            {"suspects": [{"sha": "a1b2c3d", "confidence": 0.9, "reasoning": "checkout"}]},
            {"slug": "checkout-error-rate", "confidence": 0.9, "reasoning": "match"},
            {"affected_users": 2200, "affected_percent": 17.7, "error_rate": 0.184, "reasoning": "..."},
        ]
    )
    app = create_app(settings=_settings(tmp_path), llm=llm)
    with TestClient(app) as client:
        resp = client.post(
            "/alerts", json=_alert_payload(), headers={"x-webhook-token": "secret"}
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body == {"status": "accepted", "incident_id": "inc-ddg-9273"}

        # Drain the worker
        import time
        for _ in range(50):
            fetched = client.get("/incidents/inc-ddg-9273")
            if fetched.status_code == 200 and fetched.json().get("triage"):
                break
            time.sleep(0.05)
        assert fetched.status_code == 200
        assert fetched.json()["triage"]["suspects"][0]["commit"]["sha"] == "a1b2c3d"


def test_fire_alert_rejects_bad_token(tmp_path):
    app = create_app(settings=_settings(tmp_path), llm=FakeLLM([]))
    client = TestClient(app)
    resp = client.post("/alerts", json=_alert_payload(), headers={"x-webhook-token": "wrong"})
    assert resp.status_code == 401


def test_healthz(tmp_path):
    app = create_app(settings=_settings(tmp_path), llm=FakeLLM([]))
    client = TestClient(app)
    assert client.get("/healthz").json() == {"status": "ok"}
