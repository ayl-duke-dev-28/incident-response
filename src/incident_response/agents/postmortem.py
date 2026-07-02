"""Generate a post-mortem markdown once an incident is marked resolved."""

from __future__ import annotations

from datetime import datetime

from ..models import Incident
from .llm import LLM

SYSTEM_PROMPT = """You are writing a blameless post-mortem for a resolved production incident.

Follow this structure exactly:
1. Summary (2-3 sentences)
2. Impact (users affected, duration, severity)
3. Timeline (bullet list of key events, use provided timeline verbatim where possible)
4. Root Cause (technical, specific, cites the suspect commit if applicable)
5. Detection (how we noticed — the alert)
6. Mitigation (what stopped the bleeding)
7. Action Items (3-5 concrete items with owner suggestions and priorities: P0/P1/P2)
8. Lessons Learned (short paragraph, blameless tone)

Return JSON with a single key "markdown" containing the full document.
"""


def _format_timeline(timeline: list[dict[str, object]]) -> str:
    if not timeline:
        return "(no timeline events recorded)"
    return "\n".join(
        f"- {event.get('timestamp', '?')}: {event.get('event', '')}" for event in timeline
    )


async def generate_postmortem(llm: LLM, incident: Incident) -> str:
    if incident.resolved_at is None:
        raise ValueError("Cannot generate post-mortem for an unresolved incident.")

    triage = incident.triage
    alert = incident.alert
    duration = incident.resolved_at - incident.created_at

    suspects_block = "none identified"
    if triage and triage.suspects:
        suspects_block = "\n".join(
            f"- {s.commit.sha[:10]} by {s.commit.author} (conf {s.confidence:.0%}): "
            f"{s.commit.message.splitlines()[0]} — {s.reasoning}"
            for s in triage.suspects
        )

    runbook_block = "none used"
    if triage and triage.runbook:
        runbook_block = f"{triage.runbook.runbook.title} ({triage.runbook.runbook.path})"

    impact_block = "unknown"
    if triage:
        impact_block = (
            f"~{triage.impact.affected_users} users "
            f"({triage.impact.affected_percent:.1f}% of active), "
            f"peak error rate {triage.impact.error_rate * 100:.2f}%"
        )

    user = f"""Incident: {incident.id}
Title: {alert.title}
Service: {alert.service}
Severity: {alert.severity.value}
Detected at: {incident.created_at.isoformat()}
Resolved at: {incident.resolved_at.isoformat()}
Duration: {int(duration.total_seconds() // 60)} minutes

Impact: {impact_block}

Suspect commits:
{suspects_block}

Runbook used: {runbook_block}

Timeline events:
{_format_timeline(incident.timeline)}

Write the post-mortem now.
"""

    response = await llm.json(system=SYSTEM_PROMPT, user=user, max_tokens=2500)
    markdown = response.get("markdown", "")
    if not markdown:
        # Fall back to a minimal deterministic template so we never fail silently.
        markdown = _fallback_postmortem(incident, duration)
    return markdown


def _fallback_postmortem(incident: Incident, duration) -> str:
    return f"""# Post-Mortem: {incident.alert.title}

**Incident ID:** {incident.id}
**Severity:** {incident.alert.severity.value}
**Duration:** {int(duration.total_seconds() // 60)} minutes
**Detected:** {incident.created_at.isoformat()}
**Resolved:** {(incident.resolved_at or datetime.now()).isoformat()}

## Summary
An alert fired on `{incident.alert.service}` and was resolved after {int(duration.total_seconds() // 60)} minutes.

## Timeline
{chr(10).join(f"- {e.get('timestamp', '?')}: {e.get('event', '')}" for e in incident.timeline)}

## Action Items
- [ ] Review the suspect commits for regression risk (P1)
- [ ] Confirm the runbook was up to date (P2)
- [ ] Add a canary for this failure mode (P1)
"""
