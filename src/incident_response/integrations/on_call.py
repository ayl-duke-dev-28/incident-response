"""PagerDuty on-call resolution for an internal service name."""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from ..models import OnCallResponder


class PagerDutyOnCallClient:
    def __init__(
        self,
        *,
        token: str,
        service_ids: Mapping[str, str],
        http: httpx.AsyncClient,
    ) -> None:
        self._token = token
        self._service_ids = dict(service_ids)
        self._http = http
        self._headers = {
            "Authorization": f"Token token={token}",
            "Accept": "application/vnd.pagerduty+json;version=2",
        }

    async def lookup(self, service: str) -> list[OnCallResponder]:
        service_id = self._service_ids.get(service)
        if not service_id:
            return []
        service_response = await self._http.get(
            f"/services/{service_id}",
            headers=self._headers,
        )
        service_response.raise_for_status()
        policy_id = service_response.json()["service"]["escalation_policy"]["id"]
        oncalls_response = await self._http.get(
            "/oncalls",
            headers=self._headers,
            params=[
                ("escalation_policy_ids[]", policy_id),
                ("include[]", "users"),
                ("include[]", "schedules"),
                ("earliest", "true"),
            ],
        )
        oncalls_response.raise_for_status()
        responders: list[OnCallResponder] = []
        for oncall in oncalls_response.json().get("oncalls", []):
            user = oncall.get("user") or {}
            schedule = oncall.get("schedule") or {}
            responders.append(
                OnCallResponder(
                    provider="pagerduty",
                    user_id=str(user.get("id", "")),
                    name=str(user.get("summary", "")),
                    email=str(user.get("email", "")),
                    schedule=str(schedule.get("summary", "")),
                    escalation_level=int(oncall.get("escalation_level", 1)),
                )
            )
        return responders


class MockOnCallClient:
    async def lookup(self, service: str) -> list[OnCallResponder]:
        return [
            OnCallResponder(
                provider="mock",
                user_id=f"mock-{service}",
                name=f"{service.title()} On-call",
                email=f"{service}@example.invalid",
                schedule=f"{service.title()} Primary",
            )
        ]
