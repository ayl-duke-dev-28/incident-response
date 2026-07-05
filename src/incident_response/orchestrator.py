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

from .agents.brief import compose_slack_brief, compose_streaming_brief
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
from .history import PostmortemHistory, format_for_prompt, to_prior_incident
from .integrations.github import GitHubClient
from .integrations.metrics import MetricsClient
from .integrations.slack import SlackClient
from .logging_config import set_incident_id
from .pr_annotation import compose_pr_annotation
from .verification import verify_recovery
from .models import (
    Alert,
    ImpactEstimate,
    Incident,
    IncidentStatus,
    MetricSeries,
    PriorIncident,
    Runbook,
    RunbookMatch,
    SuspectCommit,
    TriageReport,
)
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
    verification_enabled: bool = True
    verification_total_minutes: int = 10
    verification_poll_seconds: int = 30
    pr_annotate_enabled: bool = True
    pr_annotate_confidence_floor: float = 0.75


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
        self._history = PostmortemHistory.load(config.postmortem_dir)

    async def _stream_triage(
        self,
        alert: Alert,
        channel: str,
    ) -> tuple[TriageReport, str, "MetricSeries"]:
        """Post an initial placeholder brief, then update it in place as each
        agent finishes. Returns the final triage report and the message ts.
        """

        posted = await self._slack.post(
            channel=channel, text=compose_streaming_brief(alert)
        )
        ts = posted.ts

        since = alert.triggered_at - timedelta(minutes=_LOOKBACK_MINUTES)
        commits_task = self._github.recent_commits(alert.service, since=since, limit=20)
        error_task = self._metrics.error_rate(alert.service, minutes=_IMPACT_WINDOW_MINUTES)
        rps_task = self._metrics.request_rate(alert.service, minutes=_IMPACT_WINDOW_MINUTES)
        active_task = self._metrics.active_users(alert.service)
        commits, error_series, rps_series, active = await asyncio.gather(
            commits_task, error_task, rps_task, active_task
        )

        history_matches = self._history.search(
            service=alert.service,
            query=f"{alert.title} {alert.description} {alert.metric or ''}",
        )
        history_context = format_for_prompt(history_matches)
        prior_incidents: list[PriorIncident] = [
            to_prior_incident(m) for m in history_matches
        ]
        if history_matches:
            logger.info(
                "history_matched",
                extra={
                    "count": len(history_matches),
                    "top_score": round(history_matches[0].score, 3),
                    "top_path": history_matches[0].incident.path,
                },
            )

        # Wrap each agent so we know which slot to fill on completion.
        async def run_suspects() -> tuple[str, list[SuspectCommit]]:
            return "suspects", await identify_suspects(
                self._llm, alert, commits, history_context=history_context
            )

        async def run_runbook() -> tuple[str, RunbookMatch | None]:
            return "runbook", await match_runbook(self._llm, alert, self._runbooks)

        async def run_impact() -> tuple[str, ImpactEstimate]:
            return "impact", await estimate_impact(
                self._llm, error_series, rps_series, active, _IMPACT_WINDOW_MINUTES
            )

        pending = {
            asyncio.create_task(run_suspects()),
            asyncio.create_task(run_runbook()),
            asyncio.create_task(run_impact()),
        }
        suspects: list[SuspectCommit] | None = None
        runbook: RunbookMatch | None = None
        impact: ImpactEstimate | None = None

        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                slot, value = task.result()
                if slot == "suspects":
                    suspects = value
                elif slot == "runbook":
                    runbook = value
                elif slot == "impact":
                    impact = value
                await self._slack.update(
                    channel=channel,
                    ts=ts,
                    text=compose_streaming_brief(
                        alert,
                        suspects=suspects,
                        runbook=runbook,
                        impact=impact,
                        prior_incidents=prior_incidents,
                        complete=not pending,
                    ),
                )

        assert suspects is not None and impact is not None
        summary = self._summarize(alert, suspects, runbook, impact)
        report = TriageReport(
            suspects=suspects,
            runbook=runbook,
            impact=impact,
            summary=summary,
            prior_incidents=prior_incidents,
        )
        # Final rewrite with the fully-styled brief (identical schema, includes summary line).
        placeholder_incident = Incident(
            id=f"inc-{alert.id}",
            alert=alert,
            status=IncidentStatus.INVESTIGATING,
            created_at=alert.triggered_at,
        )
        await self._slack.update(
            channel=channel, ts=ts, text=compose_slack_brief(placeholder_incident, report)
        )
        return report, ts, error_series

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

        triage, slack_ts, baseline_series = await self._stream_triage(
            alert, self._config.slack_channel
        )
        incident = incident.model_copy(
            update={
                "triage": triage,
                "slack_message_ts": slack_ts,
                "timeline": incident.timeline
                + [
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "event": f"Triage complete: {len(triage.suspects)} suspect(s), "
                        f"runbook={'yes' if triage.runbook else 'no'}, "
                        f"impact={triage.impact.affected_users} users; "
                        f"streamed to Slack (ts={slack_ts})",
                    }
                ],
            }
        )
        self._store.save(incident)

        await self._maybe_annotate_pr(incident, triage)

        if triage.runbook:
            remediation_summary, executed_any = await self._run_remediation(
                triage.runbook.runbook, slack_ts
            )
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
            if executed_any and self._config.verification_enabled:
                self._schedule_verification(alert, baseline_series, slack_ts)
        return incident

    async def _maybe_annotate_pr(self, incident: Incident, triage: TriageReport) -> None:
        """Post an incident-context comment on the suspect commit's PR when the
        top suspect clears the confidence floor. Non-fatal: failures are logged
        and swallowed so the incident flow never blocks on GitHub."""

        if not self._config.pr_annotate_enabled:
            return
        if not triage.suspects:
            return
        top = triage.suspects[0]
        if top.confidence < self._config.pr_annotate_confidence_floor:
            return
        pr_number = top.commit.pr_number
        if pr_number is None:
            return

        body = compose_pr_annotation(
            incident_id=incident.id,
            service=incident.alert.service,
            severity=incident.alert.severity.value,
            suspect_commit=top.commit,
            suspect_confidence=top.confidence,
            suspect_reasoning=top.reasoning,
            affected_users=triage.impact.affected_users,
            prior_incidents=triage.prior_incidents,
        )
        try:
            await self._github.annotate_pr(pr_number, body)
            logger.info(
                "pr_annotated",
                extra={"pr_number": pr_number, "sha": top.commit.sha, "confidence": top.confidence},
            )
        except Exception:
            logger.exception("pr_annotation_failed", extra={"pr_number": pr_number})

    def _schedule_verification(
        self, alert: Alert, baseline_series: MetricSeries, thread_ts: str
    ) -> None:
        """Fire-and-forget: verify recovery in the background so it doesn't block
        the incident response path. Failures are logged, never re-raised."""

        async def _run() -> None:
            try:
                await verify_recovery(
                    service=alert.service,
                    baseline_series=baseline_series,
                    metrics=self._metrics,
                    slack=self._slack,
                    channel=self._config.slack_channel,
                    thread_ts=thread_ts,
                    total_minutes=self._config.verification_total_minutes,
                    poll_seconds=self._config.verification_poll_seconds,
                )
            except Exception:
                logger.exception("verification_task_failed", extra={"alert_id": alert.id})

        asyncio.create_task(_run(), name=f"verify-{alert.id}")

    async def _run_remediation(
        self, runbook: Runbook, thread_ts: str
    ) -> tuple[str, bool]:
        steps = parse_steps(runbook)
        if not steps:
            return "", False
        results = await self._executor.run(steps)
        text = format_results_for_slack(results)
        if text:
            await self._slack.post(
                channel=self._config.slack_channel, text=text, thread_ts=thread_ts
            )
        summary = ", ".join(f"{r.step.name}={r.status}" for r in results)
        executed_any = any(r.status == "executed" for r in results)
        return f"Remediation attempted: {summary}", executed_any

    def _summarize(
        self,
        alert: Alert,
        suspects: list[SuspectCommit],
        runbook: RunbookMatch | None,
        impact: ImpactEstimate,
    ) -> str:
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
        # Fold the new post-mortem into the RAG index so the next incident sees it.
        self._history.refresh()
        return path
