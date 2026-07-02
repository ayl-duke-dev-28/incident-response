from incident_response.rate_limit import SlidingWindowRateLimiter


def test_allows_up_to_max_then_denies():
    t = {"now": 0.0}
    limiter = SlidingWindowRateLimiter(max_events=3, window_seconds=10.0, clock=lambda: t["now"])
    assert limiter.check("k") is True
    assert limiter.check("k") is True
    assert limiter.check("k") is True
    assert limiter.check("k") is False


def test_window_slides():
    t = {"now": 0.0}
    limiter = SlidingWindowRateLimiter(max_events=2, window_seconds=5.0, clock=lambda: t["now"])
    assert limiter.check("k") is True
    assert limiter.check("k") is True
    assert limiter.check("k") is False
    t["now"] = 6.0
    assert limiter.check("k") is True


def test_keys_are_independent():
    limiter = SlidingWindowRateLimiter(max_events=1, window_seconds=10.0, clock=lambda: 0.0)
    assert limiter.check("a") is True
    assert limiter.check("a") is False
    assert limiter.check("b") is True
