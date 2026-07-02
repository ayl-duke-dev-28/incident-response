from datetime import datetime, timedelta, timezone

import pytest

from incident_response.agents.llm import FakeLLM
from incident_response.agents.triage import identify_suspects
from incident_response.models import Commit


@pytest.fixture
def commits():
    now = datetime(2026, 7, 2, 21, 0, tzinfo=timezone.utc)
    return [
        Commit(
            sha="aaaa111",
            author="jamie",
            message="perf: switch checkout pricing to new cache",
            timestamp=now - timedelta(minutes=5),
            files_changed=["services/checkout/pricing.py"],
            additions=142,
            deletions=37,
        ),
        Commit(
            sha="bbbb222",
            author="dana",
            message="docs: update README",
            timestamp=now - timedelta(hours=1),
            files_changed=["README.md"],
            additions=3,
            deletions=1,
        ),
    ]


async def test_identify_suspects_maps_sha_to_commit(alert, commits):
    llm = FakeLLM(
        [
            {
                "suspects": [
                    {"sha": "aaaa111", "confidence": 0.88, "reasoning": "recent, touches checkout"},
                    {"sha": "bbbb222", "confidence": 0.05, "reasoning": "docs only"},
                ]
            }
        ]
    )
    suspects = await identify_suspects(llm, alert, commits)
    assert len(suspects) == 2
    assert suspects[0].commit.sha == "aaaa111"
    assert suspects[0].confidence == 0.88
    assert "checkout" in suspects[0].reasoning


async def test_identify_suspects_tolerates_short_shas(alert, commits):
    llm = FakeLLM([{"suspects": [{"sha": "aaaa", "confidence": 0.7, "reasoning": "match"}]}])
    suspects = await identify_suspects(llm, alert, commits)
    assert suspects[0].commit.sha == "aaaa111"


async def test_identify_suspects_empty_when_no_commits(alert):
    llm = FakeLLM([])
    assert await identify_suspects(llm, alert, []) == []


async def test_identify_suspects_drops_unknown_sha(alert, commits):
    llm = FakeLLM([{"suspects": [{"sha": "zzz", "confidence": 0.5, "reasoning": "mystery"}]}])
    assert await identify_suspects(llm, alert, commits) == []
