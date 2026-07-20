"""Local-first operator console. Server-rendered HTML over the same domain models
the API exposes — no template engine, no frontend build. See PLAN.md for scope.

This console is not production-authenticated. It is intended for localhost use.
"""

import asyncio
import logging
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .config import Settings
from .models import Alert, Incident, IncidentStatus, Severity
from .orchestrator import IncidentOrchestrator
from .queue import AlertQueue

STATIC_DIR = Path(__file__).parent / "static"

logger = logging.getLogger(__name__)

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

_DEMO_PERSIST_TIMEOUT_SECONDS = 1.0


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


def _demo_enabled(settings: Settings) -> bool:
    return all(
        mode == "mock"
        for mode in (
            settings.llm_mode,
            settings.github_mode,
            settings.slack_mode,
            settings.metrics_mode,
            settings.remediation_mode,
        )
    )


def _render_demo_button(settings: Settings) -> str:
    if not _demo_enabled(settings):
        return ""
    return (
        '<form method="post" action="/console/demo-alert">'
        '<button type="submit" class="primary">Trigger demo incident</button>'
        "</form>"
    )


def _render_empty_state(settings: Settings) -> str:
    if not _demo_enabled(settings):
        return (
            '<section class="empty">'
            "<h2>No active incidents</h2>"
            "<p>Send an authenticated alert to populate the console.</p>"
            "</section>"
        )
    return (
        '<section class="empty">'
        "<h2>No active incidents</h2>"
        f"<p>{escape(_EMPTY_BODY)}</p>"
        f"{_render_demo_button(settings)}"
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
        f"{_render_demo_button(settings)}"
        "</header>"
    )

    if not incidents:
        return _page(
            "Incident console", header + f"<main>{_render_empty_state(settings)}</main>"
        )

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


def _detail_header(incident: Incident) -> str:
    severity = incident.alert.severity.value
    status = incident.status.value
    return (
        '<header class="detail-header">'
        '<a class="back" href="/console">← All incidents</a>'
        '<div class="detail-heading">'
        f'<span class="sev sev-{escape(severity)}">{escape(severity)}</span>'
        f'<span class="pill pill-{escape(status)}">{escape(status)}</span>'
        f"<h1>{escape(incident.alert.title)}</h1>"
        f'<span class="incident-id">{escape(incident.id)}</span>'
        "</div></header>"
    )


def _definition_list(items: list[tuple[str, str]]) -> str:
    rows = "".join(
        f"<div><dt>{escape(label)}</dt><dd>{value}</dd></div>" for label, value in items
    )
    return f'<dl class="facts">{rows}</dl>'


def _render_alert_detail(incident: Incident) -> str:
    alert = incident.alert
    tags = "—"
    if alert.tags:
        tags = " ".join(
            f'<span class="tag">{escape(key)}={escape(value)}</span>'
            for key, value in sorted(alert.tags.items())
        )
    items = [
        ("Service", escape(alert.service)),
        ("Triggered", escape(alert.triggered_at.isoformat())),
        ("Metric", escape(alert.metric) if alert.metric else "—"),
        ("Current value", f"{alert.value:g}" if alert.value is not None else "—"),
        ("Threshold", f"{alert.threshold:g}" if alert.threshold is not None else "—"),
        ("Tags", tags),
    ]
    description = escape(alert.description) if alert.description else "No description provided."
    return (
        '<section class="detail-section"><h2>Alert</h2>'
        f'<p class="summary">{description}</p>{_definition_list(items)}</section>'
    )


def _render_suspects(incident: Incident) -> str:
    triage = incident.triage
    if triage is None:
        return '<p class="muted">Triage in progress</p>'
    if not triage.suspects:
        return '<p class="muted">No suspect commits identified.</p>'

    cards = []
    for suspect in triage.suspects:
        commit = suspect.commit
        pr = f" · PR #{commit.pr_number}" if commit.pr_number is not None else ""
        files = ", ".join(escape(path) for path in commit.files_changed) or "—"
        cards.append(
            '<article class="suspect">'
            f'<div><code>{escape(commit.sha)}</code>'
            f'<strong>{suspect.confidence:.0%}</strong></div>'
            f'<p>{escape(commit.message)}</p>'
            f'<p class="muted">{escape(commit.author)}{pr} · {escape(commit.timestamp.isoformat())}</p>'
            f'<p>{escape(suspect.reasoning)}</p>'
            f'<p class="muted">Files: {files}</p>'
            "</article>"
        )
    return "".join(cards)


def _render_triage_detail(incident: Incident) -> str:
    triage = incident.triage
    if triage is None:
        return (
            '<section class="detail-section"><h2>Triage</h2>'
            '<p class="muted">Triage in progress</p></section>'
        )

    impact = triage.impact
    impact_items = [
        ("Affected users", f"{impact.affected_users:,}"),
        ("Affected percent", f"{impact.affected_percent:g}%"),
        ("Error rate", f"{impact.error_rate:g}"),
        ("Window", f"{impact.time_window_minutes} minutes"),
    ]
    if triage.runbook is None:
        runbook = '<p class="muted">No matching runbook.</p>'
    else:
        match = triage.runbook
        runbook = (
            '<article class="runbook">'
            f"<h3>{escape(match.runbook.title)}</h3>"
            f'<p><code>{escape(match.runbook.slug)}</code> · {match.confidence:.0%} match</p>'
            f"<p>{escape(match.reasoning)}</p>"
            "</article>"
        )
    return (
        '<section class="detail-section"><h2>Triage</h2>'
        f'<p class="summary">{escape(triage.summary)}</p>'
        '<div class="detail-grid">'
        f'<div><h3>Impact</h3>{_definition_list(impact_items)}'
        f'<p class="muted">{escape(impact.reasoning)}</p></div>'
        f"<div><h3>Matched runbook</h3>{runbook}</div>"
        "</div>"
        f'<h3>Suspect commits</h3><div class="suspects">{_render_suspects(incident)}</div>'
        "</section>"
    )


def _render_timeline(incident: Incident) -> str:
    if not incident.timeline:
        events = '<p class="muted">No timeline events recorded.</p>'
    else:
        events = "<ol class=\"timeline\">" + "".join(
            "<li>"
            f'<time>{escape(str(item.get("timestamp", "?")))}</time>'
            f'<p>{escape(str(item.get("event", "")))}</p>'
            "</li>"
            for item in incident.timeline
        ) + "</ol>"
    return f'<section class="detail-section"><h2>Timeline</h2>{events}</section>'


def _render_resolution(incident: Incident) -> str:
    outcome = incident.verification_outcome
    if incident.status != IncidentStatus.RESOLVED and outcome is None:
        return ""

    items: list[tuple[str, str]] = []
    if incident.resolved_at is not None:
        items.append(("Resolved", escape(incident.resolved_at.isoformat())))
    if outcome is not None:
        items.extend(
            [
                ("Verification", escape(outcome.status)),
                ("Baseline peak", f"{outcome.baseline_peak:g}"),
                ("Final peak", f"{outcome.final_peak:g}"),
                ("Elapsed", f"{outcome.minutes_elapsed:g} minutes"),
                ("Result", escape(outcome.message)),
            ]
        )
    if incident.postmortem_path:
        items.append(("Post-mortem", f"<code>{escape(incident.postmortem_path)}</code>"))
    return (
        '<section class="detail-section"><h2>Resolution</h2>'
        f"{_definition_list(items)}</section>"
    )


def _render_incident_detail(incident: Incident) -> str:
    body = (
        _detail_header(incident)
        + '<main class="detail-main">'
        + _render_alert_detail(incident)
        + _render_triage_detail(incident)
        + _render_timeline(incident)
        + _render_resolution(incident)
        + "</main>"
    )
    return _page(f"{incident.id} · Incident console", body)


def _render_not_found(incident_id: str) -> str:
    body = (
        '<main class="not-found"><h1>Incident not found</h1>'
        f'<p>No incident exists with ID <code>{escape(incident_id)}</code>.</p>'
        '<a href="/console">← All incidents</a></main>'
    )
    return _page("Incident not found", body)


def _render_console_error(title: str, message: str) -> str:
    body = (
        f'<main class="not-found"><h1>{escape(title)}</h1>'
        f"<p>{escape(message)}</p>"
        '<a href="/console">← All incidents</a></main>'
    )
    return _page(title, body)


def _build_demo_alert() -> Alert:
    token = uuid4().hex
    return Alert(
        id=f"demo-checkout-{token}",
        title="Checkout 5xx > 5%",
        description="checkout service error rate at 18%",
        service="checkout",
        severity=Severity.SEV2,
        triggered_at=datetime.now(timezone.utc),
        metric=f"http.error_rate.demo-{token}",
        threshold=0.05,
        value=0.184,
        tags={"env": "demo", "source": "console"},
    )


def _is_cross_site(request: Request) -> bool:
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        return True
    origin = request.headers.get("origin")
    if not origin:
        return False
    parsed = urlsplit(origin)
    return (parsed.scheme, parsed.netloc) != (request.url.scheme, request.url.netloc)


async def _wait_for_incident(orchestrator: IncidentOrchestrator, incident_id: str) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _DEMO_PERSIST_TIMEOUT_SECONDS
    while loop.time() < deadline:
        if orchestrator.store.get(incident_id) is not None:
            return True
        await asyncio.sleep(0.01)
    return False


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

    @app.get("/console/incidents/{incident_id}", response_class=HTMLResponse)
    async def console_incident(incident_id: str) -> HTMLResponse:
        incident = orchestrator.store.get(incident_id)
        if incident is None:
            return HTMLResponse(content=_render_not_found(incident_id), status_code=404)
        return HTMLResponse(content=_render_incident_detail(incident))

    @app.post("/console/demo-alert")
    async def console_demo_alert(request: Request) -> HTMLResponse:
        if not _demo_enabled(settings):
            return HTMLResponse(
                content=_render_console_error(
                    "Demo mode unavailable",
                    "The console demo is available only when every integration and "
                    "remediation mode is set to mock.",
                ),
                status_code=403,
            )
        if _is_cross_site(request):
            return HTMLResponse(
                content=_render_console_error(
                    "Cross-site request rejected",
                    "Open the local console directly before triggering a demo incident.",
                ),
                status_code=403,
            )

        alert = _build_demo_alert()
        incident_id = f"inc-{alert.id}"
        try:
            await queue.submit(alert)
        except Exception:
            logger.exception("console_demo_alert_enqueue_failed")
            return HTMLResponse(
                content=_render_console_error(
                    "Could not queue demo incident",
                    "The demo incident was not accepted. Check the server logs and try again.",
                ),
                status_code=503,
            )

        if not await _wait_for_incident(orchestrator, incident_id):
            logger.warning(
                "console_demo_alert_persist_timeout", extra={"incident_id": incident_id}
            )
            return HTMLResponse(
                content=_render_console_error(
                    "Demo incident is still queued",
                    "Return to the incident list and reload to see it when processing starts.",
                ),
                status_code=202,
            )
        return RedirectResponse(url=f"/console/incidents/{incident_id}", status_code=303)
