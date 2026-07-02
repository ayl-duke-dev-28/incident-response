"""Exponential backoff with jitter for external calls.

Async-only. Retries on any exception in the given tuple. Logs (via stdlib logging)
each retry so structured-log middleware can pick it up.
"""

from __future__ import annotations

import asyncio
import logging
import random
from functools import wraps
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)


def async_retry(
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    jitter: float = 0.25,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator: retry an async callable with exponential backoff.

    Delay for attempt n is min(max_delay, base_delay * 2**(n-1)) * (1 ± jitter).
    """

    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(fn)
        async def wrapper(*args, **kwargs) -> T:
            last: BaseException | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return await fn(*args, **kwargs)
                except retry_on as exc:
                    last = exc
                    if attempt == attempts:
                        break
                    delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    delay *= 1 + random.uniform(-jitter, jitter)
                    logger.warning(
                        "retry",
                        extra={
                            "fn": fn.__qualname__,
                            "attempt": attempt,
                            "max_attempts": attempts,
                            "delay_ms": int(delay * 1000),
                            "error": str(exc),
                        },
                    )
                    await asyncio.sleep(max(0.0, delay))
            assert last is not None
            raise last

        return wrapper

    return decorator
