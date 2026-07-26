"""SQLite persistence for incidents. Deliberately tiny — a single JSON blob per row."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .models import Incident, IncidentStatus, RemediationStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS incidents_status_idx ON incidents(status);
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
