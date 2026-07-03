"""Identify which recent commit most likely caused the incident."""

from __future__ import annotations

from ..models import Alert, Commit, SuspectCommit
from .llm import LLM

SYSTEM_PROMPT = """You are a staff SRE performing incident triage.

Given a production alert, a list of recent commits, and (when available) similar
past incidents, identify which commits are most likely responsible. Consider:
- Timing (commits deployed near the alert time are more suspect)
- Files touched (matching the alerting service or subsystem)
- Commit message signal (perf, refactor, dependency bumps, feature flags)
- Size (large diffs = higher risk)
- Prior incidents on this service with similar symptoms — if a past root cause
  matches (e.g. "Redis maxmemory eviction"), commits touching that area are
  much more suspect.

Rank up to 3 suspects. For each: sha, confidence (0.0-1.0), and one-sentence reasoning.
When a past incident is highly relevant, mention it briefly in the reasoning.
"""


def _format_commits(commits: list[Commit]) -> str:
    lines = []
    for c in commits:
        lines.append(
            f"- sha={c.sha} author={c.author} at={c.timestamp.isoformat()} "
            f"files={c.files_changed} +{c.additions}/-{c.deletions}\n"
            f"  message: {c.message}"
        )
    return "\n".join(lines) if lines else "(no recent commits)"


async def identify_suspects(
    llm: LLM,
    alert: Alert,
    commits: list[Commit],
    max_suspects: int = 3,
    history_context: str = "",
) -> list[SuspectCommit]:
    if not commits:
        return []

    history_block = f"\n{history_context}\n" if history_context else ""

    user = (
        f"Alert: {alert.title}\n"
        f"Service: {alert.service}\n"
        f"Severity: {alert.severity.value}\n"
        f"Triggered: {alert.triggered_at.isoformat()}\n"
        f"Metric: {alert.metric} value={alert.value} threshold={alert.threshold}\n"
        f"Description: {alert.description}\n"
        f"{history_block}\n"
        f"Recent commits (newest first):\n{_format_commits(commits)}\n\n"
        f'Return JSON: {{"suspects": [{{"sha": "...", "confidence": 0.0, "reasoning": "..."}}]}}'
    )

    response = await llm.json(system=SYSTEM_PROMPT, user=user, max_tokens=800)
    raw_suspects = response.get("suspects", [])[:max_suspects]

    by_sha = {c.sha: c for c in commits}
    result: list[SuspectCommit] = []
    for entry in raw_suspects:
        sha = entry.get("sha", "")
        commit = by_sha.get(sha)
        if commit is None:
            # Try prefix match — Claude sometimes shortens SHAs.
            commit = next((c for c in commits if c.sha.startswith(sha) or sha.startswith(c.sha)), None)
        if commit is None:
            continue
        result.append(
            SuspectCommit(
                commit=commit,
                confidence=float(entry.get("confidence", 0.0)),
                reasoning=str(entry.get("reasoning", "")),
            )
        )
    return result
