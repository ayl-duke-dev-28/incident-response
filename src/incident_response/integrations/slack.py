"""Slack adapter with a mock mode (captures messages in-memory) and a webhook mode."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from ..retry import async_retry

_RETRYABLE_HTTP = (httpx.HTTPError, TimeoutError)


@dataclass
class PostedMessage:
    channel: str
    text: str
    ts: str
    thread_ts: str | None = None


class SlackClient(ABC):
    @abstractmethod
    async def post(self, channel: str, text: str, thread_ts: str | None = None) -> PostedMessage:
        ...


@dataclass
class MockSlackClient(SlackClient):
    """In-memory Slack. Message ts is monotonic so tests can assert ordering."""

    sent: list[PostedMessage] = field(default_factory=list)

    async def post(self, channel: str, text: str, thread_ts: str | None = None) -> PostedMessage:
        ts = f"{datetime.now(timezone.utc).timestamp():.6f}"
        msg = PostedMessage(channel=channel, text=text, ts=ts, thread_ts=thread_ts)
        self.sent.append(msg)
        return msg


class WebhookSlackClient(SlackClient):
    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url

    @async_retry(attempts=3, base_delay=0.5, retry_on=_RETRYABLE_HTTP)
    async def post(self, channel: str, text: str, thread_ts: str | None = None) -> PostedMessage:
        payload = {"channel": channel, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(self._webhook_url, json=payload)
            resp.raise_for_status()
        ts = f"{datetime.now(timezone.utc).timestamp():.6f}"
        return PostedMessage(channel=channel, text=text, ts=ts, thread_ts=thread_ts)


def build_slack_client(mode: str, webhook_url: str) -> SlackClient:
    if mode == "webhook" and webhook_url:
        return WebhookSlackClient(webhook_url=webhook_url)
    return MockSlackClient()
