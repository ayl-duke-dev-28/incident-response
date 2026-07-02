"""Metrics adapter. Mock generates a synthetic spike; Datadog mode hits the real API."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

import httpx

from ..models import MetricPoint, MetricSeries
from ..retry import async_retry

_RETRYABLE_HTTP = (httpx.HTTPError, TimeoutError)


class MetricsClient(ABC):
    @abstractmethod
    async def error_rate(self, service: str, minutes: int) -> MetricSeries:
        ...

    @abstractmethod
    async def request_rate(self, service: str, minutes: int) -> MetricSeries:
        ...

    @abstractmethod
    async def active_users(self, service: str) -> int:
        ...


class MockMetricsClient(MetricsClient):
    """Deterministic synthetic data — baseline traffic with an error-rate spike at the tail."""

    def __init__(self, active_users: int = 12_400, baseline_rps: float = 320.0) -> None:
        self._active = active_users
        self._rps = baseline_rps

    async def error_rate(self, service: str, minutes: int) -> MetricSeries:
        now = datetime.now(timezone.utc)
        points: list[MetricPoint] = []
        for i in range(minutes):
            t = now - timedelta(minutes=minutes - i)
            # Spike in last third of the window
            spiked = i > (minutes * 2 // 3)
            value = 0.007 if not spiked else 0.184
            points.append(MetricPoint(timestamp=t, value=value))
        return MetricSeries(name=f"{service}.error_rate", points=points, unit="ratio")

    async def request_rate(self, service: str, minutes: int) -> MetricSeries:
        now = datetime.now(timezone.utc)
        points = [
            MetricPoint(timestamp=now - timedelta(minutes=minutes - i), value=self._rps)
            for i in range(minutes)
        ]
        return MetricSeries(name=f"{service}.rps", points=points, unit="req/s")

    async def active_users(self, service: str) -> int:
        return self._active


class DatadogMetricsClient(MetricsClient):
    def __init__(self, api_key: str, app_key: str) -> None:
        self._api_key = api_key
        self._app_key = app_key
        self._base = "https://api.datadoghq.com/api/v1"

    @async_retry(attempts=3, base_delay=0.5, retry_on=_RETRYABLE_HTTP)
    async def _query(self, query: str, minutes: int) -> MetricSeries:
        now = int(datetime.now(timezone.utc).timestamp())
        params = {"from": now - minutes * 60, "to": now, "query": query}
        headers = {"DD-API-KEY": self._api_key, "DD-APPLICATION-KEY": self._app_key}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{self._base}/query", params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        series = data.get("series", [{}])[0]
        raw_points = series.get("pointlist", [])
        points = [
            MetricPoint(
                timestamp=datetime.fromtimestamp(p[0] / 1000, tz=timezone.utc), value=float(p[1])
            )
            for p in raw_points
            if p[1] is not None
        ]
        return MetricSeries(name=query, points=points, unit=series.get("unit", ""))

    async def error_rate(self, service: str, minutes: int) -> MetricSeries:
        query = f"sum:trace.http.request.errors{{service:{service}}}.as_rate()"
        return await self._query(query, minutes)

    async def request_rate(self, service: str, minutes: int) -> MetricSeries:
        query = f"sum:trace.http.request.hits{{service:{service}}}.as_rate()"
        return await self._query(query, minutes)

    async def active_users(self, service: str) -> int:
        # Datadog custom RUM query would go here; return best-effort 0 in absence.
        return 0


def build_metrics_client(mode: str, dd_api_key: str, dd_app_key: str) -> MetricsClient:
    if mode == "datadog" and dd_api_key and dd_app_key:
        return DatadogMetricsClient(api_key=dd_api_key, app_key=dd_app_key)
    return MockMetricsClient()
