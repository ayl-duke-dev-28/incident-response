"""Post-remediation verification.

After the executor runs actions, spawn a background task that polls the metrics
client for N minutes and posts one of three outcomes back to the incident thread:

  ✅ RECOVERED — error rate dropped below the recovery threshold
  ⚠️ IMPROVING — error rate down significantly but still elevated
  ❌ STILL ELEVATED — no meaningful drop, escalation warranted

The pre-remediation "baseline" is the peak error rate observed at trigger time,
so the recovery decision is grounded in real-vs-real numbers, not just "is it
below the alert threshold now" (which can flap).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from .integrations.metrics import MetricsClient
from .integrations.slack import SlackClient
from .models import MetricSeries

logger = logging.getLogger(__name__)


DEFAULT_TOTAL_MINUTES = 10
DEFAULT_POLL_SECONDS = 30
DEFAULT_RECOVERY_RATIO = 0.25  # current peak < 25% of pre-remediation peak = recovered
DEFAULT_IMPROVEMENT_RATIO = 0.60


@dataclass(frozen=True)
class VerificationResult:
    status: str  # "recovered" | "improving" | "still_elevated" | "no_baseline"
    baseline_peak: float
    final_peak: float
    minutes_elapsed: float
    message: str


def _series_peak(series: MetricSeries) -> float:
    if not series.points:
        return 0.0
    return max(p.value for p in series.points)


async def verify_recovery(
    *,
    service: str,
    baseline_series: MetricSeries,
    metrics: MetricsClient,
    slack: SlackClient,
    channel: str,
    thread_ts: str,
    total_minutes: int = DEFAULT_TOTAL_MINUTES,
    poll_seconds: int = DEFAULT_POLL_SECONDS,
    recovery_ratio: float = DEFAULT_RECOVERY_RATIO,
    improvement_ratio: float = DEFAULT_IMPROVEMENT_RATIO,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> VerificationResult:
    """Poll the error-rate series after remediation and report the outcome.

    `sleep` is injectable so tests can drive the loop deterministically without
    burning real seconds.
    """

    baseline_peak = _series_peak(baseline_series)
    if baseline_peak == 0:
        result = VerificationResult(
            status="no_baseline",
            baseline_peak=0.0,
            final_peak=0.0,
            minutes_elapsed=0.0,
            message="No baseline error rate to compare against; skipping verification.",
        )
        await _post(slack, channel, thread_ts, _format(result))
        return result

    deadline_iterations = max(1, int(total_minutes * 60 // poll_seconds))
    elapsed_seconds = 0.0
    final_peak = baseline_peak

    for _ in range(deadline_iterations):
        await sleep(poll_seconds)
        elapsed_seconds += poll_seconds
        # Look at a short recent window so we react to the current state, not stale peaks.
        window_minutes = max(1, int(elapsed_seconds // 60) or 1)
        current = await metrics.error_rate(service, minutes=window_minutes)
        current_peak = _series_peak(current)
        final_peak = current_peak
        logger.info(
            "verification_poll",
            extra={
                "service": service,
                "baseline_peak": baseline_peak,
                "current_peak": current_peak,
                "elapsed_s": elapsed_seconds,
            },
        )
        if current_peak <= baseline_peak * recovery_ratio:
            result = VerificationResult(
                status="recovered",
                baseline_peak=baseline_peak,
                final_peak=current_peak,
                minutes_elapsed=elapsed_seconds / 60,
                message=(
                    f"error rate {baseline_peak * 100:.2f}% → {current_peak * 100:.2f}% "
                    f"after {elapsed_seconds / 60:.1f} min"
                ),
            )
            await _post(slack, channel, thread_ts, _format(result))
            return result

    if final_peak <= baseline_peak * improvement_ratio:
        status = "improving"
    else:
        status = "still_elevated"
    result = VerificationResult(
        status=status,
        baseline_peak=baseline_peak,
        final_peak=final_peak,
        minutes_elapsed=elapsed_seconds / 60,
        message=(
            f"error rate {baseline_peak * 100:.2f}% → {final_peak * 100:.2f}% "
            f"after {elapsed_seconds / 60:.1f} min"
        ),
    )
    await _post(slack, channel, thread_ts, _format(result))
    return result


def _format(result: VerificationResult) -> str:
    icons = {
        "recovered": ":white_check_mark: *Recovered.*",
        "improving": ":large_yellow_circle: *Improving but still elevated.*",
        "still_elevated": ":x: *Still elevated — escalating.*",
        "no_baseline": ":information_source: *Verification skipped* — no baseline.",
    }
    return f"{icons[result.status]} {result.message}"


async def _post(slack: SlackClient, channel: str, thread_ts: str, text: str) -> None:
    try:
        await slack.post(channel=channel, text=text, thread_ts=thread_ts)
    except Exception:
        logger.exception("verification_slack_post_failed")
