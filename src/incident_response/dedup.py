"""Alert deduplication.

Fingerprint = (service, metric, severity, time-bucket). Repeat fires within the
dedup window attach to the existing open incident as timeline events instead of
opening a new one.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .models import Alert


DEFAULT_BUCKET_MINUTES = 15
DEFAULT_TTL_SECONDS = 60 * 60  # 1 hour
DEFAULT_MAX_KEYS = 4096


class RedisKeyValueClient(Protocol):
    async def get(self, key: str) -> object:
        ...

    async def set(self, key: str, value: str, *, ex: int) -> object:
        ...

    async def delete(self, key: str) -> object:
        ...


def alert_fingerprint(alert: Alert, bucket_minutes: int = DEFAULT_BUCKET_MINUTES) -> str:
    bucket = int(alert.triggered_at.timestamp() // (bucket_minutes * 60))
    key = f"{alert.service}|{alert.metric or ''}|{alert.severity.value}|{bucket}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


@dataclass
class DedupIndex:
    """Bounded LRU: fingerprint → incident_id, with TTL-based expiry."""

    ttl_seconds: float = DEFAULT_TTL_SECONDS
    max_keys: int = DEFAULT_MAX_KEYS
    clock: Callable[[], float] = field(default=time.monotonic)
    _entries: "OrderedDict[str, tuple[str, float]]" = field(default_factory=OrderedDict)

    def get(self, fingerprint: str) -> str | None:
        entry = self._entries.get(fingerprint)
        if entry is None:
            return None
        incident_id, expires_at = entry
        if self.clock() >= expires_at:
            self._entries.pop(fingerprint, None)
            return None
        self._entries.move_to_end(fingerprint)
        return incident_id

    def set(self, fingerprint: str, incident_id: str) -> None:
        expires_at = self.clock() + self.ttl_seconds
        if fingerprint in self._entries:
            self._entries.move_to_end(fingerprint)
        self._entries[fingerprint] = (incident_id, expires_at)
        while len(self._entries) > self.max_keys:
            self._entries.popitem(last=False)

    def forget(self, fingerprint: str) -> None:
        self._entries.pop(fingerprint, None)


class RedisDedupIndex:
    """Cross-process fingerprint index backed by expiring Redis keys."""

    def __init__(
        self,
        redis: RedisKeyValueClient,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        namespace: str = "incident-response",
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._redis = redis
        self._ttl_seconds = max(1, math.ceil(ttl_seconds))
        self._prefix = f"{namespace}:dedup:"

    def _key(self, fingerprint: str) -> str:
        return f"{self._prefix}{fingerprint}"

    async def get(self, fingerprint: str) -> str | None:
        value = await self._redis.get(self._key(fingerprint))
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    async def set(self, fingerprint: str, incident_id: str) -> None:
        await self._redis.set(
            self._key(fingerprint),
            incident_id,
            ex=self._ttl_seconds,
        )

    async def forget(self, fingerprint: str) -> None:
        await self._redis.delete(self._key(fingerprint))
