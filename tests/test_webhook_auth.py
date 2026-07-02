import base64
import hashlib
import hmac
import json
from pathlib import Path

from fastapi.testclient import TestClient

from incident_response.agents.llm import FakeLLM
from incident_response.config import Settings
from incident_response.main import create_app


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        anthropic_api_key="test",
        github_mode="mock",
        slack_mode="mock",
        metrics_mode="mock",
        runbooks_dir=Path(__file__).parent.parent / "runbooks",
        db_path=tmp_path / "incidents.db",
        webhook_token="",  # disable token auth to isolate HMAC path
    )
    base.update(overrides)
    return Settings(**base)


_ALERT = {
    "id": "ddg-42",
    "title": "x",
    "service": "checkout",
    "severity": "sev3",
    "triggered_at": "2026-07-02T21:00:00+00:00",
    "metric": "http.error_rate",
}


def _llm():
    return FakeLLM(
        [
            {"suspects": []},
            {"slug": "", "confidence": 0.0, "reasoning": ""},
            {"affected_users": 1, "affected_percent": 0.1, "error_rate": 0.01, "reasoning": "."},
        ]
    )


def test_valid_datadog_signature_accepted(tmp_path):
    body = json.dumps(_ALERT).encode()
    settings = _settings(tmp_path, datadog_webhook_secret="dd-secret")
    sig = base64.b64encode(hmac.new(b"dd-secret", body, hashlib.sha256).digest()).decode()
    with TestClient(create_app(settings=settings, llm=_llm())) as client:
        resp = client.post(
            "/alerts", content=body,
            headers={"content-type": "application/json", "x-datadog-signature": sig},
        )
        assert resp.status_code == 202


def test_missing_credentials_rejected(tmp_path):
    settings = _settings(tmp_path, datadog_webhook_secret="dd-secret")
    with TestClient(create_app(settings=settings, llm=_llm())) as client:
        resp = client.post("/alerts", json=_ALERT)
        assert resp.status_code == 401


def test_rate_limit_returns_429(tmp_path):
    settings = _settings(
        tmp_path,
        webhook_token="tok",
        rate_limit_max=2,
        rate_limit_window_seconds=60,
    )
    with TestClient(create_app(settings=settings, llm=FakeLLM([{"suspects": []}] * 30))) as client:
        headers = {"x-webhook-token": "tok"}
        assert client.post("/alerts", json=_ALERT, headers=headers).status_code == 202
        assert client.post("/alerts", json=_ALERT, headers=headers).status_code == 202
        assert client.post("/alerts", json=_ALERT, headers=headers).status_code == 429
