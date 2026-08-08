"""Sliding-window rate limiter with monotonic clock injection.

In-memory only — fine for a single-instance deployment. For multi-instance, back this
with Redis (implement RateLimiter with the same shape).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Protocol


class RedisScriptClient(Protocol):
    async def eval(self, script: str, numkeys: int, *args: object) -> object:
        ...


_SLIDING_WINDOW_SCRIPT = """
local redis_time = redis.call('TIME')
local now = (redis_time[1] * 1000) + math.floor(redis_time[2] / 1000)
local window = tonumber(ARGV[1])
local maximum = tonumber(ARGV[2])
local cutoff = now - window

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff)
local count = redis.call('ZCARD', KEYS[1])
if count >= maximum then
    return {0, 0}
end

local member = redis_time[1] .. ':' .. redis_time[2] .. ':' .. count
redis.call('ZADD', KEYS[1], now, member)
redis.call('PEXPIRE', KEYS[1], window)
return {1, maximum - count - 1}
"""


@dataclass
class SlidingWindowRateLimiter:
    """Allow at most `max_events` per `window_seconds` per key."""

    max_events: int
    window_seconds: float
    clock: Callable[[], float] = field(default=time.monotonic)
    _hits: dict[str, Deque[float]] = field(default_factory=dict)

    def check(self, key: str) -> bool:
        """Return True if allowed, False if rate-limited."""

        now = self.clock()
        window_start = now - self.window_seconds
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] < window_start:
            hits.popleft()
        if len(hits) >= self.max_events:
            return False
        hits.append(now)
        return True

    def remaining(self, key: str) -> int:
        return max(0, self.max_events - len(self._hits.get(key, ())))


class RedisSlidingWindowRateLimiter:
    """Atomic cross-process sliding window backed by one Redis sorted set."""

    def __init__(
        self,
        redis: RedisScriptClient,
        *,
        max_events: int,
        window_seconds: float,
        namespace: str = "incident-response",
    ) -> None:
        if max_events < 1:
            raise ValueError("max_events must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._redis = redis
        self._max_events = max_events
        self._window_ms = max(1, round(window_seconds * 1000))
        self._prefix = f"{namespace}:rate-limit:"

    async def check(self, key: str) -> bool:
        result = await self._redis.eval(
            _SLIDING_WINDOW_SCRIPT,
            1,
            f"{self._prefix}{key}",
            self._window_ms,
            self._max_events,
        )
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise RuntimeError("Redis rate-limit script returned an invalid result")
        return bool(int(result[0]))
