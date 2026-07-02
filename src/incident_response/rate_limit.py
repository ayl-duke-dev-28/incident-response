"""Sliding-window rate limiter with monotonic clock injection.

In-memory only — fine for a single-instance deployment. For multi-instance, back this
with Redis (implement RateLimiter with the same shape).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque


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
