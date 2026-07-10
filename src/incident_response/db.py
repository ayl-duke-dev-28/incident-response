"""SQLite persistence for incidents. Deliberately tiny — a single JSON blob per row."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import Incident

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

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, incident: Incident) -> None:
        payload = incident.model_dump_json()
        with self._conn() as conn:
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
                    payload,
                    incident.created_at.isoformat(),
                ),
            )

    def get(self, incident_id: str) -> Incident | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
        if row is None:
            return None
        return Incident.model_validate(json.loads(row["payload"]))

    def list_open(self) -> list[Incident]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT payload FROM incidents WHERE status != 'resolved' ORDER BY created_at DESC"
            ).fetchall()
        return [Incident.model_validate(json.loads(r["payload"])) for r in rows]

    def list_recent(self, limit: int = 50) -> list[Incident]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT payload FROM incidents ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [Incident.model_validate(json.loads(r["payload"])) for r in rows]
