from datetime import datetime, timezone

import pytest

from incident_response.integrations.slack import MockSlackClient
from incident_response.models import MetricPoint, MetricSeries
from incident_response.verification import verify_recovery


class _StubMetrics:
    """Returns a scripted sequence of error-rate peaks so tests can drive the loop."""

    def __init__(self, peaks: list[float]) -> None:
        self._peaks = list(peaks)
        self.calls = 0

    async def error_rate(self, service: str, minutes: int) -> MetricSeries:
        self.calls += 1
        peak = self._peaks.pop(0) if self._peaks else 0.0
        return MetricSeries(
            name=f"{service}.err",
            points=[MetricPoint(timestamp=datetime.now(timezone.utc), value=peak)],
            unit="ratio",
        )

    async def request_rate(self, service: str, minutes: int):  # unused
        raise NotImplementedError

    async def active_users(self, service: str) -> int:
        return 0


async def _sleep_noop(seconds: float) -> None:
    return None


def _baseline(peak: float) -> MetricSeries:
    return MetricSeries(
        name="checkout.err",
        points=[MetricPoint(timestamp=datetime.now(timezone.utc), value=peak)],
        unit="ratio",
    )


@pytest.mark.asyncio
async def test_recovered_when_error_rate_drops_below_threshold():
    metrics = _StubMetrics([0.02])  # 0.02 vs baseline 0.18 → 11% → recovered
    slack = MockSlackClient()
    result = await verify_recovery(
        service="checkout",
        baseline_series=_baseline(0.18),
        metrics=metrics,
        slack=slack,
        channel="#x",
        thread_ts="t1",
        total_minutes=1,
        poll_seconds=30,
        sleep=_sleep_noop,
    )
    assert result.status == "recovered"
    assert slack.sent[0].thread_ts == "t1"
    assert "Recovered" in slack.sent[0].text


@pytest.mark.asyncio
async def test_still_elevated_when_error_rate_does_not_drop():
    metrics = _StubMetrics([0.17, 0.17])  # basically unchanged from 0.18
    slack = MockSlackClient()
    result = await verify_recovery(
        service="checkout",
        baseline_series=_baseline(0.18),
        metrics=metrics,
        slack=slack,
        channel="#x",
        thread_ts="t1",
        total_minutes=1,
        poll_seconds=30,
        sleep=_sleep_noop,
    )
    assert result.status == "still_elevated"
    assert "Still elevated" in slack.sent[0].text


@pytest.mark.asyncio
async def test_improving_when_between_thresholds():
    metrics = _StubMetrics([0.08, 0.08])  # 44% of baseline → improving
    slack = MockSlackClient()
    result = await verify_recovery(
        service="checkout",
        baseline_series=_baseline(0.18),
        metrics=metrics,
        slack=slack,
        channel="#x",
        thread_ts="t1",
        total_minutes=1,
        poll_seconds=30,
        sleep=_sleep_noop,
    )
    assert result.status == "improving"


@pytest.mark.asyncio
async def test_no_baseline_short_circuits():
    metrics = _StubMetrics([])
    slack = MockSlackClient()
    result = await verify_recovery(
        service="checkout",
        baseline_series=_baseline(0.0),
        metrics=metrics,
        slack=slack,
        channel="#x",
        thread_ts="t1",
        sleep=_sleep_noop,
    )
    assert result.status == "no_baseline"
    assert metrics.calls == 0
    assert "Verification skipped" in slack.sent[0].text
