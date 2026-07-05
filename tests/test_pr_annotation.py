"""PR annotation: when triage nominates a high-confidence suspect with a PR
number, post a comment on that PR linking to the incident and prior similar
incidents. Failures never break the incident flow."""

from datetime import datetime, timezone

from incident_response.agents.llm import FakeLLM
from incident_response.db import IncidentStore
from incident_response.executor import MockExecutor
from incident_response.integrations.github import MockGitHubClient
from incident_response.integrations.metrics import MockMetricsClient
from incident_response.integrations.slack import MockSlackClient
from incident_response.models import Commit, PriorIncident
from incident_response.orchestrator import IncidentOrchestrator, OrchestratorConfig
from incident_response.pr_annotation import compose_pr_annotation


def _high_confidence_responses():
    return [
        {"suspects": [{"sha": "a1b2c3d", "confidence": 0.9, "reasoning": "deployed 8m before alert"}]},
        {"slug": "checkout-error-rate", "confidence": 0.9, "reasoning": "matches"},
        {"affected_users": 2100, "affected_percent": 17.0, "error_rate": 0.18, "reasoning": "spike"},
    ]


def _low_confidence_responses():
    return [
        {"suspects": [{"sha": "a1b2c3d", "confidence": 0.4, "reasoning": "weak signal"}]},
        {"slug": "checkout-error-rate", "confidence": 0.9, "reasoning": "matches"},
        {"affected_users": 2100, "affected_percent": 17.0, "error_rate": 0.18, "reasoning": "spike"},
    ]


def _build(*, llm, tmp_db, pm_dir, runbooks_dir, github, pr_annotate_enabled=True,
           pr_annotate_confidence_floor=0.75):
    slack = MockSlackClient()
    orch = IncidentOrchestrator(
        llm=llm,
        github=github,
        slack=slack,
        metrics=MockMetricsClient(),
        store=IncidentStore(tmp_db),
        config=OrchestratorConfig(
            slack_channel="#x",
            runbooks_dir=runbooks_dir,
            postmortem_dir=pm_dir,
            verification_enabled=False,
            pr_annotate_enabled=pr_annotate_enabled,
            pr_annotate_confidence_floor=pr_annotate_confidence_floor,
        ),
        executor=MockExecutor(),
    )
    return orch, slack


async def test_pr_annotated_when_top_suspect_high_confidence(alert, tmp_db, runbooks_dir, tmp_path):
    pm_dir = tmp_path / "pm"
    pm_dir.mkdir()
    github = MockGitHubClient()
    orch, _ = _build(
        llm=FakeLLM(_high_confidence_responses()),
        tmp_db=tmp_db, pm_dir=pm_dir, runbooks_dir=runbooks_dir, github=github,
    )
    await orch.handle_alert(alert)

    assert len(github.annotations) == 1
    pr_number, body = github.annotations[0]
    assert pr_number == 4821  # from mock github fixture
    assert "inc-ddg-9273" in body
    assert "checkout" in body


async def test_pr_not_annotated_when_below_confidence_floor(alert, tmp_db, runbooks_dir, tmp_path):
    pm_dir = tmp_path / "pm"
    pm_dir.mkdir()
    github = MockGitHubClient()
    orch, _ = _build(
        llm=FakeLLM(_low_confidence_responses()),
        tmp_db=tmp_db, pm_dir=pm_dir, runbooks_dir=runbooks_dir, github=github,
    )
    await orch.handle_alert(alert)

    assert github.annotations == []


async def test_pr_not_annotated_when_disabled(alert, tmp_db, runbooks_dir, tmp_path):
    pm_dir = tmp_path / "pm"
    pm_dir.mkdir()
    github = MockGitHubClient()
    orch, _ = _build(
        llm=FakeLLM(_high_confidence_responses()),
        tmp_db=tmp_db, pm_dir=pm_dir, runbooks_dir=runbooks_dir, github=github,
        pr_annotate_enabled=False,
    )
    await orch.handle_alert(alert)

    assert github.annotations == []


async def test_pr_not_annotated_when_suspect_has_no_pr_number(alert, tmp_db, runbooks_dir, tmp_path):
    """A commit missing pr_number can't be annotated. Skip silently."""

    pm_dir = tmp_path / "pm"
    pm_dir.mkdir()
    # Rig the fixture so the top suspect has no PR number.
    commits = [
        Commit(
            sha="pr-less",
            author="ghost",
            message="hotfix: bypass PR",
            timestamp=datetime(2026, 7, 5, 20, 55, tzinfo=timezone.utc),
        )
    ]
    github = MockGitHubClient(commits=commits)
    responses = [
        {"suspects": [{"sha": "pr-less", "confidence": 0.95, "reasoning": "direct push"}]},
        {"slug": "checkout-error-rate", "confidence": 0.9, "reasoning": "matches"},
        {"affected_users": 2100, "affected_percent": 17.0, "error_rate": 0.18, "reasoning": "spike"},
    ]
    orch, _ = _build(
        llm=FakeLLM(responses),
        tmp_db=tmp_db, pm_dir=pm_dir, runbooks_dir=runbooks_dir, github=github,
    )
    await orch.handle_alert(alert)

    assert github.annotations == []


class _AnnotationFailingGitHub(MockGitHubClient):
    """Github client whose annotate_pr always raises."""

    async def annotate_pr(self, pr_number: int, body: str) -> None:
        raise RuntimeError("github api down")


async def test_annotation_failure_does_not_break_incident_flow(alert, tmp_db, runbooks_dir, tmp_path):
    pm_dir = tmp_path / "pm"
    pm_dir.mkdir()
    github = _AnnotationFailingGitHub()
    orch, slack = _build(
        llm=FakeLLM(_high_confidence_responses()),
        tmp_db=tmp_db, pm_dir=pm_dir, runbooks_dir=runbooks_dir, github=github,
    )
    incident = await orch.handle_alert(alert)

    # Incident still completed with a Slack brief; failure did not propagate.
    assert incident.triage is not None
    assert slack.sent, "the brief should still have been posted"


def test_compose_pr_annotation_includes_prior_incidents():
    commit = Commit(
        sha="a1b2c3d",
        author="jamie",
        message="perf: switch pricing to new cache",
        timestamp=datetime(2026, 7, 5, 20, 55, tzinfo=timezone.utc),
        pr_number=4821,
    )
    priors = [
        PriorIncident(
            title="Checkout Redis outage",
            service="checkout",
            date="2026-06-10",
            root_cause="Redis maxmemory eviction; new cache TTL was too long.",
            score=0.83,
            postmortem_path="postmortems/2026-06-10-inc-checkout-1.md",
        )
    ]
    body = compose_pr_annotation(
        incident_id="inc-ddg-9273",
        service="checkout",
        severity="sev2",
        suspect_commit=commit,
        suspect_confidence=0.87,
        suspect_reasoning="deployed 8m before alert",
        affected_users=2232,
        prior_incidents=priors,
    )
    assert "inc-ddg-9273" in body
    assert "checkout" in body
    assert "sev2" in body.lower() or "SEV2" in body
    assert "87%" in body
    assert "2,232" in body
    assert "Checkout Redis outage" in body
    assert "2026-06-10" in body
    assert "Redis maxmemory eviction" in body


def test_compose_pr_annotation_omits_prior_section_when_empty():
    commit = Commit(
        sha="a1b2c3d",
        author="jamie",
        message="perf: cache",
        timestamp=datetime(2026, 7, 5, tzinfo=timezone.utc),
        pr_number=1,
    )
    body = compose_pr_annotation(
        incident_id="inc-x",
        service="checkout",
        severity="sev3",
        suspect_commit=commit,
        suspect_confidence=0.9,
        suspect_reasoning="matches",
        affected_users=10,
        prior_incidents=[],
    )
    assert "Prior similar incidents" not in body
