"""Local-first operator console. Server-rendered HTML over the same domain models
the API exposes — no template engine, no frontend build. See PLAN.md for scope.

This console is not production-authenticated. It is intended for localhost use.
"""

from datetime import datetime, timezone
from html import escape
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from .config import Settings
from .models import Incident, IncidentStatus
from .orchestrator import IncidentOrchestrator
from .queue import AlertQueue

STATIC_DIR = Path(__file__).parent / "static"

CONSOLE_LIMIT = 50

_OPEN_STATUSES = (
    IncidentStatus.OPEN,
    IncidentStatus.INVESTIGATING,
    IncidentStatus.MITIGATED,
)

_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400

_EMPTY_BODY = (
    "Trigger the demo incident to watch triage, runbook matching, "
    "impact estimation, and post-mortem generation end to end."
)


def _format_age(created_at: datetime, now: datetime) -> str:
    moment = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    seconds = max((now - moment).total_seconds(), 0)
    if seconds < _SECONDS_PER_MINUTE:
        return f"{int(seconds)}s"
    if seconds < _SECONDS_PER_HOUR:
        return f"{int(seconds // _SECONDS_PER_MINUTE)}m"
    if seconds < _SECONDS_PER_DAY:
        return f"{int(seconds // _SECONDS_PER_HOUR)}h"
    return f"{int(seconds // _SECONDS_PER_DAY)}d"


def _format_value(incident: Incident) -> str:
    alert = incident.alert
    if alert.value is None:
        return "—"
    if alert.metric is None:
        return f"{alert.value:g}"
    return f"{escape(alert.metric)} {alert.value:g}"


def _format_runbook(incident: Incident) -> str:
    # Plain text until /console/runbooks/{slug} exists — see PLAN.md Phase 3.
    if incident.triage is None or incident.triage.runbook is None:
        return "—"
    return escape(incident.triage.runbook.runbook.slug)


def _format_confidence(incident: Incident) -> str:
    if incident.triage is None or not incident.triage.suspects:
        return "—"
    top = max(suspect.confidence for suspect in incident.triage.suspects)
    return f"{top:.0%}"


def _render_row(incident: Incident, now: datetime) -> str:
    severity = incident.alert.severity.value
    status = incident.status.value
    return (
        "<tr>"
        f'<td><span class="sev sev-{escape(severity)}">{escape(severity)}</span></td>'
        f"<td>{escape(incident.alert.service)}</td>"
        f'<td class="title">'
        f'<a href="/console/incidents/{escape(incident.id)}">'
        f"{escape(incident.alert.title)}</a></td>"
        f'<td><span class="pill pill-{escape(status)}">{escape(status)}</span></td>'
        f"<td>{_format_age(incident.created_at, now)}</td>"
        f"<td>{_format_value(incident)}</td>"
        f"<td>{_format_runbook(incident)}</td>"
        f"<td>{_format_confidence(incident)}</td>"
        "</tr>"
    )


def _render_table(incidents: list[Incident], now: datetime) -> str:
    rows = "".join(_render_row(incident, now) for incident in incidents)
    return (
        '<table class="incidents">'
        "<thead><tr>"
        "<th>Sev</th><th>Service</th><th>Title</th><th>Status</th>"
        "<th>Age</th><th>Metric</th><th>Runbook</th><th>Top suspect</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
    )


def _render_demo_button() -> str:
    return (
        '<form method="post" action="/console/demo-alert">'
        '<button type="submit" class="primary">Trigger demo incident</button>'
        "</form>"
    )


def _render_empty_state() -> str:
    return (
        '<section class="empty">'
        "<h2>No active incidents</h2>"
        f"<p>{escape(_EMPTY_BODY)}</p>"
        f"{_render_demo_button()}"
        "</section>"
    )


def _render_environment(settings: Settings) -> str:
    modes = (
        f"llm {settings.llm_mode}",
        f"github {settings.github_mode}",
        f"slack {settings.slack_mode}",
        f"metrics {settings.metrics_mode}",
    )
    tags = "".join(f'<span class="mode">{escape(mode)}</span>' for mode in modes)
    return f'<div class="modes">{tags}</div>'


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title>"
        '<link rel="stylesheet" href="/static/console.css">'
        f"</head><body>{body}</body></html>"
    )


def _render_console(
    incidents: list[Incident],
    settings: Settings,
    queue_depth: int,
    now: datetime,
) -> str:
    open_incidents = [i for i in incidents if i.status in _OPEN_STATUSES]
    resolved = [i for i in incidents if i.status == IncidentStatus.RESOLVED]

    header = (
        '<header class="topbar">'
        "<h1>Autonomous Incident Response</h1>"
        f"{_render_environment(settings)}"
        f'<div class="queue">Queue depth <strong>{queue_depth}</strong></div>'
        f"{_render_demo_button()}"
        "</header>"
    )

    if not incidents:
        return _page("Incident console", header + f"<main>{_render_empty_state()}</main>")

    sections = []
    if open_incidents:
        sections.append(
            "<section><h2>Open incidents</h2>"
            f"{_render_table(open_incidents, now)}</section>"
        )
    if resolved:
        sections.append(
            "<section><h2>Recently resolved</h2>"
            f"{_render_table(resolved, now)}</section>"
        )
    return _page("Incident console", header + f"<main>{''.join(sections)}</main>")


def register_console(
    app: FastAPI,
    *,
    orchestrator: IncidentOrchestrator,
    queue: AlertQueue,
    settings: Settings,
) -> None:
    """Mount console routes onto an existing app. Call after API routes."""

    @app.get("/console", response_class=HTMLResponse)
    async def console() -> HTMLResponse:
        incidents = orchestrator.store.list_recent(limit=CONSOLE_LIMIT)
        html = _render_console(
            incidents,
            settings=settings,
            queue_depth=queue.qsize(),
            now=datetime.now(timezone.utc),
        )
        return HTMLResponse(content=html)
