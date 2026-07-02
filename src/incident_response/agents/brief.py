"""Compose the initial Slack incident brief.

Deterministic template — no LLM needed. Keeps the on-call message concise and skimmable
in the first 30 seconds of a page.
"""

from __future__ import annotations

from ..models import Incident, TriageReport


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

    lines.append("")
    lines.append(f"*Summary:* {triage.summary}")
    return "\n".join(lines)
