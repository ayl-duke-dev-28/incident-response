import json
from datetime import datetime, timezone

import httpx

from incident_response.integrations.on_call import PagerDutyOnCallClient
from incident_response.integrations.tickets import JiraTicketClient, LinearTicketClient
from incident_response.models import Alert, Incident, Severity


def _incident() -> Incident:
    alert = Alert(
        id="event-1",
        title="Checkout failures",
        description="Error rate is above threshold",
        service="checkout",
        severity=Severity.SEV2,
        triggered_at=datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc),
    )
    return Incident(id="inc-event-1", alert=alert, created_at=alert.triggered_at)


async def test_pagerduty_resolves_service_policy_and_current_on_call():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/services/PSERVICE":
            return httpx.Response(
                200,
                json={"service": {"escalation_policy": {"id": "PEP"}}},
            )
        return httpx.Response(
            200,
            json={
                "oncalls": [
                    {
                        "escalation_level": 1,
                        "user": {
                            "id": "PUSER",
                            "summary": "Alex Operator",
                            "email": "alex@example.com",
                        },
                        "schedule": {"id": "PSCHEDULE", "summary": "Checkout primary"},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.pagerduty.com",
    ) as http:
        client = PagerDutyOnCallClient(
            token="pd-token",
            service_ids={"checkout": "PSERVICE"},
            http=http,
        )
        responders = await client.lookup("checkout")

    assert responders[0].email == "alex@example.com"
    assert responders[0].schedule == "Checkout primary"
    assert requests[0].headers["authorization"] == "Token token=pd-token"
    assert requests[1].url.params.get_list("escalation_policy_ids[]") == ["PEP"]


async def test_jira_creates_adf_incident_ticket_and_returns_reference():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["authorization"]
        captured["payload"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "10001", "key": "OPS-42"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://acme.atlassian.net",
    ) as http:
        client = JiraTicketClient(
            base_url="https://acme.atlassian.net",
            email="jira@example.com",
            api_token="jira-token",
            project_key="OPS",
            issue_type="Incident",
            http=http,
        )
        reference = await client.create(_incident(), idempotency_key="incident:inc-event-1:jira")

    fields = captured["payload"]["fields"]
    assert fields["project"] == {"key": "OPS"}
    assert fields["description"]["type"] == "doc"
    assert reference.provider == "jira"
    assert reference.external_id == "OPS-42"
    assert reference.url == "https://acme.atlassian.net/browse/OPS-42"
    assert str(captured["authorization"]).startswith("Basic ")


async def test_linear_creates_incident_ticket_and_returns_reference():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["authorization"]
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": {
                    "issueCreate": {
                        "success": True,
                        "issue": {
                            "id": "linear-id",
                            "identifier": "OPS-9",
                            "url": "https://linear.app/acme/issue/OPS-9",
                        },
                    }
                }
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.linear.app",
    ) as http:
        client = LinearTicketClient(token="lin_api_token", team_id="team-id", http=http)
        reference = await client.create(_incident(), idempotency_key="incident:inc-event-1:linear")

    variables = captured["payload"]["variables"]
    assert variables["input"]["teamId"] == "team-id"
    assert "inc-event-1" in variables["input"]["description"]
    assert captured["authorization"] == "lin_api_token"
    assert reference.provider == "linear"
    assert reference.external_id == "OPS-9"
