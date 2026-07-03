"""Slack adapter.

Three clients behind one interface:
- MockSlackClient    — captures messages in memory; supports update() for tests.
- WebhookSlackClient — posts via an incoming webhook. Cannot update (webhook
  limitation), so update() falls back to posting a fresh threaded message.
- BotTokenSlackClient — full Web API (chat.postMessage + chat.update). This is
  what production wants when you need streaming brief updates.

The abstract SlackClient always exposes `post()` + `update()`; callers use both
freely and let the implementation degrade gracefully when updates aren't native.
"""

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

    @abstractmethod
    async def update(self, channel: str, ts: str, text: str) -> PostedMessage:
        ...


@dataclass
class MockSlackClient(SlackClient):
    """In-memory Slack. Tracks posts and updates independently so tests can
    assert on both the initial brief and every streaming update."""

    sent: list[PostedMessage] = field(default_factory=list)
    updates: list[PostedMessage] = field(default_factory=list)

    async def post(self, channel: str, text: str, thread_ts: str | None = None) -> PostedMessage:
        ts = f"{datetime.now(timezone.utc).timestamp():.6f}"
        msg = PostedMessage(channel=channel, text=text, ts=ts, thread_ts=thread_ts)
        self.sent.append(msg)
        return msg

    async def update(self, channel: str, ts: str, text: str) -> PostedMessage:
        msg = PostedMessage(channel=channel, text=text, ts=ts)
        self.updates.append(msg)
        # Reflect the update in `sent` so `latest_text_for(ts)` reads the newest.
        for i, existing in enumerate(self.sent):
            if existing.ts == ts:
                self.sent[i] = PostedMessage(
                    channel=existing.channel,
                    text=text,
                    ts=existing.ts,
                    thread_ts=existing.thread_ts,
                )
                break
        return msg

    def latest_text_for(self, ts: str) -> str | None:
        for msg in reversed(self.sent):
            if msg.ts == ts:
                return msg.text
        return None


class WebhookSlackClient(SlackClient):
    """Incoming webhooks can post but not update. update() falls back to a fresh
    threaded reply so callers still see progress in the channel."""

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

    async def update(self, channel: str, ts: str, text: str) -> PostedMessage:
        # Webhook mode has no message update. Fall back to threaded reply.
        return await self.post(channel=channel, text=f":arrows_counterclockwise: {text}", thread_ts=ts)


class BotTokenSlackClient(SlackClient):
    """Full Slack Web API. Requires a bot token with chat:write scope."""

    def __init__(self, bot_token: str) -> None:
        self._token = bot_token
        self._base = "https://slack.com/api"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    @async_retry(attempts=3, base_delay=0.5, retry_on=_RETRYABLE_HTTP)
    async def post(self, channel: str, text: str, thread_ts: str | None = None) -> PostedMessage:
        payload: dict[str, object] = {"channel": channel, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self._base}/chat.postMessage", json=payload, headers=self._headers()
            )
            resp.raise_for_status()
            body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"slack chat.postMessage failed: {body.get('error')}")
        return PostedMessage(
            channel=body.get("channel", channel),
            text=text,
            ts=body.get("ts", ""),
            thread_ts=thread_ts,
        )

    @async_retry(attempts=3, base_delay=0.5, retry_on=_RETRYABLE_HTTP)
    async def update(self, channel: str, ts: str, text: str) -> PostedMessage:
        payload = {"channel": channel, "ts": ts, "text": text}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self._base}/chat.update", json=payload, headers=self._headers()
            )
            resp.raise_for_status()
            body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"slack chat.update failed: {body.get('error')}")
        return PostedMessage(channel=channel, text=text, ts=ts)


def build_slack_client(
    mode: str, webhook_url: str = "", bot_token: str = ""
) -> SlackClient:
    if mode == "bot" and bot_token:
        return BotTokenSlackClient(bot_token=bot_token)
    if mode == "webhook" and webhook_url:
        return WebhookSlackClient(webhook_url=webhook_url)
    return MockSlackClient()
