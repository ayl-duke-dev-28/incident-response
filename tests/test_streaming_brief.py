from datetime import datetime, timezone

from incident_response.agents.brief import compose_streaming_brief
from incident_response.models import (
    Alert,
    Commit,
    ImpactEstimate,
    PriorIncident,
    Runbook,
    RunbookMatch,
    Severity,
    SuspectCommit,
)


def _alert() -> Alert:
    return Alert(
        id="a1",
        title="Checkout 5xx",
        service="checkout",
        severity=Severity.SEV2,
        triggered_at=datetime(2026, 7, 2, 21, 5, tzinfo=timezone.utc),
        metric="http.error_rate",
    )


def test_placeholder_brief_shows_all_three_slots_pending():
    text = compose_streaming_brief(_alert())
    assert ":rotating_light:" in text
    # Three "investigating…" placeholders (impact, suspects, runbook)
    assert text.count(":hourglass_flowing_sand:") == 3
    assert "SEV2" in text


def test_brief_updates_as_slots_fill():
    impact = ImpactEstimate(
        affected_users=2100, affected_percent=17.0, error_rate=0.18, reasoning="17% of active"
    )
    text = compose_streaming_brief(_alert(), impact=impact)
    assert "2,100 users" in text
    assert "17.0%" in text
    # Suspects and runbook still pending
    assert text.count(":hourglass_flowing_sand:") == 2


def test_complete_brief_uses_checkmark_and_shows_all_fields():
    commit = Commit(
        sha="a1b2c3d4",
        author="jamie",
        message="perf: switch checkout to new pricing cache",
        timestamp=datetime(2026, 7, 2, 20, 55, tzinfo=timezone.utc),
    )
    text = compose_streaming_brief(
        _alert(),
        suspects=[SuspectCommit(commit=commit, confidence=0.85, reasoning="matches")],
        runbook=RunbookMatch(
            runbook=Runbook(
                slug="x", title="Checkout runbook", tags=[], content="", path="runbooks/x.md"
            ),
            confidence=0.9,
            reasoning="matches",
        ),
        impact=ImpactEstimate(
            affected_users=2100, affected_percent=17.0, error_rate=0.18, reasoning="."
        ),
        complete=True,
    )
    assert ":white_check_mark:" in text
    assert ":hourglass_flowing_sand:" not in text
    assert "a1b2c3d4" in text
    assert "Checkout runbook" in text


def test_streaming_brief_renders_prior_incidents_when_present():
    priors = [
        PriorIncident(
            title="Checkout Redis outage",
            service="checkout",
            date="2026-06-10",
            root_cause="Redis maxmemory eviction.",
            score=0.83,
            postmortem_path="postmortems/2026-06-10-inc-checkout-1.md",
        )
    ]
    text = compose_streaming_brief(_alert(), prior_incidents=priors)
    assert "Prior similar incidents" in text
    assert "Checkout Redis outage" in text
    assert "2026-06-10" in text
