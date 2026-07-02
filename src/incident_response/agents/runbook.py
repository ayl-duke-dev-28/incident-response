"""Match the alert to the most relevant runbook."""

from __future__ import annotations

from ..models import Alert, Runbook, RunbookMatch
from .llm import LLM

SYSTEM_PROMPT = """You are an SRE assistant. Given a production alert and a library of
runbooks, choose the single runbook most likely to apply. If nothing is a good fit,
return slug="" and confidence < 0.3. Never invent runbooks not in the list.
"""


def _format_runbooks(runbooks: list[Runbook]) -> str:
    lines = []
    for r in runbooks:
        tags = ", ".join(r.tags) if r.tags else "(none)"
        preview = r.content.splitlines()[0] if r.content else ""
        lines.append(f"- slug={r.slug} title=\"{r.title}\" tags=[{tags}]\n  first-line: {preview}")
    return "\n".join(lines) if lines else "(no runbooks)"


async def match_runbook(
    llm: LLM, alert: Alert, runbooks: list[Runbook]
) -> RunbookMatch | None:
    if not runbooks:
        return None

    user = (
        f"Alert: {alert.title}\n"
        f"Service: {alert.service}\n"
        f"Description: {alert.description}\n"
        f"Metric: {alert.metric}\n"
        f"Tags: {alert.tags}\n\n"
        f"Runbook library:\n{_format_runbooks(runbooks)}\n\n"
        f'Return JSON: {{"slug": "...", "confidence": 0.0, "reasoning": "..."}}'
    )

    response = await llm.json(system=SYSTEM_PROMPT, user=user, max_tokens=400)
    slug = response.get("slug", "")
    confidence = float(response.get("confidence", 0.0))
    reasoning = str(response.get("reasoning", ""))

    if not slug or confidence < 0.3:
        return None

    runbook = next((r for r in runbooks if r.slug == slug), None)
    if runbook is None:
        return None

    return RunbookMatch(runbook=runbook, confidence=confidence, reasoning=reasoning)
