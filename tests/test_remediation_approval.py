from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from incident_response.agents.llm import FakeLLM
from incident_response.config import Settings
from incident_response.db import IncidentStore
from incident_response.executor import RemediationExecutor, StepResult
from incident_response.integrations.github import MockGitHubClient
from incident_response.integrations.metrics import MockMetricsClient
from incident_response.integrations.slack import MockSlackClient
from incident_response.main import create_app
from incident_response.models import (
    Alert,
    ImpactEstimate,
    Incident,
    IncidentStatus,
    Runbook,
    RunbookMatch,
    TriageReport,
)
from incident_response.orchestrator import IncidentOrchestrator, OrchestratorConfig


def _triage_responses() -> list[dict]:
    return [
        {"suspects": []},
        {"slug": "checkout-error-rate", "confidence": 0.93, "reasoning": "matches"},
        {
            "affected_users": 100,
            "affected_percent": 1.0,
            "error_rate": 0.1,
            "reasoning": "elevated checkout failures",
        },
    ]


class RecordingExecutor(RemediationExecutor):
    def __init__(self) -> None:
        self.calls: list[list] = []

    async def run(self, steps):
        captured = list(steps)
        self.calls.append(captured)
        return [
            StepResult(step=step, status="executed", stdout="ok", exit_code=0)
            for step in captured
        ]


def _build_orchestrator(
    *,
    tmp_db: Path,
    postmortem_dir: Path,
    runbooks_dir: Path,
    executor: RemediationExecutor,
) -> tuple[IncidentOrchestrator, MockSlackClient, IncidentStore]:
    store = IncidentStore(tmp_db)
    slack = MockSlackClient()
    orchestrator = IncidentOrchestrator(
        llm=FakeLLM(_triage_responses()),
        github=MockGitHubClient(),
        slack=slack,
        metrics=MockMetricsClient(),
        store=store,
        config=OrchestratorConfig(
            slack_channel="#incidents",
            runbooks_dir=runbooks_dir,
            postmortem_dir=postmortem_dir,
            verification_enabled=False,
        ),
        executor=executor,
    )
    return orchestrator, slack, store


async def test_runbook_remediation_waits_for_persisted_approval(
    alert, tmp_db, postmortem_dir, runbooks_dir
):
    executor = RecordingExecutor()
    orchestrator, slack, store = _build_orchestrator(
        tmp_db=tmp_db,
        postmortem_dir=postmortem_dir,
        runbooks_dir=runbooks_dir,
        executor=executor,
    )

    incident = await orchestrator.handle_alert(alert)

    assert executor.calls == []
    assert incident.remediation is not None
    assert incident.remediation.status.value == "pending"
    assert incident.remediation.runbook_slug == "checkout-error-rate"
    assert incident.remediation.steps
    assert store.get(incident.id).remediation.status.value == "pending"
    assert "approval required" in incident.timeline[-1]["event"].lower()
    assert "approval required" in slack.sent[-1].text.lower()


async def test_approval_executes_once_and_persists_operator_decision(
    alert, tmp_db, postmortem_dir, runbooks_dir
):
    executor = RecordingExecutor()
    orchestrator, _, store = _build_orchestrator(
        tmp_db=tmp_db,
        postmortem_dir=postmortem_dir,
        runbooks_dir=runbooks_dir,
        executor=executor,
    )
    incident = await orchestrator.handle_alert(alert)

    approved = await orchestrator.approve_remediation(
        incident.id,
        decided_by="alice@example.com",
        note="Rollback approved by primary on-call",
    )

    assert len(executor.calls) == 1
    assert approved.remediation.status.value == "completed"
    assert approved.remediation.decided_by == "alice@example.com"
    assert approved.remediation.note == "Rollback approved by primary on-call"
    assert approved.remediation.decided_at is not None
    assert "Remediation attempted:" in approved.timeline[-1]["event"]
    assert store.get(incident.id).remediation.status.value == "completed"

    with pytest.raises(ValueError, match="already"):
        await orchestrator.approve_remediation(
            incident.id,
            decided_by="second-operator",
        )
    assert len(executor.calls) == 1


async def test_rejection_is_final_and_never_executes(
    alert, tmp_db, postmortem_dir, runbooks_dir
):
    executor = RecordingExecutor()
    orchestrator, _, store = _build_orchestrator(
        tmp_db=tmp_db,
        postmortem_dir=postmortem_dir,
        runbooks_dir=runbooks_dir,
        executor=executor,
    )
    incident = await orchestrator.handle_alert(alert)

    rejected = await orchestrator.reject_remediation(
        incident.id,
        decided_by="incident-commander",
        note="Use the manual database failover instead",
    )

    assert rejected.remediation.status.value == "rejected"
    assert rejected.remediation.decided_by == "incident-commander"
    assert "Use the manual database failover instead" in rejected.timeline[-1]["event"]
    assert store.get(incident.id).remediation.status.value == "rejected"
    assert executor.calls == []

    with pytest.raises(ValueError, match="already"):
        await orchestrator.approve_remediation(
            incident.id,
            decided_by="late-approver",
        )
    assert executor.calls == []


async def test_approval_executes_the_persisted_proposal_if_runbook_changes(
    alert, tmp_db, postmortem_dir, runbooks_dir
):
    executor = RecordingExecutor()
    orchestrator, _, _ = _build_orchestrator(
        tmp_db=tmp_db,
        postmortem_dir=postmortem_dir,
        runbooks_dir=runbooks_dir,
        executor=executor,
    )
    incident = await orchestrator.handle_alert(alert)
    approved_commands = [step.command for step in incident.remediation.steps]

    original = orchestrator.get_runbook("checkout-error-rate")
    assert original is not None
    changed = original.model_copy(
        update={
            "content": (
                "## Automated actions\n```json\n"
                '[{"name":"changed after review","command":"deploy unexpected","auto":true}]\n'
                "```"
            )
        }
    )
    orchestrator._runbooks = [
        changed if runbook.slug == changed.slug else runbook
        for runbook in orchestrator._runbooks
    ]

    await orchestrator.approve_remediation(
        incident.id,
        decided_by="careful-operator",
    )

    assert [step.command for step in executor.calls[0]] == approved_commands


def _settings(tmp_path: Path, runbooks_dir: Path) -> Settings:
    return Settings(
        llm_mode="mock",
        github_mode="mock",
        slack_mode="mock",
        metrics_mode="mock",
        remediation_mode="mock",
        runbooks_dir=runbooks_dir,
        postmortem_dir=tmp_path / "postmortems",
        db_path=tmp_path / "incidents.db",
        webhook_token="secret",
        verification_enabled=False,
    )


def _pending_incident(alert: Alert) -> Incident:
    runbook = Runbook(
        slug="checkout-error-rate",
        title="Checkout elevated error rate",
        tags=["checkout"],
        content=(
            "## Automated actions\n```json\n"
            '[{"name":"rollback checkout","command":"deploy rollback checkout","auto":true}]\n'
            "```"
        ),
        path="runbooks/checkout-error-rate.md",
    )
    triage = TriageReport(
        suspects=[],
        runbook=RunbookMatch(runbook=runbook, confidence=0.9, reasoning="matches"),
        impact=ImpactEstimate(
            affected_users=100,
            affected_percent=1,
            error_rate=0.1,
            reasoning="elevated failures",
        ),
        summary="Rollback is recommended.",
    )
    return Incident(
        id="inc-pending-approval",
        alert=alert.model_copy(update={"id": "pending-approval"}),
        status=IncidentStatus.INVESTIGATING,
        created_at=datetime(2026, 7, 2, 21, 5, tzinfo=timezone.utc),
        triage=triage,
        remediation={
            "status": "pending",
            "runbook_slug": runbook.slug,
            "requested_at": "2026-07-02T21:06:00Z",
            "steps": [
                {
                    "name": "rollback checkout",
                    "command": "deploy rollback checkout",
                    "auto": True,
                }
            ],
        },
    )


def test_authenticated_api_can_approve_pending_remediation(
    tmp_path, runbooks_dir, alert
):
    settings = _settings(tmp_path, runbooks_dir)
    IncidentStore(settings.db_path).save(_pending_incident(alert))
    app = create_app(settings=settings, llm=FakeLLM([]))

    with TestClient(app) as client:
        unauthenticated = client.post(
            "/alerts/inc-pending-approval/remediation/approve",
            json={"decided_by": "api-operator", "note": "approved"},
        )
        approved = client.post(
            "/alerts/inc-pending-approval/remediation/approve",
            headers={"x-webhook-token": "secret"},
            json={"decided_by": "api-operator", "note": "approved"},
        )

    assert unauthenticated.status_code == 401
    assert approved.status_code == 200
    assert approved.json()["remediation"]["status"] == "completed"
    assert approved.json()["remediation"]["decided_by"] == "api-operator"


def test_console_renders_and_submits_pending_approval_actions(
    tmp_path, runbooks_dir, alert
):
    settings = _settings(tmp_path, runbooks_dir)
    IncidentStore(settings.db_path).save(_pending_incident(alert))
    app = create_app(settings=settings, llm=FakeLLM([]))

    with TestClient(app) as client:
        detail = client.get("/console/incidents/inc-pending-approval")
        approved = client.post(
            "/console/incidents/inc-pending-approval/remediation/approve",
            data={"note": "Approved locally"},
            follow_redirects=False,
        )

    assert "Approval required" in detail.text
    assert "deploy rollback checkout" in detail.text
    assert 'action="/console/incidents/inc-pending-approval/remediation/approve"' in detail.text
    assert 'action="/console/incidents/inc-pending-approval/remediation/reject"' in detail.text
    assert approved.status_code == 303
    assert approved.headers["location"] == "/console/incidents/inc-pending-approval"
