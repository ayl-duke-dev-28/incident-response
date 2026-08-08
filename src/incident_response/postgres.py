"""PostgreSQL persistence used by the production runtime profile."""

from __future__ import annotations

import json
import hashlib
import time
from datetime import datetime, timedelta
from typing import Any, Callable, ContextManager, Protocol
from uuid import uuid4

from .models import (
    Alert,
    CorrelationResult,
    ExternalReference,
    Incident,
    IncidentStatus,
    OutboxMessage,
    RemediationStatus,
)
from .queue import (
    AlertAlreadyQueuedError,
    DeadLetter,
    DeadLetterNotFoundError,
    _ClaimedAlert,
    _retry_delay,
)


class PostgresConnection(Protocol):
    def execute(self, sql: str, params: object = None) -> Any:
        ...


class PostgresDatabaseLike(Protocol):
    def connection(self) -> ContextManager[PostgresConnection]:
        ...


class PostgresDatabase:
    """Own a psycopg connection pool and the production schema lifecycle."""

    def __init__(
        self,
        database_url: str,
        *,
        pool_factory: Callable[..., Any] | None = None,
        row_factory: object | None = None,
    ) -> None:
        prefix = "postgresql+psycopg://"
        if not database_url.startswith(prefix):
            raise ValueError("PostgreSQL URL must use the postgresql+psycopg driver")
        conninfo = "postgresql://" + database_url.removeprefix(prefix)
        if pool_factory is None or row_factory is None:
            try:
                from psycopg.rows import dict_row
                from psycopg_pool import ConnectionPool
            except ImportError as exc:
                raise RuntimeError(
                    "PostgreSQL requires the production dependencies; "
                    "install incident-response[production]"
                ) from exc
            pool_factory = pool_factory or ConnectionPool
            row_factory = row_factory or dict_row
        self._pool = pool_factory(
            conninfo,
            open=False,
            kwargs={"row_factory": row_factory},
        )

    def open(self, *, timeout: float = 30.0) -> None:
        self._pool.open(wait=True, timeout=timeout)

    def migrate(self) -> None:
        with self.connection() as connection:
            apply_postgres_migrations(connection)

    def connection(self) -> ContextManager[PostgresConnection]:
        return self._pool.connection()

    def close(self) -> None:
        self._pool.close()


_FOUNDATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS incidents_status_idx ON incidents(status);

CREATE TABLE IF NOT EXISTS alert_queue (
    alert_id TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_error TEXT,
    lease_token TEXT,
    lease_expires_at DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS alert_queue_due_idx
ON alert_queue(next_attempt_at, created_at, alert_id);

CREATE TABLE IF NOT EXISTS alert_dead_letters (
    alert_id TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    attempt_count INTEGER NOT NULL,
    last_error TEXT NOT NULL,
    failed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_CORRELATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS incident_alerts (
    source TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    correlation_key TEXT NOT NULL,
    triggered_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (source, provider_event_id)
);
CREATE INDEX IF NOT EXISTS incident_alerts_incident_idx
ON incident_alerts(incident_id);

CREATE TABLE IF NOT EXISTS incident_correlations (
    correlation_key TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    last_alert_at TIMESTAMPTZ NOT NULL
);
"""

_OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY,
    aggregate_id TEXT NOT NULL REFERENCES incidents(id),
    destination TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_error TEXT,
    lease_token TEXT,
    lease_expires_at DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS outbox_due_idx
ON outbox(status, next_attempt_at, created_at, id);
"""


def apply_postgres_migrations(connection: PostgresConnection) -> None:
    """Apply idempotent schema versions within the caller's transaction."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    row = connection.execute(
        "SELECT version FROM schema_migrations WHERE version = %s",
        (1,),
    ).fetchone()
    if row is None:
        connection.execute(_FOUNDATION_SCHEMA)
        connection.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s)",
            (1,),
        )
    row = connection.execute(
        "SELECT version FROM schema_migrations WHERE version = %s",
        (2,),
    ).fetchone()
    if row is None:
        connection.execute(_CORRELATION_SCHEMA)
        connection.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s)",
            (2,),
        )
    row = connection.execute(
        "SELECT version FROM schema_migrations WHERE version = %s",
        (3,),
    ).fetchone()
    if row is None:
        connection.execute(_OUTBOX_SCHEMA)
        connection.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s)",
            (3,),
        )


def _model_payload(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        loaded = json.loads(value)
        if isinstance(loaded, dict):
            return loaded
    raise TypeError("PostgreSQL JSON payload must be an object")


class PostgresIncidentStore:
    def __init__(
        self,
        database: PostgresDatabaseLike,
        *,
        outbox_destinations: tuple[str, ...] = (),
    ) -> None:
        self._database = database
        self._outbox_destinations = outbox_destinations

    @staticmethod
    def _save_with_conn(connection: PostgresConnection, incident: Incident) -> None:
        connection.execute(
            """
            INSERT INTO incidents (id, status, payload, created_at, updated_at)
            VALUES (%s, %s, %s::jsonb, %s, now())
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                payload = excluded.payload,
                updated_at = now()
            """,
            (
                incident.id,
                incident.status.value,
                incident.model_dump_json(),
                incident.created_at,
            ),
        )

    @staticmethod
    def _get_with_conn(
        connection: PostgresConnection,
        incident_id: str,
        *,
        for_update: bool = False,
    ) -> Incident | None:
        lock = " FOR UPDATE" if for_update else ""
        row = connection.execute(
            f"SELECT payload FROM incidents WHERE id = %s{lock}",
            (incident_id,),
        ).fetchone()
        if row is None:
            return None
        return Incident.model_validate(_model_payload(row["payload"]))

    def save(self, incident: Incident) -> None:
        with self._database.connection() as connection:
            self._save_with_conn(connection, incident)

    def get(self, incident_id: str) -> Incident | None:
        with self._database.connection() as connection:
            return self._get_with_conn(connection, incident_id)

    def _enqueue_outbox(
        self,
        connection: PostgresConnection,
        incident: Incident,
    ) -> None:
        for destination in self._outbox_destinations:
            idempotency_key = f"incident:{incident.id}:ticket:{destination}"
            message_id = hashlib.sha256(idempotency_key.encode()).hexdigest()
            connection.execute(
                """
                INSERT INTO outbox (id, aggregate_id, destination, idempotency_key)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (message_id, incident.id, destination, idempotency_key),
            )

    def save_with_ticket_outbox(self, incident: Incident) -> None:
        with self._database.connection() as connection:
            self._save_with_conn(connection, incident)
            self._enqueue_outbox(connection, incident)

    @staticmethod
    def _outbox_from_row(row: object) -> OutboxMessage:
        return OutboxMessage(
            id=row["id"],
            aggregate_id=row["aggregate_id"],
            destination=row["destination"],
            idempotency_key=row["idempotency_key"],
            status=row["status"],
            attempt_count=row["attempt_count"],
            next_attempt_at=row["next_attempt_at"],
            lease_token=row["lease_token"],
            lease_expires_at=row["lease_expires_at"],
        )

    def claim_outbox(self, *, now: float, lease_seconds: float) -> OutboxMessage | None:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                SELECT id, aggregate_id, destination, idempotency_key, status,
                       attempt_count, next_attempt_at, lease_token, lease_expires_at
                FROM outbox
                WHERE status = 'pending'
                  AND next_attempt_at <= %s
                  AND (lease_token IS NULL OR lease_expires_at <= %s)
                ORDER BY created_at, id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                (now, now),
            ).fetchone()
            if row is None:
                return None
            lease_token = uuid4().hex
            lease_expires_at = now + lease_seconds
            connection.execute(
                "UPDATE outbox SET lease_token = %s, lease_expires_at = %s WHERE id = %s",
                (lease_token, lease_expires_at, row["id"]),
            )
            values = dict(row)
            values.update(
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
            )
            return self._outbox_from_row(values)

    def complete_outbox(
        self,
        message: OutboxMessage,
        reference: ExternalReference,
    ) -> bool:
        if not message.lease_token:
            return False
        with self._database.connection() as connection:
            row = connection.execute(
                """
                SELECT status, lease_token FROM outbox
                WHERE id = %s FOR UPDATE
                """,
                (message.id,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "pending"
                or row["lease_token"] != message.lease_token
            ):
                return False
            incident = self._get_with_conn(connection, message.aggregate_id, for_update=True)
            if incident is None:
                raise RuntimeError("Outbox message references a missing incident")
            references = incident.external_references
            if not any(
                item.provider == reference.provider
                and item.external_id == reference.external_id
                for item in references
            ):
                incident = incident.model_copy(
                    update={"external_references": references + [reference]}
                )
                self._save_with_conn(connection, incident)
            result = connection.execute(
                """
                UPDATE outbox SET status = 'delivered', delivered_at = now(),
                    lease_token = NULL, lease_expires_at = NULL
                WHERE id = %s AND status = 'pending' AND lease_token = %s
                """,
                (message.id, message.lease_token),
            )
            return result.rowcount == 1

    def record_outbox_failure(
        self,
        message: OutboxMessage,
        *,
        error: str,
        now: float,
        max_attempts: int,
        retry_base_seconds: float,
        retry_max_seconds: float,
    ) -> bool:
        if not message.lease_token:
            return False
        with self._database.connection() as connection:
            row = connection.execute(
                """
                SELECT status, lease_token, attempt_count FROM outbox
                WHERE id = %s FOR UPDATE
                """,
                (message.id,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "pending"
                or row["lease_token"] != message.lease_token
            ):
                return False
            attempts = int(row["attempt_count"]) + 1
            dead = attempts >= max_attempts
            delay = min(retry_max_seconds, retry_base_seconds * (2 ** (attempts - 1)))
            result = connection.execute(
                """
                UPDATE outbox SET status = %s, attempt_count = %s, next_attempt_at = %s,
                    last_error = %s, lease_token = NULL, lease_expires_at = NULL
                WHERE id = %s AND status = 'pending' AND lease_token = %s
                """,
                (
                    "dead" if dead else "pending",
                    attempts,
                    now + delay,
                    error[:2000],
                    message.id,
                    message.lease_token,
                ),
            )
            return result.rowcount == 1

    def correlate_alert(
        self,
        alert: Alert,
        candidate: Incident,
        *,
        merge_window_minutes: int,
    ) -> CorrelationResult:
        """Serialize a correlation key and attach a provider event exactly once."""
        source = alert.source or "generic"
        event_id = alert.provider_event_id or alert.id
        key = alert.correlation_key or (
            f"{alert.service}|{alert.metric or ''}|{alert.environment or ''}"
        )
        with self._database.connection() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (key,),
            )
            membership = connection.execute(
                """
                SELECT incident_id FROM incident_alerts
                WHERE source = %s AND provider_event_id = %s
                """,
                (source, event_id),
            ).fetchone()
            if membership is not None:
                incident = self._get_with_conn(connection, membership["incident_id"])
                if incident is None:
                    raise RuntimeError("Alert membership references a missing incident")
                return CorrelationResult(incident=incident, created=False, duplicate=True)

            correlation = connection.execute(
                """
                SELECT incident_id, last_alert_at FROM incident_correlations
                WHERE correlation_key = %s
                FOR UPDATE
                """,
                (key,),
            ).fetchone()
            incident = (
                self._get_with_conn(connection, correlation["incident_id"], for_update=True)
                if correlation is not None
                else None
            )
            last_alert_at = correlation["last_alert_at"] if correlation is not None else None
            within_window = bool(
                incident
                and isinstance(last_alert_at, datetime)
                and abs(alert.triggered_at - last_alert_at)
                <= timedelta(minutes=merge_window_minutes)
            )
            if (
                incident is not None
                and incident.status != IncidentStatus.RESOLVED
                and within_window
            ):
                attached_at = datetime.now(alert.triggered_at.tzinfo)
                incident = incident.model_copy(
                    update={
                        "related_alerts": incident.related_alerts + [alert],
                        "timeline": incident.timeline
                        + [
                            {
                                "timestamp": attached_at.isoformat(),
                                "event": (
                                    f"Duplicate/correlated {source} alert attached: {alert.title} "
                                    f"(event_id={event_id}, service={alert.service})"
                                ),
                            }
                        ],
                    }
                )
                created = False
            else:
                incident = candidate
                created = True

            self._save_with_conn(connection, incident)
            connection.execute(
                """
                INSERT INTO incident_correlations
                    (correlation_key, incident_id, last_alert_at)
                VALUES (%s, %s, %s)
                ON CONFLICT(correlation_key) DO UPDATE SET
                    incident_id = excluded.incident_id,
                    last_alert_at = excluded.last_alert_at
                """,
                (key, incident.id, alert.triggered_at),
            )
            connection.execute(
                """
                INSERT INTO incident_alerts
                    (source, provider_event_id, incident_id, correlation_key, triggered_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (source, event_id, incident.id, key, alert.triggered_at),
            )
            return CorrelationResult(incident=incident, created=created, duplicate=False)

    def decide_remediation(
        self,
        incident_id: str,
        *,
        decision: RemediationStatus,
        decided_at: datetime,
        decided_by: str,
        note: str,
    ) -> Incident:
        if decision not in (RemediationStatus.APPROVED, RemediationStatus.REJECTED):
            raise ValueError(f"Invalid remediation decision: {decision.value}")

        with self._database.connection() as connection:
            incident = self._get_with_conn(connection, incident_id, for_update=True)
            if incident is None:
                raise LookupError(f"Unknown incident: {incident_id}")
            if incident.status == IncidentStatus.RESOLVED:
                raise ValueError("Cannot decide remediation for a resolved incident")
            remediation = incident.remediation
            if remediation is None:
                raise ValueError("Incident has no remediation awaiting approval")
            if remediation.status != RemediationStatus.PENDING:
                raise ValueError(f"Remediation already decided: {remediation.status.value}")

            decided = remediation.model_copy(
                update={
                    "status": decision,
                    "decided_at": decided_at,
                    "decided_by": decided_by,
                    "note": note,
                }
            )
            action = "approved" if decision == RemediationStatus.APPROVED else "rejected"
            event = f"Remediation {action} by {decided_by}."
            if note:
                event += f" {note}"
            incident = incident.model_copy(
                update={
                    "remediation": decided,
                    "timeline": incident.timeline
                    + [{"timestamp": decided_at.isoformat(), "event": event}],
                }
            )
            self._save_with_conn(connection, incident)
            return incident

    def finish_remediation(
        self,
        incident_id: str,
        *,
        status: RemediationStatus,
        summary: str,
        finished_at: datetime,
    ) -> Incident:
        if status not in (RemediationStatus.COMPLETED, RemediationStatus.FAILED):
            raise ValueError(f"Invalid remediation completion: {status.value}")
        with self._database.connection() as connection:
            incident = self._get_with_conn(connection, incident_id, for_update=True)
            if incident is None:
                raise LookupError(f"Unknown incident: {incident_id}")
            remediation = incident.remediation
            if remediation is None:
                raise ValueError("Incident has no remediation")
            if remediation.status != RemediationStatus.APPROVED:
                raise ValueError(f"Remediation is not executing: {remediation.status.value}")
            finished = remediation.model_copy(
                update={"status": status, "execution_summary": summary}
            )
            incident = incident.model_copy(
                update={
                    "remediation": finished,
                    "timeline": incident.timeline
                    + [{"timestamp": finished_at.isoformat(), "event": summary}],
                }
            )
            self._save_with_conn(connection, incident)
            return incident

    def list_open(self) -> list[Incident]:
        with self._database.connection() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM incidents
                WHERE status != %s
                ORDER BY created_at DESC
                """,
                (IncidentStatus.RESOLVED.value,),
            ).fetchall()
        return [Incident.model_validate(_model_payload(row["payload"])) for row in rows]

    def list_recent(
        self,
        limit: int = 50,
        status: IncidentStatus | None = None,
    ) -> list[Incident]:
        with self._database.connection() as connection:
            if status is None:
                rows = connection.execute(
                    "SELECT payload FROM incidents ORDER BY created_at DESC LIMIT %s",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT payload FROM incidents
                    WHERE status = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (status.value, limit),
                ).fetchall()
        return [Incident.model_validate(_model_payload(row["payload"])) for row in rows]


class PostgresAlertStore:
    def __init__(self, database: PostgresDatabaseLike) -> None:
        self._database = database

    def enqueue(self, alert: Alert) -> bool:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO alert_queue (alert_id, payload)
                VALUES (%s, %s::jsonb)
                ON CONFLICT(alert_id) DO NOTHING
                RETURNING alert_id
                """,
                (alert.id, alert.model_dump_json()),
            ).fetchone()
        return row is not None

    def claim(
        self,
        alert_id: str,
        *,
        now: float,
        lease_seconds: float,
    ) -> _ClaimedAlert | None:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                SELECT alert_id, payload FROM alert_queue
                WHERE alert_id = %s
                  AND next_attempt_at <= %s
                  AND (lease_token IS NULL OR lease_expires_at <= %s)
                FOR UPDATE SKIP LOCKED
                """,
                (alert_id, now, now),
            ).fetchone()
            return self._finish_claim(connection, row, now, lease_seconds)

    def claim_next(self, *, now: float, lease_seconds: float) -> _ClaimedAlert | None:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                SELECT alert_id, payload FROM alert_queue
                WHERE next_attempt_at <= %s
                  AND (lease_token IS NULL OR lease_expires_at <= %s)
                ORDER BY created_at ASC, alert_id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                (now, now),
            ).fetchone()
            return self._finish_claim(connection, row, now, lease_seconds)

    @staticmethod
    def _finish_claim(
        connection: PostgresConnection,
        row: object,
        now: float,
        lease_seconds: float,
    ) -> _ClaimedAlert | None:
        if row is None:
            return None
        lease_token = uuid4().hex
        connection.execute(
            """
            UPDATE alert_queue SET lease_token = %s, lease_expires_at = %s
            WHERE alert_id = %s
            """,
            (lease_token, now + lease_seconds, row["alert_id"]),
        )
        return _ClaimedAlert(
            alert=Alert.model_validate(_model_payload(row["payload"])),
            lease_token=lease_token,
        )

    def delete(self, alert_id: str, *, lease_token: str) -> bool:
        with self._database.connection() as connection:
            result = connection.execute(
                "DELETE FROM alert_queue WHERE alert_id = %s AND lease_token = %s",
                (alert_id, lease_token),
            )
        return result.rowcount == 1

    def renew_lease(
        self,
        alert_id: str,
        *,
        lease_token: str,
        now: float,
        lease_seconds: float,
    ) -> bool:
        with self._database.connection() as connection:
            result = connection.execute(
                """
                UPDATE alert_queue SET lease_expires_at = %s
                WHERE alert_id = %s AND lease_token = %s
                """,
                (now + lease_seconds, alert_id, lease_token),
            )
        return result.rowcount == 1

    def record_failure(
        self,
        alert_id: str,
        *,
        lease_token: str,
        error: str,
        base_seconds: float,
        max_seconds: float,
        max_attempts: int,
    ) -> bool:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                SELECT payload, attempt_count FROM alert_queue
                WHERE alert_id = %s AND lease_token = %s
                FOR UPDATE
                """,
                (alert_id, lease_token),
            ).fetchone()
            if row is None:
                return False
            attempt_count = int(row["attempt_count"]) + 1
            truncated_error = error[:500]
            if attempt_count >= max_attempts:
                connection.execute(
                    """
                    INSERT INTO alert_dead_letters
                        (alert_id, payload, attempt_count, last_error, failed_at)
                    VALUES (%s, %s::jsonb, %s, %s, now())
                    ON CONFLICT(alert_id) DO UPDATE SET
                        payload = excluded.payload,
                        attempt_count = excluded.attempt_count,
                        last_error = excluded.last_error,
                        failed_at = excluded.failed_at
                    """,
                    (
                        alert_id,
                        json.dumps(_model_payload(row["payload"])),
                        attempt_count,
                        truncated_error,
                    ),
                )
                connection.execute(
                    "DELETE FROM alert_queue WHERE alert_id = %s AND lease_token = %s",
                    (alert_id, lease_token),
                )
                return True
            delay = _retry_delay(
                attempt_count,
                base_seconds=base_seconds,
                max_seconds=max_seconds,
            )
            connection.execute(
                """
                UPDATE alert_queue SET
                    attempt_count = %s,
                    next_attempt_at = %s,
                    last_error = %s,
                    lease_token = NULL,
                    lease_expires_at = NULL
                WHERE alert_id = %s AND lease_token = %s
                """,
                (
                    attempt_count,
                    time.time() + delay,
                    truncated_error,
                    alert_id,
                    lease_token,
                ),
            )
            return False

    def count(self) -> int:
        with self._database.connection() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM alert_queue").fetchone()
        return int(row["count"])

    def list_dead_letters(self, limit: int) -> list[DeadLetter]:
        with self._database.connection() as connection:
            rows = connection.execute(
                """
                SELECT payload, attempt_count, last_error, failed_at
                FROM alert_dead_letters
                ORDER BY failed_at DESC, alert_id ASC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [
            DeadLetter(
                alert=Alert.model_validate(_model_payload(row["payload"])),
                attempt_count=int(row["attempt_count"]),
                last_error=str(row["last_error"]),
                failed_at=row["failed_at"],
            )
            for row in rows
        ]

    def replay_dead_letter(self, alert_id: str) -> Alert:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                SELECT payload FROM alert_dead_letters
                WHERE alert_id = %s
                FOR UPDATE
                """,
                (alert_id,),
            ).fetchone()
            if row is None:
                raise DeadLetterNotFoundError(alert_id)
            inserted = connection.execute(
                """
                INSERT INTO alert_queue
                    (alert_id, payload, attempt_count, next_attempt_at, last_error)
                VALUES (%s, %s::jsonb, 0, 0, NULL)
                ON CONFLICT(alert_id) DO NOTHING
                RETURNING alert_id
                """,
                (alert_id, json.dumps(_model_payload(row["payload"]))),
            ).fetchone()
            if inserted is None:
                raise AlertAlreadyQueuedError(alert_id)
            connection.execute(
                "DELETE FROM alert_dead_letters WHERE alert_id = %s",
                (alert_id,),
            )
        return Alert.model_validate(_model_payload(row["payload"]))
