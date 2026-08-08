from incident_response.dedup import RedisDedupIndex
from incident_response.rate_limit import RedisSlidingWindowRateLimiter


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}
        self.eval_results: list[list[int]] = []
        self.eval_calls: list[tuple[str, int, tuple[object, ...]]] = []

    async def eval(self, script: str, numkeys: int, *args: object) -> list[int]:
        self.eval_calls.append((script, numkeys, args))
        return self.eval_results.pop(0)

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, *, ex: int) -> bool:
        self.values[key] = value
        self.expirations[key] = ex
        return True

    async def delete(self, key: str) -> int:
        existed = key in self.values
        self.values.pop(key, None)
        self.expirations.pop(key, None)
        return int(existed)


async def test_redis_rate_limit_uses_one_atomic_script_and_shared_namespace():
    redis = FakeRedis()
    redis.eval_results = [[1, 1], [0, 0]]
    first = RedisSlidingWindowRateLimiter(
        redis,
        max_events=2,
        window_seconds=10,
        namespace="incident-response:test",
    )
    second = RedisSlidingWindowRateLimiter(
        redis,
        max_events=2,
        window_seconds=10,
        namespace="incident-response:test",
    )

    assert await first.check("10.0.0.1|checkout") is True
    assert await second.check("10.0.0.1|checkout") is False

    assert len(redis.eval_calls) == 2
    script, numkeys, args = redis.eval_calls[0]
    assert "redis.call('TIME')" in script
    assert numkeys == 1
    assert args[0] == "incident-response:test:rate-limit:10.0.0.1|checkout"
    assert args[1:] == (10_000, 2)


async def test_redis_dedup_is_shared_expiring_namespaced_and_clearable():
    redis = FakeRedis()
    first = RedisDedupIndex(
        redis,
        ttl_seconds=90,
        namespace="incident-response:test",
    )
    second = RedisDedupIndex(
        redis,
        ttl_seconds=90,
        namespace="incident-response:test",
    )

    await first.set("fingerprint", "inc-123")

    key = "incident-response:test:dedup:fingerprint"
    assert await second.get("fingerprint") == "inc-123"
    assert redis.expirations[key] == 90

    await second.forget("fingerprint")
    assert await first.get("fingerprint") is None
