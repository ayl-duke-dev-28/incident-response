"""Deterministic composer for the PR comment posted on a suspect commit.

No LLM. Markdown body suitable for the GitHub issue-comments endpoint. Same
'we've seen this before' context as the Slack brief, so the PR author lands
in the incident with the resolution history already in hand.
"""

from __future__ import annotations

from .models import Commit, PriorIncident

_MAX_PRIORS = 3
_ROOT_CAUSE_SNIPPET_CHARS = 200


def compose_pr_annotation(
    *,
    incident_id: str,
    service: str,
    severity: str,
    suspect_commit: Commit,
    suspect_confidence: float,
    suspect_reasoning: str,
    affected_users: int,
    prior_incidents: list[PriorIncident],
) -> str:
    lines = [
        f"**Incident {incident_id}** — `{service}` service, {severity.upper()}",
        "",
        f"Triage flagged commit `{suspect_commit.sha[:8]}` as a suspect "
        f"(confidence {suspect_confidence:.0%}): {suspect_reasoning}",
        "",
        f"**Impact:** ~{affected_users:,} users affected",
    ]

    if prior_incidents:
        lines.append("")
        lines.append("**Prior similar incidents on this service:**")
        for p in prior_incidents[:_MAX_PRIORS]:
            snippet = " ".join(p.root_cause.split())[:_ROOT_CAUSE_SNIPPET_CHARS]
            if len(p.root_cause) > _ROOT_CAUSE_SNIPPET_CHARS:
                snippet += "…"
            lines.append(
                f"- {p.date} [{p.title}]({p.postmortem_path}) "
                f"(similarity {p.score:.2f}) — {snippet}"
            )

    lines.append("")
    lines.append(
        "_Auto-annotated by the incident-response system. "
        "Reply here or in the Slack incident thread to add context._"
    )
    return "\n".join(lines)
