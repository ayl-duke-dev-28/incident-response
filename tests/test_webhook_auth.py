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


def test_provider_endpoints_verify_their_own_signature_and_normalize(tmp_path):
    datadog = {
        "id": "event-7",
        "title": "Checkout errors",
        "date": 1_722_528_000,
        "service": "checkout",
        "metric": "http.errors",
    }
    body = json.dumps(datadog).encode()
    signature = base64.b64encode(
        hmac.new(b"dd-secret", body, hashlib.sha256).digest()
    ).decode()
    settings = _settings(
        tmp_path,
        datadog_webhook_secret="dd-secret",
        pagerduty_webhook_secret="pd-secret",
    )

    with TestClient(create_app(settings=settings, llm=_llm())) as client:
        accepted = client.post(
            "/alerts/datadog",
            content=body,
            headers={
                "content-type": "application/json",
                "x-datadog-signature": signature,
            },
        )
        wrong_provider = client.post(
            "/alerts/pagerduty",
            content=body,
            headers={
                "content-type": "application/json",
                "x-datadog-signature": signature,
            },
        )

    assert accepted.status_code == 202
    assert accepted.json()["incident_id"] == "inc-datadog:event-7"
    assert wrong_provider.status_code == 401


def test_invalid_provider_payload_returns_422_without_enqueueing(tmp_path):
    body = b'{"unexpected": true}'
    signature = base64.b64encode(
        hmac.new(b"dd-secret", body, hashlib.sha256).digest()
    ).decode()
    settings = _settings(tmp_path, datadog_webhook_secret="dd-secret")

    with TestClient(create_app(settings=settings, llm=_llm())) as client:
        response = client.post(
            "/alerts/datadog",
            content=body,
            headers={
                "content-type": "application/json",
                "x-datadog-signature": signature,
            },
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid datadog alert payload"}
