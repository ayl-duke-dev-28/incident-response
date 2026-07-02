import pytest

from incident_response.retry import async_retry


async def test_retry_succeeds_after_transient_failure():
    calls = {"n": 0}

    @async_retry(attempts=3, base_delay=0.001, jitter=0.0)
    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("nope")
        return "ok"

    assert await flaky() == "ok"
    assert calls["n"] == 3


async def test_retry_raises_after_exhausting_attempts():
    @async_retry(attempts=2, base_delay=0.001, jitter=0.0)
    async def always_fail() -> None:
        raise ConnectionError("permanent")

    with pytest.raises(ConnectionError):
        await always_fail()


async def test_retry_does_not_swallow_non_retryable():
    @async_retry(attempts=3, base_delay=0.001, jitter=0.0, retry_on=(ConnectionError,))
    async def raises_value_error() -> None:
        raise ValueError("bug")

    with pytest.raises(ValueError):
        await raises_value_error()
