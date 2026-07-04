"""Compose the initial Slack incident brief.

Deterministic template — no LLM needed. Keeps the on-call message concise and skimmable
in the first 30 seconds of a page.
"""

from __future__ import annotations

from ..models import (
    Alert,
    ImpactEstimate,
    Incident,
    PriorIncident,
    RunbookMatch,
    SuspectCommit,
    TriageReport,
)

_MAX_PRIORS_IN_BRIEF = 3
_ROOT_CAUSE_SNIPPET_CHARS = 140


def _format_prior_incidents(priors: list[PriorIncident]) -> list[str]:
    """Render prior incidents as Slack lines. Empty list => empty output."""

    if not priors:
        return []
    lines = ["*Prior similar incidents:*"]
    for p in priors[:_MAX_PRIORS_IN_BRIEF]:
        snippet = " ".join(p.root_cause.split())[:_ROOT_CAUSE_SNIPPET_CHARS]
        if len(p.root_cause) > _ROOT_CAUSE_SNIPPET_CHARS:
            snippet += "…"
        lines.append(
            f"  • {p.date} <{p.postmortem_path}|{p.title}> "
            f"(sim {p.score:.2f}) — _{snippet}_"
        )
    return lines


def compose_slack_brief(incident: Incident, triage: TriageReport) -> str:
    alert = incident.alert
    lines = [
        f":rotating_light: *{alert.severity.value.upper()} — {alert.title}*",
        f"*Service:* `{alert.service}`  •  *Alert ID:* `{alert.id}`  •  "
        f"*Triggered:* {alert.triggered_at.isoformat(timespec='seconds')}",
        "",
        f"*Impact:* ~{triage.impact.affected_users:,} users affected "
        f"({triage.impact.affected_percent:.1f}% of active) — "
        f"error rate {triage.impact.error_rate * 100:.1f}%",
        f"_{triage.impact.reasoning}_",
        "",
    ]

    if triage.suspects:
        lines.append("*Likely bad commits:*")
        for s in triage.suspects[:3]:
            pr = f" (PR #{s.commit.pr_number})" if s.commit.pr_number else ""
            lines.append(
                f"  • `{s.commit.sha[:8]}` by {s.commit.author}{pr} — "
                f"conf {s.confidence:.0%} — {s.commit.message.splitlines()[0]}"
            )
            lines.append(f"    _{s.reasoning}_")
    else:
        lines.append("*Likely bad commits:* none identified in recent window.")

    lines.append("")
    if triage.runbook:
        rb = triage.runbook.runbook
        lines.append(
            f"*Runbook:* <{rb.path}|{rb.title}> "
            f"(match {triage.runbook.confidence:.0%}) — _{triage.runbook.reasoning}_"
        )
    else:
        lines.append("*Runbook:* no strong match — paging on-call for manual triage.")

    prior_lines = _format_prior_incidents(triage.prior_incidents)
    if prior_lines:
        lines.append("")
        lines.extend(prior_lines)

    lines.append("")
    lines.append(f"*Summary:* {triage.summary}")
    return "\n".join(lines)


def compose_streaming_brief(
    alert: Alert,
    *,
    suspects: list[SuspectCommit] | None = None,
    runbook: RunbookMatch | None = None,
    impact: ImpactEstimate | None = None,
    prior_incidents: list[PriorIncident] | None = None,
    complete: bool = False,
) -> str:
    """Render a brief that shows partial progress as agents complete.

    Fields default to ':hourglass_flowing_sand: investigating…' when the
    corresponding agent hasn't returned yet, letting the caller edit the
    same Slack message in place as more information arrives.
    """

    header_icon = ":white_check_mark:" if complete else ":rotating_light:"
    lines = [
        f"{header_icon} *{alert.severity.value.upper()} — {alert.title}*",
        f"*Service:* `{alert.service}`  •  *Alert ID:* `{alert.id}`  •  "
        f"*Triggered:* {alert.triggered_at.isoformat(timespec='seconds')}",
        "",
    ]

    if impact is not None:
        lines.append(
            f"*Impact:* ~{impact.affected_users:,} users affected "
            f"({impact.affected_percent:.1f}% of active) — "
            f"error rate {impact.error_rate * 100:.1f}%"
        )
        lines.append(f"_{impact.reasoning}_")
    else:
        lines.append("*Impact:* :hourglass_flowing_sand: estimating…")
    lines.append("")

    if suspects is None:
        lines.append("*Likely bad commits:* :hourglass_flowing_sand: investigating…")
    elif suspects:
        lines.append("*Likely bad commits:*")
        for s in suspects[:3]:
            pr = f" (PR #{s.commit.pr_number})" if s.commit.pr_number else ""
            lines.append(
                f"  • `{s.commit.sha[:8]}` by {s.commit.author}{pr} — "
                f"conf {s.confidence:.0%} — {s.commit.message.splitlines()[0]}"
            )
            lines.append(f"    _{s.reasoning}_")
    else:
        lines.append("*Likely bad commits:* none identified in recent window.")
    lines.append("")

    if runbook is None and not complete:
        lines.append("*Runbook:* :hourglass_flowing_sand: searching…")
    elif runbook is not None:
        rb = runbook.runbook
        lines.append(
            f"*Runbook:* <{rb.path}|{rb.title}> "
            f"(match {runbook.confidence:.0%}) — _{runbook.reasoning}_"
        )
    else:
        lines.append("*Runbook:* no strong match — paging on-call for manual triage.")

    prior_lines = _format_prior_incidents(prior_incidents or [])
    if prior_lines:
        lines.append("")
        lines.extend(prior_lines)

    return "\n".join(lines)
