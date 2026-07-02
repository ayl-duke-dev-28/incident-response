"""HMAC signature verification for inbound webhooks.

Two formats supported out-of-the-box:
- Datadog:  header `X-Datadog-Signature` = base64(hmac_sha256(secret, body))
- PagerDuty: header `X-PagerDuty-Signature` = "v1=<hex>,..." — we verify any v1 match.

The shared-token check (X-Webhook-Token) is retained as a cheap first line of defense.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Iterable


def verify_datadog(secret: str, body: bytes, signature: str) -> bool:
    if not secret or not signature:
        return False
    expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, signature)


def verify_pagerduty(secret: str, body: bytes, signature_header: str) -> bool:
    """PagerDuty rotates signing keys, sending multiple `v1=` entries. Match any."""

    if not secret or not signature_header:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    for entry in _parse_pd_header(signature_header):
        if hmac.compare_digest(entry, expected):
            return True
    return False


def _parse_pd_header(header: str) -> Iterable[str]:
    for chunk in header.split(","):
        chunk = chunk.strip()
        if chunk.startswith("v1="):
            yield chunk[3:]


def verify_generic_hmac(secret: str, body: bytes, signature_hex: str) -> bool:
    """Plain hex-encoded HMAC-SHA256 — useful for custom senders."""

    if not secret or not signature_hex:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_hex.strip().lower())
