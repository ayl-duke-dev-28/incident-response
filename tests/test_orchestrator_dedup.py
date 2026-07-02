from incident_response.agents.llm import FakeLLM
from incident_response.db import IncidentStore
from incident_response.dedup import DedupIndex
from incident_response.executor import MockExecutor
from incident_response.integrations.github import MockGitHubClient
from incident_response.integrations.metrics import MockMetricsClient
from incident_response.integrations.slack import MockSlackClient
from incident_response.orchestrator import IncidentOrchestrator, OrchestratorConfig


def _triage_responses():
    return [
        {"suspects": [{"sha": "a1b2c3d", "confidence": 0.9, "reasoning": "checkout"}]},
        {"slug": "checkout-error-rate", "confidence": 0.9, "reasoning": "match"},
        {"affected_users": 100, "affected_percent": 1.0, "error_rate": 0.01, "reasoning": "..."},
    ]


async def test_duplicate_alert_within_bucket_attaches_to_existing(alert, tmp_db, postmortem_dir, runbooks_dir):
    llm = FakeLLM(_triage_responses())
    slack = MockSlackClient()
    orch = IncidentOrchestrator(
        llm=llm,
        github=MockGitHubClient(),
        slack=slack,
        metrics=MockMetricsClient(),
        store=IncidentStore(tmp_db),
        config=OrchestratorConfig(
            slack_channel="#x",
            runbooks_dir=runbooks_dir,
            postmortem_dir=postmortem_dir,
        ),
        dedup=DedupIndex(),
        executor=MockExecutor(),
    )
    first = await orch.handle_alert(alert)
    posts_after_first = len(slack.sent)
    duplicate = alert.model_copy(update={"id": "different-id"})
    second = await orch.handle_alert(duplicate)

    # Same incident returned, no additional Slack posts (dedup path bypasses brief).
    assert second.id == first.id
    assert len(slack.sent) == posts_after_first
    # Timeline captured the duplicate.
    assert any("Duplicate" in e["event"] for e in second.timeline)
