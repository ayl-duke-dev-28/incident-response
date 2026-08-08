import json
from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from incident_response.config import Settings
from incident_response.main import create_app
from incident_response.models import (
    Alert,
    Incident,
    RemediationRequest,
    RemediationStatus,
    RemediationStep,
    Severity,
)
from incident_response.postgres import (
    PostgresAlertStore,
    PostgresDatabase,
    PostgresIncidentStore,
    apply_postgres_migrations,
)


class Result:
    def __init__(self, *, one=None, all_rows=None, rowcount=1) -> None:
        self._one = one
        self._all = all_rows or []
        self.rowcount = rowcount

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class RecordingConnection:
    def __init__(self, results=None) -> None:
        self.results = list(results or [])
        self.calls: list[tuple[str, tuple[object, ...] | None]] = []

    def execute(self, sql: str, params=None) -> Result:
        self.calls.append((" ".join(sql.split()), params))
        return self.results.pop(0) if self.results else Result()


class RecordingDatabase:
    def __init__(self, connection: RecordingConnection) -> None:
        self.connection_value = connection

    @contextmanager
    def connection(self):
        yield self.connection_value


class LifecycleDatabase(RecordingDatabase):
    def __init__(self, connection: RecordingConnection) -> None:
        super().__init__(connection)
        self.events: list[str] = []

    def open(self, *, timeout: float = 30) -> None:
        self.events.append("open")

    def migrate(self) -> None:
        self.events.append("migrate")

    def close(self) -> None:
        self.events.append("close")


class RecordingPool:
    def __init__(self, connection: RecordingConnection) -> None:
        self.connection_value = connection
        self.open_calls: list[tuple[bool, float]] = []
        self.closed = False

    def open(self, *, wait: bool, timeout: float) -> None:
        self.open_calls.append((wait, timeout))

    @contextmanager
    def connection(self):
        yield self.connection_value

    def close(self) -> None:
        self.closed = True


def _alert() -> Alert:
    return Alert(
        id="pg-alert",
        title="Checkout failures",
        service="checkout",
        severity=Severity.SEV2,
        triggered_at=datetime.now(timezone.utc),
    )


def test_postgres_migrations_are_versioned_and_create_all_foundation_tables():
    connection = RecordingConnection(results=[Result(one=None)])

    apply_postgres_migrations(connection)

    sql = " ".join(statement for statement, _ in connection.calls)
    assert "schema_migrations" in sql
    assert "incidents" in sql
    assert "alert_queue" in sql
    assert "alert_dead_letters" in sql
    assert any("INSERT INTO schema_migrations" in statement for statement, _ in connection.calls)


def test_postgres_database_owns_pool_lifecycle_and_runs_migrations():
    connection = RecordingConnection()
    pool = RecordingPool(connection)
    factory_calls = []

    def pool_factory(conninfo, **kwargs):
        factory_calls.append((conninfo, kwargs))
        return pool

    database = PostgresDatabase(
        "postgresql+psycopg://app:secret@db/incidents",
        pool_factory=pool_factory,
        row_factory="dict_row",
    )

    database.open(timeout=7)
    database.migrate()
    database.close()

    assert factory_calls == [
        (
            "postgresql://app:secret@db/incidents",
            {"open": False, "kwargs": {"row_factory": "dict_row"}},
        )
    ]
    assert pool.open_calls == [(True, 7)]
    assert any("schema_migrations" in statement for statement, _ in connection.calls)
    assert pool.closed is True


def test_postgres_queue_claims_due_work_with_skip_locked():
    alert = _alert()
    connection = RecordingConnection(
        results=[
            Result(one={"alert_id": alert.id, "payload": json.loads(alert.model_dump_json())}),
            Result(),
        ]
    )
    store = PostgresAlertStore(RecordingDatabase(connection))

    claimed = store.claim_next(now=100, lease_seconds=30)

    assert claimed is not None
    assert claimed.alert == alert
    assert any("FOR UPDATE SKIP LOCKED" in statement for statement, _ in connection.calls)


def test_postgres_remediation_decision_locks_latest_incident_row():
    now = datetime.now(timezone.utc)
    incident = Incident(
        id="inc-pg",
        alert=_alert(),
        created_at=now,
        remediation=RemediationRequest(
            status=RemediationStatus.PENDING,
            runbook_slug="checkout-errors",
            requested_at=now,
            steps=[RemediationStep(name="rollback", command="deploy rollback")],
        ),
    )
    connection = RecordingConnection(
        results=[Result(one={"payload": json.loads(incident.model_dump_json())}), Result()]
    )
    store = PostgresIncidentStore(RecordingDatabase(connection))

    decided = store.decide_remediation(
        incident.id,
        decision=RemediationStatus.APPROVED,
        decided_at=now,
        decided_by="operator@example.com",
        note="approved",
    )

    assert decided.remediation is not None
    assert decided.remediation.status == RemediationStatus.APPROVED
    assert any("FOR UPDATE" in statement for statement, _ in connection.calls)


def test_production_app_uses_postgres_stores_and_manages_database_lifecycle(
    runbooks_dir,
):
    database = LifecycleDatabase(RecordingConnection())

    class Redis:
        async def eval(self, *args):
            return [1, 0]

        async def get(self, key):
            return None

        async def set(self, key, value, *, ex):
            return True

        async def delete(self, key):
            return 0

    settings = Settings(
        _env_file=None,
        environment="production",
        database_url="postgresql+psycopg://app:secret@db/incidents",
        redis_url="rediss://redis:6379/0",
        auth_mode="oidc",
        session_secret="s" * 32,
        oidc_client_id="incident-response",
        oidc_client_secret="client-secret",
        oidc_metadata_url="https://identity.example/.well-known/openid-configuration",
        llm_mode="mock",
        runbooks_dir=runbooks_dir,
    )

    app = create_app(settings, redis_client=Redis(), postgres_database=database)

    assert isinstance(app.state.orchestrator.store, PostgresIncidentStore)
    assert isinstance(app.state.queue._store, PostgresAlertStore)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
    assert database.events == ["open", "migrate", "close"]
