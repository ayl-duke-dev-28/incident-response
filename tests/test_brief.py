from datetime import datetime, timezone

from incident_response.agents.brief import compose_slack_brief
from incident_response.models import (
    Commit,
    ImpactEstimate,
    Incident,
    IncidentStatus,
    Runbook,
    RunbookMatch,
    SuspectCommit,
    TriageReport,
)


def _fixture_triage() -> TriageReport:
    commit = Commit(
        sha="a1b2c3d4e5f6",
        author="jamie",
        message="perf: switch pricing to new cache",
        timestamp=datetime(2026, 7, 2, 20, 57, tzinfo=timezone.utc),
        files_changed=["services/checkout/pricing.py"],
        pr_number=4821,
    )
    return TriageReport(
        suspects=[
            SuspectCommit(commit=commit, confidence=0.87, reasoning="deployed 8m before alert")
        ],
        runbook=RunbookMatch(
            runbook=Runbook(
                slug="checkout-error-rate",
                title="Checkout elevated error rate",
                tags=["checkout"],
                content="body",
                path="runbooks/checkout-error-rate.md",
            ),
            confidence=0.92,
            reasoning="matches symptoms",
        ),
        impact=ImpactEstimate(
            affected_users=2232,
            affected_percent=18.0,
            error_rate=0.184,
            reasoning="18% of 12400",
        ),
        summary="Top suspect a1b2c3d4",
    )


def test_brief_includes_severity_service_impact_and_suspect(alert):
    incident = Incident(
        id="inc-1", alert=alert, status=IncidentStatus.INVESTIGATING, created_at=alert.triggered_at
    )
    text = compose_slack_brief(incident, _fixture_triage())
    assert "SEV2" in text
    assert "`checkout`" in text
    assert "2,232 users" in text
    assert "a1b2c3d4" in text
    assert "PR #4821" in text
    assert "Checkout elevated error rate" in text


def test_brief_handles_no_suspects_and_no_runbook(alert):
    triage = _fixture_triage().model_copy(update={"suspects": [], "runbook": None})
    incident = Incident(
        id="inc-1", alert=alert, status=IncidentStatus.INVESTIGATING, created_at=alert.triggered_at
    )
    text = compose_slack_brief(incident, triage)
    assert "none identified" in text
    assert "no strong match" in text
