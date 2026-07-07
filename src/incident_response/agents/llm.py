"""Thin wrapper around the Anthropic SDK that always returns parsed JSON.

The agents share this so their prompts stay focused on domain logic rather than plumbing.
A `FakeLLM` is provided for tests — dependency-injected wherever the real client is used.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any

from anthropic import AsyncAnthropic, APIConnectionError, APIStatusError, RateLimitError

from ..retry import async_retry

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_RETRYABLE_ANTHROPIC = (APIConnectionError, RateLimitError, APIStatusError, TimeoutError)


class LLM(ABC):
    @abstractmethod
    async def json(
        self, *, system: str, user: str, max_tokens: int = 1024
    ) -> dict[str, Any]:
        ...


class AnthropicLLM(LLM):
    def __init__(self, api_key: str, model: str) -> None:
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model

    @async_retry(attempts=4, base_delay=0.75, retry_on=_RETRYABLE_ANTHROPIC)
    async def json(self, *, system: str, user: str, max_tokens: int = 1024) -> dict[str, Any]:
        message = await self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system + "\n\nRespond with a single JSON object and nothing else.",
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(
            block.text for block in message.content if getattr(block, "type", "") == "text"
        )
        return _extract_json(text)


class FakeLLM(LLM):
    """Returns queued responses in FIFO order. Raises if it runs out."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    async def json(self, *, system: str, user: str, max_tokens: int = 1024) -> dict[str, Any]:
        self.calls.append((system, user))
        if not self._responses:
            raise AssertionError("FakeLLM exhausted — queue another response.")
        return self._responses.pop(0)


class DemoLLM(LLM):
    """Deterministic local LLM for demos and offline development."""

    async def json(
        self, *, system: str, user: str, max_tokens: int = 1024
    ) -> dict[str, Any]:
        if "staff SRE performing incident triage" in system:
            return {
                "suspects": [
                    {
                        "sha": "a1b2c3d",
                        "confidence": 0.87,
                        "reasoning": (
                            "The checkout pricing-cache change landed minutes before "
                            "the 5xx spike and touched the affected service."
                        ),
                    }
                ]
            }
        if "choose the single runbook" in system:
            slug = "checkout-error-rate" if "checkout" in user.lower() else ""
            return {
                "slug": slug,
                "confidence": 0.91 if slug else 0.0,
                "reasoning": "The alert service and error-rate metric match this runbook.",
            }
        if "estimate user impact" in system:
            return {
                "affected_users": 2282,
                "affected_percent": 18.4,
                "error_rate": 0.184,
                "reasoning": "Mock metrics show a tail spike at 18.4% across 12,400 active users.",
            }
        if "blameless post-mortem" in system:
            return {
                "markdown": (
                    "# Post-Mortem\n\n"
                    "## Summary\n"
                    "Checkout saw elevated 5xxs after the pricing-cache rollout. "
                    "The demo rollback completed and the incident was resolved.\n\n"
                    "## Impact\n"
                    "Roughly 2,282 active users were affected at peak.\n\n"
                    "## Action Items\n"
                    "- [ ] Add a canary for pricing-cache error rate (P1)\n"
                    "- [ ] Add rollback verification to the checkout runbook (P1)\n"
                    "- [ ] Review cache fallback behavior before the next rollout (P2)\n"
                )
            }
        return {}


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = _JSON_BLOCK.search(text)
    if not match:
        raise ValueError(f"LLM did not return JSON: {text[:200]}")
    return json.loads(match.group(0))
