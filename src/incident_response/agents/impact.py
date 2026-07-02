"""Estimate user impact from metrics."""

from __future__ import annotations

from ..models import ImpactEstimate, MetricSeries
from .llm import LLM

SYSTEM_PROMPT = """You estimate user impact for a live incident.

You receive error-rate and request-rate series plus a headcount of active users.
Return an integer estimate of affected users, the percent of active users affected,
the current peak error rate, and a one-sentence reasoning.

Formulas to prefer:
- peak_error_rate = max(error_rate points in the last third of the window)
- affected_users ≈ active_users * peak_error_rate (integer)
- affected_percent = peak_error_rate * 100
"""


def _summarize(series: MetricSeries) -> str:
    if not series.points:
        return "no data"
    values = [p.value for p in series.points]
    peak = max(values)
    tail = values[-max(1, len(values) // 3) :]
    tail_peak = max(tail)
    return (
        f"n={len(values)} unit={series.unit} peak={peak:.4f} tail_peak={tail_peak:.4f} "
        f"first={values[0]:.4f} last={values[-1]:.4f}"
    )


async def estimate_impact(
    llm: LLM, error_rate: MetricSeries, request_rate: MetricSeries, active_users: int, window_minutes: int
) -> ImpactEstimate:
    user = (
        f"Active users right now: {active_users}\n"
        f"Time window: last {window_minutes} minutes\n"
        f"Error rate: {_summarize(error_rate)}\n"
        f"Request rate: {_summarize(request_rate)}\n\n"
        f'Return JSON: {{"affected_users": 0, "affected_percent": 0.0, '
        f'"error_rate": 0.0, "reasoning": "..."}}'
    )

    response = await llm.json(system=SYSTEM_PROMPT, user=user, max_tokens=400)
    return ImpactEstimate(
        affected_users=int(response.get("affected_users", 0)),
        affected_percent=float(response.get("affected_percent", 0.0)),
        error_rate=float(response.get("error_rate", 0.0)),
        reasoning=str(response.get("reasoning", "")),
        time_window_minutes=window_minutes,
    )
