"""Jira Cloud and Linear incident ticket adapters."""

from __future__ import annotations

import httpx

from ..models import ExternalReference, Incident


def _description(incident: Incident, idempotency_key: str) -> str:
    return (
        f"Incident: {incident.id}\n"
        f"Service: {incident.alert.service}\n"
        f"Severity: {incident.alert.severity.value}\n\n"
        f"{incident.alert.description}\n\n"
        f"Idempotency key: {idempotency_key}"
    )


class JiraTicketClient:
    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        api_token: str,
        project_key: str,
        issue_type: str,
        http: httpx.AsyncClient,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._email = email
        self._api_token = api_token
        self._project_key = project_key
        self._issue_type = issue_type
        self._http = http

    async def create(self, incident: Incident, *, idempotency_key: str) -> ExternalReference:
        description = _description(incident, idempotency_key)
        response = await self._http.post(
            "/rest/api/3/issue",
            auth=httpx.BasicAuth(self._email, self._api_token),
            json={
                "fields": {
                    "project": {"key": self._project_key},
                    "issuetype": {"name": self._issue_type},
                    "summary": f"[{incident.alert.severity.value.upper()}] {incident.alert.title}",
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": description}],
                            }
                        ],
                    },
                },
                "properties": [
                    {
                        "key": "incident_response_idempotency_key",
                        "value": idempotency_key,
                    }
                ],
            },
        )
        response.raise_for_status()
        key = str(response.json()["key"])
        return ExternalReference(
            provider="jira",
            external_id=key,
            url=f"{self._base_url}/browse/{key}",
        )


class LinearTicketClient:
    def __init__(
        self,
        *,
        token: str,
        team_id: str,
        http: httpx.AsyncClient,
    ) -> None:
        self._token = token
        self._team_id = team_id
        self._http = http

    async def create(self, incident: Incident, *, idempotency_key: str) -> ExternalReference:
        response = await self._http.post(
            "/graphql",
            headers={"Authorization": self._token, "Content-Type": "application/json"},
            json={
                "query": """
                    mutation CreateIncident($input: IssueCreateInput!) {
                      issueCreate(input: $input) {
                        success
                        issue { id identifier url }
                      }
                    }
                """,
                "variables": {
                    "input": {
                        "teamId": self._team_id,
                        "title": (
                            f"[{incident.alert.severity.value.upper()}] "
                            f"{incident.alert.title}"
                        ),
                        "description": _description(incident, idempotency_key),
                    }
                },
            },
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise RuntimeError("Linear issue creation failed")
        result = payload["data"]["issueCreate"]
        if not result.get("success") or not result.get("issue"):
            raise RuntimeError("Linear issue creation failed")
        issue = result["issue"]
        return ExternalReference(
            provider="linear",
            external_id=str(issue["identifier"]),
            url=str(issue["url"]),
        )


class MockTicketClient:
    def __init__(self, provider: str) -> None:
        self._provider = provider

    async def create(self, incident: Incident, *, idempotency_key: str) -> ExternalReference:
        external_id = f"MOCK-{incident.id}"
        return ExternalReference(
            provider=self._provider,
            external_id=external_id,
            url=f"https://example.invalid/{self._provider}/{external_id}",
        )
