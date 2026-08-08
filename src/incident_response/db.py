"""SQLite persistence for incidents. Deliberately tiny — a single JSON blob per row."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from .models import Alert, CorrelationResult, Incident, IncidentStatus, RemediationStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS incidents_status_idx ON incidents(status);

CREATE TABLE IF NOT EXISTS incident_alerts (
    source TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    incident_id TEXT NOT NULL,
    correlation_key TEXT NOT NULL,
    triggered_at TEXT NOT NULL,
    PRIMARY KEY (source, provider_event_id)
);
CREATE INDEX IF NOT EXISTS incident_alerts_incident_idx ON incident_alerts(incident_id);

CREATE TABLE IF NOT EXISTS incident_correlations (
    correlation_key TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    last_alert_at TEXT NOT NULL
);
"""


class IncidentStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _save_with_conn(conn: sqlite3.Connection, incident: Incident) -> None:
        conn.execute(
            """
            INSERT INTO incidents (id, status, payload, created_at, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                payload = excluded.payload,
                updated_at = datetime('now')
            """,
            (
                incident.id,
                incident.status.value,
                incident.model_dump_json(),
                incident.created_at.isoformat(),
            ),
        )

    @staticmethod
    def _get_with_conn(
        conn: sqlite3.Connection,
        incident_id: str,
    ) -> Incident | None:
        row = conn.execute(
            "SELECT payload FROM incidents WHERE id = ?",
            (incident_id,),
        ).fetchone()
        if row is None:
            return None
        return Incident.model_validate(json.loads(row["payload"]))

    def save(self, incident: Incident) -> None:
        with self._conn() as conn:
            self._save_with_conn(conn, incident)

    def get(self, incident_id: str) -> Incident | None:
        with self._conn() as conn:
            return self._get_with_conn(conn, incident_id)

    def correlate_alert(
        self,
        alert: Alert,
        candidate: Incident,
        *,
        merge_window_minutes: int,
    ) -> CorrelationResult:
        """Atomically create, attach, or return an idempotent provider retry."""
        source = alert.source or "generic"
        event_id = alert.provider_event_id or alert.id
        key = alert.correlation_key or (
            f"{alert.service}|{alert.metric or ''}|{alert.environment or ''}"
        )
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            membership = conn.execute(
                """
                SELECT incident_id FROM incident_alerts
                WHERE source = ? AND provider_event_id = ?
                """,
                (source, event_id),
            ).fetchone()
            if membership is not None:
                incident = self._get_with_conn(conn, membership["incident_id"])
                if incident is None:
                    raise RuntimeError("Alert membership references a missing incident")
                return CorrelationResult(incident=incident, created=False, duplicate=True)

            correlation = conn.execute(
                """
                SELECT incident_id, last_alert_at FROM incident_correlations
                WHERE correlation_key = ?
                """,
                (key,),
            ).fetchone()
            incident = (
                self._get_with_conn(conn, correlation["incident_id"])
                if correlation is not None
                else None
            )
            last_alert_at = (
                datetime.fromisoformat(correlation["last_alert_at"])
                if correlation is not None
                else None
            )
            within_window = bool(
                incident
                and last_alert_at
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

            self._save_with_conn(conn, incident)
            conn.execute(
                """
                INSERT INTO incident_correlations (correlation_key, incident_id, last_alert_at)
                VALUES (?, ?, ?)
                ON CONFLICT(correlation_key) DO UPDATE SET
                    incident_id = excluded.incident_id,
                    last_alert_at = excluded.last_alert_at
                """,
                (key, incident.id, alert.triggered_at.isoformat()),
            )
            conn.execute(
                """
                INSERT INTO incident_alerts
                    (source, provider_event_id, incident_id, correlation_key, triggered_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (source, event_id, incident.id, key, alert.triggered_at.isoformat()),
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
        """Atomically claim a pending remediation for one final decision."""

        if decision not in (RemediationStatus.APPROVED, RemediationStatus.REJECTED):
            raise ValueError(f"Invalid remediation decision: {decision.value}")

        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            incident = self._get_with_conn(conn, incident_id)
            if incident is None:
                raise LookupError(f"Unknown incident: {incident_id}")
            if incident.status == IncidentStatus.RESOLVED:
                raise ValueError("Cannot decide remediation for a resolved incident")
            remediation = incident.remediation
            if remediation is None:
                raise ValueError("Incident has no remediation awaiting approval")
            if remediation.status != RemediationStatus.PENDING:
                raise ValueError(
                    f"Remediation already decided: {remediation.status.value}"
                )

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
            self._save_with_conn(conn, incident)
            return incident

    def finish_remediation(
        self,
        incident_id: str,
        *,
        status: RemediationStatus,
        summary: str,
        finished_at: datetime,
    ) -> Incident:
        """Update only remediation fields on the latest persisted incident."""

        if status not in (RemediationStatus.COMPLETED, RemediationStatus.FAILED):
            raise ValueError(f"Invalid remediation completion: {status.value}")

        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            incident = self._get_with_conn(conn, incident_id)
            if incident is None:
                raise LookupError(f"Unknown incident: {incident_id}")
            remediation = incident.remediation
            if remediation is None:
                raise ValueError("Incident has no remediation")
            if remediation.status != RemediationStatus.APPROVED:
                raise ValueError(
                    f"Remediation is not executing: {remediation.status.value}"
                )

            finished = remediation.model_copy(
                update={
                    "status": status,
                    "execution_summary": summary,
                }
            )
            incident = incident.model_copy(
                update={
                    "remediation": finished,
                    "timeline": incident.timeline
                    + [{"timestamp": finished_at.isoformat(), "event": summary}],
                }
            )
            self._save_with_conn(conn, incident)
            return incident

    def list_open(self) -> list[Incident]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT payload FROM incidents WHERE status != 'resolved' ORDER BY created_at DESC"
            ).fetchall()
        return [Incident.model_validate(json.loads(r["payload"])) for r in rows]

    def list_recent(
        self, limit: int = 50, status: IncidentStatus | None = None
    ) -> list[Incident]:
        with self._conn() as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT payload FROM incidents ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT payload FROM incidents
                    WHERE status = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (status.value, limit),
                ).fetchall()
        return [Incident.model_validate(json.loads(r["payload"])) for r in rows]
