from incident_response.agents.llm import FakeLLM
from incident_response.agents.runbook import match_runbook
from incident_response.runbooks_loader import load_runbooks


async def test_matches_runbook_by_slug(alert, runbooks_dir):
    runbooks = load_runbooks(runbooks_dir)
    llm = FakeLLM([{"slug": "checkout-error-rate", "confidence": 0.92, "reasoning": "5xx spike"}])
    match = await match_runbook(llm, alert, runbooks)
    assert match is not None
    assert match.runbook.slug == "checkout-error-rate"
    assert match.confidence == 0.92


async def test_returns_none_when_low_confidence(alert, runbooks_dir):
    runbooks = load_runbooks(runbooks_dir)
    llm = FakeLLM([{"slug": "checkout-error-rate", "confidence": 0.1, "reasoning": "weak"}])
    assert await match_runbook(llm, alert, runbooks) is None


async def test_returns_none_when_unknown_slug(alert, runbooks_dir):
    runbooks = load_runbooks(runbooks_dir)
    llm = FakeLLM([{"slug": "made-up", "confidence": 0.9, "reasoning": "hallucinated"}])
    assert await match_runbook(llm, alert, runbooks) is None
