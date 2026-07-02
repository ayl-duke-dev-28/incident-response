from datetime import datetime, timezone

from incident_response.agents.impact import estimate_impact
from incident_response.agents.llm import FakeLLM
from incident_response.models import MetricPoint, MetricSeries


async def test_impact_estimate_parses_llm_response():
    now = datetime.now(timezone.utc)
    err = MetricSeries(
        name="err",
        points=[MetricPoint(timestamp=now, value=0.18)],
        unit="ratio",
    )
    rps = MetricSeries(
        name="rps",
        points=[MetricPoint(timestamp=now, value=320.0)],
        unit="req/s",
    )
    llm = FakeLLM(
        [
            {
                "affected_users": 2232,
                "affected_percent": 18.0,
                "error_rate": 0.18,
                "reasoning": "18% of 12400 active users",
            }
        ]
    )
    impact = await estimate_impact(llm, err, rps, active_users=12_400, window_minutes=15)
    assert impact.affected_users == 2232
    assert impact.affected_percent == 18.0
    assert impact.error_rate == 0.18
    assert impact.time_window_minutes == 15
