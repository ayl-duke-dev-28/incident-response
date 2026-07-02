"""Alert deduplication.

Fingerprint = (service, metric, severity, time-bucket). Repeat fires within the
dedup window attach to the existing open incident as timeline events instead of
opening a new one.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

from .models import Alert


DEFAULT_BUCKET_MINUTES = 15
DEFAULT_TTL_SECONDS = 60 * 60  # 1 hour
DEFAULT_MAX_KEYS = 4096


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
