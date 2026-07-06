"""Integration tests covering the three new features end-to-end."""

from pathlib import Path

from incident_response.agents.llm import FakeLLM
from incident_response.db import IncidentStore
from incident_response.executor import (
    MockExecutor,
    RemediationExecutor,
    StepResult,
)
from incident_response.integrations.github import MockGitHubClient
from incident_response.integrations.metrics import MockMetricsClient
from incident_response.integrations.slack import MockSlackClient
from incident_response.orchestrator import IncidentOrchestrator, OrchestratorConfig


def _triage_responses():
    return [
        {"suspects": [{"sha": "a1b2c3d", "confidence": 0.9, "reasoning": "checkout"}]},
        {"slug": "checkout-error-rate", "confidence": 0.9, "reasoning": "yes"},
        {"affected_users": 100, "affected_percent": 1.0, "error_rate": 0.01, "reasoning": "."},
    ]


def _build(*, llm: FakeLLM, tmp_db: Path, pm_dir: Path, runbooks_dir: Path,
           executor: RemediationExecutor | None = None,
           verification_enabled: bool = False) -> tuple[IncidentOrchestrator, MockSlackClient]:
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
            postmortem_dir=pm_dir,
            verification_enabled=verification_enabled,
            verification_total_minutes=1,
            verification_poll_seconds=1,
        ),
        executor=executor or MockExecutor(),
    )
    return orch, slack


async def test_history_context_flows_into_triage_prompt(alert, tmp_db, runbooks_dir, tmp_path):
    pm_dir = tmp_path / "pm"
    pm_dir.mkdir()
    (pm_dir / "2026-06-10-inc-checkout-1.md").write_text(
        "# Prior checkout outage\n\n**Service:** `checkout`\n\n"
        "## Root Cause\nRedis maxmemory eviction under load, cache TTL too long.\n",
        encoding="utf-8",
    )
    llm = FakeLLM(_triage_responses())
    orch, _ = _build(llm=llm, tmp_db=tmp_db, pm_dir=pm_dir, runbooks_dir=runbooks_dir)
    await orch.handle_alert(alert)

    # First LLM call was the triage prompt. Check the user message carried the history block.
    triage_call = llm.calls[0]
    _, user_msg = triage_call
    assert "Prior similar incidents" in user_msg
    assert "Redis" in user_msg
    assert "checkout" in user_msg


async def test_streaming_updates_fire_during_triage(alert, tmp_db, runbooks_dir, tmp_path):
    pm_dir = tmp_path / "pm"
    pm_dir.mkdir()
    llm = FakeLLM(_triage_responses())
    orch, slack = _build(llm=llm, tmp_db=tmp_db, pm_dir=pm_dir, runbooks_dir=runbooks_dir)
    await orch.handle_alert(alert)

    # At least three streaming updates (one per agent completion) plus the final rewrite.
    assert len(slack.updates) >= 3
    # Initial placeholder showed all three "investigating…" markers.
    first_update = slack.updates[0]
    assert first_update.ts == slack.sent[0].ts
    # Final visible text should be the fully rendered brief.
    final = slack.latest_text_for(slack.sent[0].ts)
    assert final is not None
    assert ":hourglass_flowing_sand:" not in final
    assert "a1b2c3d" in final


class _AlwaysExecuteExecutor(RemediationExecutor):
    """Executor that reports every step as executed, so verification is scheduled."""

    async def run(self, steps):
        return [
            StepResult(step=s, status="executed", stdout="ok", exit_code=0) for s in steps
        ]


async def test_verification_scheduled_after_real_execution(alert, tmp_db, runbooks_dir, tmp_path):
    pm_dir = tmp_path / "pm"
    pm_dir.mkdir()
    llm = FakeLLM(_triage_responses())
    store = IncidentStore(tmp_db)
    slack = MockSlackClient()
    orch = IncidentOrchestrator(
        llm=llm,
        github=MockGitHubClient(),
        slack=slack,
        metrics=MockMetricsClient(),
        store=store,
        config=OrchestratorConfig(
            slack_channel="#x",
            runbooks_dir=runbooks_dir,
            postmortem_dir=pm_dir,
            verification_enabled=True,
            verification_total_minutes=1,
            verification_poll_seconds=1,
        ),
        executor=_AlwaysExecuteExecutor(),
    )
    incident = await orch.handle_alert(alert)

    # Drain the fire-and-forget verification task (poll_seconds=1, total_minutes=1)
    import asyncio

    def _is_verdict(text: str) -> bool:
        return any(k in text for k in ("Recovered", "Still elevated", "Improving"))

    for _ in range(80):
        if any(_is_verdict(m.text) for m in slack.sent):
            break
        await asyncio.sleep(0.05)

    verification_posts = [m for m in slack.sent if _is_verdict(m.text)]
    assert verification_posts, "verification should have posted a result"
    assert verification_posts[0].thread_ts == slack.sent[0].ts

    # The outcome must land on the persisted incident record — not just Slack.
    # Poll briefly for the async task to complete and write back to the store.
    for _ in range(40):
        persisted = store.get(incident.id)
        if persisted and persisted.verification_outcome is not None:
            break
        await asyncio.sleep(0.05)
    persisted = store.get(incident.id)
    assert persisted is not None
    assert persisted.verification_outcome is not None
    assert persisted.verification_outcome.status in (
        "recovered", "improving", "still_elevated", "no_baseline",
    )
    # Runbook slug is captured so future retrieval can group by "which runbook worked."
    assert persisted.verification_outcome.runbook_slug == "checkout-error-rate"


async def test_verification_skipped_when_no_real_execution(alert, tmp_db, runbooks_dir, tmp_path):
    """MockExecutor's dry-run status must NOT trigger a verification loop."""

    pm_dir = tmp_path / "pm"
    pm_dir.mkdir()
    llm = FakeLLM(_triage_responses())
    orch, slack = _build(
        llm=llm, tmp_db=tmp_db, pm_dir=pm_dir, runbooks_dir=runbooks_dir,
        verification_enabled=True,
    )
    await orch.handle_alert(alert)

    # Give any spurious task a chance to run
    import asyncio
    await asyncio.sleep(0.1)
    assert not any("recovered" in m.text.lower() or "still elevated" in m.text.lower() for m in slack.sent)
