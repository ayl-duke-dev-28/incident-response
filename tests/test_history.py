from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from incident_response.history import PostmortemHistory, format_for_prompt


def _write_pm(directory: Path, name: str, body: str, mtime: datetime | None = None) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    if mtime is not None:
        ts = mtime.timestamp()
        import os

        os.utime(path, (ts, ts))
    return path


@pytest.fixture
def pm_dir(tmp_path: Path) -> Path:
    d = tmp_path / "postmortems"
    d.mkdir()
    return d


def test_load_returns_empty_when_directory_missing(tmp_path: Path):
    history = PostmortemHistory.load(tmp_path / "nope")
    assert history.incidents == []
    assert history.search(service="x", query="y") == []


def test_load_parses_service_and_root_cause(pm_dir: Path):
    _write_pm(
        pm_dir,
        "2026-06-10-inc-checkout-1.md",
        "# Post-Mortem: Checkout 5xx spike\n\n"
        "**Service:** `checkout`\n\n"
        "## Summary\nBad pricing cache deploy.\n\n"
        "## Root Cause\nRedis maxmemory eviction under load; new cache TTL was too long.\n\n"
        "## Action Items\n- Add cache size alert (P1)\n",
    )
    history = PostmortemHistory.load(pm_dir)
    assert len(history.incidents) == 1
    inc = history.incidents[0]
    assert inc.service == "checkout"
    assert "Redis" in inc.root_cause
    assert "cache" in inc.tokens


def test_search_prefers_same_service_and_shared_keywords(pm_dir: Path):
    _write_pm(
        pm_dir,
        "2026-06-10-inc-checkout-1.md",
        "# Checkout Redis outage\n\n**Service:** `checkout`\n\n## Root Cause\n"
        "Redis maxmemory eviction under load spikes.\n",
    )
    _write_pm(
        pm_dir,
        "2026-05-01-inc-auth-1.md",
        "# Auth login timeout\n\n**Service:** `auth`\n\n## Root Cause\n"
        "Session store latency during OAuth token exchange.\n",
    )
    history = PostmortemHistory.load(pm_dir)
    matches = history.search(
        service="checkout",
        query="checkout 5xx spike error rate Redis pricing cache",
        now=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    assert matches
    assert matches[0].incident.service == "checkout"
    assert matches[0].score > 0.5


def test_search_recency_breaks_ties(pm_dir: Path):
    now = datetime(2026, 7, 2, tzinfo=timezone.utc)
    _write_pm(
        pm_dir,
        "2020-01-01-inc-checkout-old.md",
        "# Old\n**Service:** `checkout`\n\n## Root Cause\nRedis eviction.\n",
        mtime=now - timedelta(days=400),
    )
    _write_pm(
        pm_dir,
        "2026-06-01-inc-checkout-new.md",
        "# New\n**Service:** `checkout`\n\n## Root Cause\nRedis eviction.\n",
        mtime=now - timedelta(days=30),
    )
    history = PostmortemHistory.load(pm_dir)
    matches = history.search(
        service="checkout", query="Redis eviction", now=now
    )
    assert matches[0].incident.filename == "2026-06-01-inc-checkout-new.md"


def test_search_returns_empty_below_min_score(pm_dir: Path):
    _write_pm(
        pm_dir,
        "2026-01-01-inc-auth-1.md",
        "# Auth\n**Service:** `auth`\n\n## Root Cause\nnothing related\n",
    )
    history = PostmortemHistory.load(pm_dir)
    matches = history.search(
        service="unrelated-service", query="totally different topic", min_score=0.9
    )
    assert matches == []


def test_format_for_prompt_is_readable():
    from incident_response.history import HistoricalIncident, HistoryMatch

    inc = HistoricalIncident(
        path="/x.md",
        filename="x.md",
        service="checkout",
        title="Checkout 5xx",
        root_cause="Redis maxmemory eviction",
        tokens=frozenset(),
        written_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    text = format_for_prompt([HistoryMatch(incident=inc, score=0.83)])
    assert "checkout" in text
    assert "Redis" in text
    assert "0.83" in text
    assert "2026-06-01" in text


def test_to_prior_incident_maps_fields():
    from incident_response.history import HistoricalIncident, HistoryMatch, to_prior_incident

    inc = HistoricalIncident(
        path="postmortems/2026-06-10-inc-checkout-1.md",
        filename="2026-06-10-inc-checkout-1.md",
        service="checkout",
        title="Checkout Redis outage",
        root_cause="Redis maxmemory eviction under load; new cache TTL was too long.",
        tokens=frozenset(),
        written_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
    )
    prior = to_prior_incident(HistoryMatch(incident=inc, score=0.837))
    assert prior.title == "Checkout Redis outage"
    assert prior.service == "checkout"
    assert prior.date == "2026-06-10"
    assert prior.postmortem_path == "postmortems/2026-06-10-inc-checkout-1.md"
    assert "Redis maxmemory" in prior.root_cause
    assert prior.score == pytest.approx(0.837, abs=0.001)
