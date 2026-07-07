from incident_response.agents.llm import FakeLLM
from incident_response.db import IncidentStore
from incident_response.integrations.github import MockGitHubClient
from incident_response.integrations.metrics import MockMetricsClient
from incident_response.integrations.slack import MockSlackClient
from incident_response.models import IncidentStatus
from incident_response.orchestrator import IncidentOrchestrator, OrchestratorConfig


def _build(tmp_db, postmortem_dir, runbooks_dir, llm):
    slack = MockSlackClient()
    orch = IncidentOrchestrator(
        llm=llm,
        github=MockGitHubClient(),
        slack=slack,
        metrics=MockMetricsClient(),
        store=IncidentStore(tmp_db),
        config=OrchestratorConfig(
            slack_channel="#incidents",
            runbooks_dir=runbooks_dir,
            postmortem_dir=postmortem_dir,
        ),
    )
    return orch, slack


async def test_handle_alert_posts_brief_and_saves_incident(
    alert, tmp_db, postmortem_dir, runbooks_dir
):
    llm = FakeLLM(
        [
            # triage → identify_suspects
            {
                "suspects": [
                    {"sha": "a1b2c3d", "confidence": 0.87, "reasoning": "checkout pricing"}
                ]
            },
            # runbook match
            {"slug": "checkout-error-rate", "confidence": 0.9, "reasoning": "spot on"},
            # impact estimate
            {
                "affected_users": 2200,
                "affected_percent": 17.7,
                "error_rate": 0.184,
                "reasoning": "18% * 12400",
            },
        ]
    )
    orch, slack = _build(tmp_db, postmortem_dir, runbooks_dir, llm)

    incident = await orch.handle_alert(alert)

    assert incident.status == IncidentStatus.INVESTIGATING
    assert incident.triage is not None
    assert incident.triage.suspects[0].commit.sha == "a1b2c3d"
    assert incident.triage.runbook.runbook.slug == "checkout-error-rate"
    assert incident.triage.impact.affected_users == 2200
    # 1st post: brief. 2nd: mock-executor remediation summary (checkout-error-rate has actions).
    assert len(slack.sent) == 2
    assert "SEV2" in slack.sent[0].text
    assert "a1b2c3d" in slack.sent[0].text
    assert slack.sent[1].thread_ts == slack.sent[0].ts
    assert "Automated remediation" in slack.sent[1].text
    assert incident.slack_message_ts is not None


async def test_resolve_generates_postmortem_and_threads_reply(
    alert, tmp_db, postmortem_dir, runbooks_dir
):
    llm = FakeLLM(
        [
            {"suspects": [{"sha": "a1b2c3d", "confidence": 0.9, "reasoning": "matches"}]},
            {"slug": "checkout-error-rate", "confidence": 0.9, "reasoning": "yes"},
            {"affected_users": 2200, "affected_percent": 17.7, "error_rate": 0.184, "reasoning": "..."},
            {
                "markdown": "# Post-Mortem\n\nSummary: rolled back pricing cache commit.\n"
            },
        ]
    )
    orch, slack = _build(tmp_db, postmortem_dir, runbooks_dir, llm)

    incident = await orch.handle_alert(alert)
    resolved = await orch.resolve(incident.id, resolution_note="rolled back a1b2c3d")

    assert resolved.status == IncidentStatus.RESOLVED
    assert resolved.resolved_at is not None
    assert resolved.postmortem_path is not None
    pm_text = open(resolved.postmortem_path).read()
    assert "Post-Mortem" in pm_text
    # 1: brief. 2: remediation summary. 3: post-mortem thread reply.
    assert len(slack.sent) == 3
    assert slack.sent[2].thread_ts == slack.sent[0].ts
    assert "Post-mortem" in slack.sent[2].text


async def test_resolve_unknown_incident_raises(tmp_db, postmortem_dir, runbooks_dir):
    llm = FakeLLM([])
    orch, _ = _build(tmp_db, postmortem_dir, runbooks_dir, llm)
    import pytest

    with pytest.raises(LookupError):
        await orch.resolve("does-not-exist")
