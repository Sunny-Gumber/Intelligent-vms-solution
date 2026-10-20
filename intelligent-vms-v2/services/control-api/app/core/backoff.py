"""Bounded retry with exponential backoff and full jitter.

Every outbound interaction (camera probe, ONVIF call, gateway call, webhook,
database connect) uses a bounded policy. Unbounded retries are what turn one
flapping camera into a server-wide outage.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class BackoffPolicy:
    initial_s: float = 0.5
    max_s: float = 60.0
    multiplier: float = 2.0
    max_attempts: int = 0          # 0 == unlimited attempts, still bounded delay
    jitter: bool = True

    def delay_for(self, attempt: int, *, rng: random.Random | None = None) -> float:
        """Delay before `attempt` (1-based). Full jitter in [0, computed]."""
        if attempt < 1:
            return 0.0
        raw = min(self.max_s, self.initial_s * (self.multiplier ** (attempt - 1)))
        if not self.jitter:
            return raw
        return (rng or random).uniform(0.0, raw)  # noqa: S311 - jitter, not crypto

    def delays(self, count: int, *, rng: random.Random | None = None) -> list[float]:
        return [self.delay_for(i, rng=rng) for i in range(1, count + 1)]


class RetryExhausted(RuntimeError):
    def __init__(self, attempts: int, last_error: BaseException | None) -> None:
        super().__init__(f"retries exhausted after {attempts} attempts: {last_error!r}")
        self.attempts = attempts
        self.last_error = last_error


async def retry_async(operation: Callable[[], Awaitable[T]], *, policy: BackoffPolicy,
                      retry_on: tuple[type[BaseException], ...] = (Exception,),
                      on_retry: Callable[[int, BaseException, float], None] | None = None,
                      sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> T:
    attempt = 0
    last: BaseException | None = None
    while True:
        attempt += 1
        try:
            return await operation()
        except retry_on as exc:
            last = exc
            if policy.max_attempts and attempt >= policy.max_attempts:
                raise RetryExhausted(attempt, last) from exc
            delay = policy.delay_for(attempt)
            if on_retry:
                on_retry(attempt, exc, delay)
            await sleep(delay)


def iter_delays(policy: BackoffPolicy, attempts: int) -> Iterable[float]:
    for attempt in range(1, attempts + 1):
        yield policy.delay_for(attempt)
