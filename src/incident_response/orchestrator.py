"""Incident orchestrator: runs the full autonomous response flow.

The three triage agents run in parallel (independent I/O) and the results are stitched
into a Slack brief. On resolve, we call the post-mortem generator, save the markdown,
and post a thread reply.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .agents.brief import compose_slack_brief
from .agents.impact import estimate_impact
from .agents.llm import LLM
from .agents.postmortem import generate_postmortem
from .agents.runbook import match_runbook
from .agents.triage import identify_suspects
from .db import IncidentStore
from .dedup import DedupIndex, alert_fingerprint
from .executor import (
    MockExecutor,
    RemediationExecutor,
    format_results_for_slack,
    parse_steps,
)
from .integrations.github import GitHubClient
from .integrations.metrics import MetricsClient
from .integrations.slack import SlackClient
from .logging_config import set_incident_id
from .models import Alert, Incident, IncidentStatus, Runbook, TriageReport
from .runbooks_loader import load_runbooks

logger = logging.getLogger(__name__)


_LOOKBACK_MINUTES = 90
_IMPACT_WINDOW_MINUTES = 15


@dataclass
class OrchestratorConfig:
    slack_channel: str
    runbooks_dir: Path
    postmortem_dir: Path
    dedup_bucket_minutes: int = 15


class IncidentOrchestrator:
    def __init__(
        self,
        *,
        llm: LLM,
        github: GitHubClient,
        slack: SlackClient,
        metrics: MetricsClient,
        store: IncidentStore,
        config: OrchestratorConfig,
        dedup: DedupIndex | None = None,
        executor: RemediationExecutor | None = None,
    ) -> None:
        self._llm = llm
        self._github = github
        self._slack = slack
        self._metrics = metrics
        self._store = store
        self._config = config
        self._dedup = dedup or DedupIndex()
        self._executor = executor or MockExecutor()
        self._runbooks: list[Runbook] = load_runbooks(config.runbooks_dir)

    async def handle_alert(self, alert: Alert) -> Incident:
        now = datetime.now(timezone.utc)
        fingerprint = alert_fingerprint(alert, self._config.dedup_bucket_minutes)

        existing_id = self._dedup.get(fingerprint)
        if existing_id:
            existing = self._store.get(existing_id)
            if existing and existing.status != IncidentStatus.RESOLVED:
                set_incident_id(existing_id)
                logger.info(
                    "alert_deduped",
                    extra={"fingerprint": fingerprint, "incident_id": existing_id},
                )
                updated = existing.model_copy(
                    update={
                        "timeline": existing.timeline
                        + [
                            {
                                "timestamp": now.isoformat(),
                                "event": f"Duplicate alert attached: {alert.title} "
                                f"(alert_id={alert.id})",
                            }
                        ]
                    }
                )
                self._store.save(updated)
                return updated

        incident_id = f"inc-{alert.id}"
        set_incident_id(incident_id)
        incident = Incident(
            id=incident_id,
            alert=alert,
            status=IncidentStatus.INVESTIGATING,
            created_at=now,
            timeline=[{"timestamp": now.isoformat(), "event": f"Alert fired: {alert.title}"}],
        )
        self._store.save(incident)
        self._dedup.set(fingerprint, incident.id)
        logger.info("incident_opened", extra={"service": alert.service, "severity": alert.severity.value})

        triage = await self._run_triage(alert)
        incident = incident.model_copy(
            update={
                "triage": triage,
                "timeline": incident.timeline
                + [
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "event": f"Triage complete: {len(triage.suspects)} suspect(s), "
                        f"runbook={'yes' if triage.runbook else 'no'}, "
                        f"impact={triage.impact.affected_users} users",
                    }
                ],
            }
        )
        self._store.save(incident)

        brief = compose_slack_brief(incident, triage)
        posted = await self._slack.post(channel=self._config.slack_channel, text=brief)
        incident = incident.model_copy(
            update={
                "slack_message_ts": posted.ts,
                "timeline": incident.timeline
                + [
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "event": f"Slack brief posted to {posted.channel} (ts={posted.ts})",
                    }
                ],
            }
        )
        self._store.save(incident)

        if triage.runbook:
            remediation_summary = await self._run_remediation(triage.runbook.runbook, posted.ts)
            if remediation_summary:
                incident = incident.model_copy(
                    update={
                        "timeline": incident.timeline
                        + [
                            {
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "event": remediation_summary,
                            }
                        ]
                    }
                )
                self._store.save(incident)
        return incident

    async def _run_remediation(self, runbook: Runbook, thread_ts: str) -> str:
        steps = parse_steps(runbook)
        if not steps:
            return ""
        results = await self._executor.run(steps)
        text = format_results_for_slack(results)
        if text:
            await self._slack.post(
                channel=self._config.slack_channel, text=text, thread_ts=thread_ts
            )
        summary = ", ".join(f"{r.step.name}={r.status}" for r in results)
        return f"Remediation attempted: {summary}"

    async def _run_triage(self, alert: Alert) -> TriageReport:
        since = alert.triggered_at - timedelta(minutes=_LOOKBACK_MINUTES)

        commits_task = self._github.recent_commits(alert.service, since=since, limit=20)
        error_task = self._metrics.error_rate(alert.service, minutes=_IMPACT_WINDOW_MINUTES)
        rps_task = self._metrics.request_rate(alert.service, minutes=_IMPACT_WINDOW_MINUTES)
        active_task = self._metrics.active_users(alert.service)

        commits, error_series, rps_series, active = await asyncio.gather(
            commits_task, error_task, rps_task, active_task
        )

        suspects_task = identify_suspects(self._llm, alert, commits)
        runbook_task = match_runbook(self._llm, alert, self._runbooks)
        impact_task = estimate_impact(
            self._llm, error_series, rps_series, active, _IMPACT_WINDOW_MINUTES
        )

        suspects, runbook, impact = await asyncio.gather(
            suspects_task, runbook_task, impact_task
        )

        summary = self._summarize(alert, suspects, runbook, impact)
        return TriageReport(suspects=suspects, runbook=runbook, impact=impact, summary=summary)

    def _summarize(self, alert, suspects, runbook, impact) -> str:
        if suspects:
            top = suspects[0]
            suspect_str = (
                f"top suspect `{top.commit.sha[:8]}` by {top.commit.author} "
                f"({top.confidence:.0%})"
            )
        else:
            suspect_str = "no obvious suspect commit"
        runbook_str = (
            f"runbook `{runbook.runbook.slug}`" if runbook else "no matching runbook"
        )
        return (
            f"Alert on `{alert.service}` — {impact.affected_users} users at risk, "
            f"{suspect_str}, {runbook_str}."
        )

    async def resolve(self, incident_id: str, resolution_note: str = "") -> Incident:
        set_incident_id(incident_id)
        incident = self._store.get(incident_id)
        if incident is None:
            raise LookupError(f"Unknown incident: {incident_id}")

        now = datetime.now(timezone.utc)
        incident = incident.model_copy(
            update={
                "status": IncidentStatus.RESOLVED,
                "resolved_at": now,
                "timeline": incident.timeline
                + [
                    {
                        "timestamp": now.isoformat(),
                        "event": f"Resolved. {resolution_note}".strip(),
                    }
                ],
            }
        )

        markdown = await generate_postmortem(self._llm, incident)
        pm_path = self._write_postmortem(incident, markdown)
        incident = incident.model_copy(update={"postmortem_path": str(pm_path)})
        self._store.save(incident)

        if incident.slack_message_ts:
            await self._slack.post(
                channel=self._config.slack_channel,
                text=f":memo: Post-mortem generated: `{pm_path}` — "
                f"resolution: {resolution_note or 'n/a'}",
                thread_ts=incident.slack_message_ts,
            )
        return incident

    def _write_postmortem(self, incident: Incident, markdown: str) -> Path:
        self._config.postmortem_dir.mkdir(parents=True, exist_ok=True)
        date_slug = incident.created_at.strftime("%Y-%m-%d")
        filename = f"{date_slug}-{incident.id}.md"
        path = self._config.postmortem_dir / filename
        path.write_text(markdown, encoding="utf-8")
        return path
