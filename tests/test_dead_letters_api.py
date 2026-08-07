import sqlite3
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from incident_response.agents.llm import FakeLLM
from incident_response.config import Settings
from incident_response.main import create_app
from incident_response.models import Alert, Severity


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


def _alert(alert_id: str) -> Alert:
    from datetime import datetime, timezone

    return Alert(
        id=alert_id,
        title=f"Alert {alert_id}",
        service="checkout",
        severity=Severity.SEV2,
        triggered_at=datetime(2026, 8, 6, 12, tzinfo=timezone.utc),
    )


def _seed_dead_letter(
    db_path: Path,
    *,
    alert_id: str,
    attempt_count: int,
    last_error: str,
    failed_at: str,
) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO alert_dead_letters
                (alert_id, payload, attempt_count, last_error, failed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                alert_id,
                _alert(alert_id).model_dump_json(),
                attempt_count,
                last_error,
                failed_at,
            ),
        )
        conn.commit()


def test_list_dead_letters_requires_authentication(tmp_path, runbooks_dir):
    app = create_app(settings=_settings(tmp_path, runbooks_dir), llm=FakeLLM([]))

    with TestClient(app) as client:
        response = client.get("/dead-letters")

    assert response.status_code == 401


def test_list_dead_letters_returns_newest_first_with_failure_context(
    tmp_path, runbooks_dir
):
    settings = _settings(tmp_path, runbooks_dir)
    app = create_app(settings=settings, llm=FakeLLM([]))
    _seed_dead_letter(
        settings.db_path,
        alert_id="older",
        attempt_count=5,
        last_error="old failure",
        failed_at="2026-08-06 12:00:00",
    )
    _seed_dead_letter(
        settings.db_path,
        alert_id="newer",
        attempt_count=3,
        last_error="new failure",
        failed_at="2026-08-06 12:01:00",
    )

    with TestClient(app) as client:
        response = client.get(
            "/dead-letters", headers={"x-webhook-token": "secret"}
        )

    assert response.status_code == 200
    assert response.json() == [
        {
            "alert": _alert("newer").model_dump(mode="json"),
            "attempt_count": 3,
            "last_error": "new failure",
            "failed_at": "2026-08-06T12:01:00",
        },
        {
            "alert": _alert("older").model_dump(mode="json"),
            "attempt_count": 5,
            "last_error": "old failure",
            "failed_at": "2026-08-06T12:00:00",
        },
    ]


def test_list_dead_letters_applies_limit_without_deleting_rows(tmp_path, runbooks_dir):
    settings = _settings(tmp_path, runbooks_dir)
    app = create_app(settings=settings, llm=FakeLLM([]))
    _seed_dead_letter(
        settings.db_path,
        alert_id="older",
        attempt_count=5,
        last_error="old failure",
        failed_at="2026-08-06 12:00:00",
    )
    _seed_dead_letter(
        settings.db_path,
        alert_id="newer",
        attempt_count=5,
        last_error="new failure",
        failed_at="2026-08-06 12:01:00",
    )

    with TestClient(app) as client:
        response = client.get(
            "/dead-letters?limit=1", headers={"x-webhook-token": "secret"}
        )

    assert response.status_code == 200
    assert [item["alert"]["id"] for item in response.json()] == ["newer"]
    with closing(sqlite3.connect(settings.db_path)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM alert_dead_letters").fetchone()
    assert count == (2,)


def test_list_dead_letters_rejects_out_of_range_limit(tmp_path, runbooks_dir):
    app = create_app(settings=_settings(tmp_path, runbooks_dir), llm=FakeLLM([]))

    with TestClient(app) as client:
        too_small = client.get(
            "/dead-letters?limit=0", headers={"x-webhook-token": "secret"}
        )
        too_large = client.get(
            "/dead-letters?limit=201", headers={"x-webhook-token": "secret"}
        )

    assert too_small.status_code == 422
    assert too_large.status_code == 422
