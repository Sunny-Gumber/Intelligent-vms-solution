"""Circuit breaker.

Protects the control plane from a device or dependency failing fast. After
`failure_threshold` consecutive failures the breaker opens and calls are
rejected immediately for `reset_timeout_s`, then one probe (half-open) is
allowed. Keeps a dead camera from consuming a worker slot forever.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    def __init__(self, name: str, retry_after_s: float) -> None:
        super().__init__(f"circuit {name!r} is open; retry in {retry_after_s:.1f}s")
        self.name = name
        self.retry_after_s = retry_after_s


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 5
    reset_timeout_s: float = 30.0
    half_open_successes: int = 1
    clock: Callable[[], float] = time.monotonic

    _state: BreakerState = field(default=BreakerState.CLOSED, init=False)
    _failures: int = field(default=0, init=False)
    _successes: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)

    @property
    def state(self) -> BreakerState:
        if self._state is BreakerState.OPEN and self._elapsed() >= self.reset_timeout_s:
            self._state = BreakerState.HALF_OPEN
            self._successes = 0
        return self._state

    def _elapsed(self) -> float:
        return self.clock() - self._opened_at

    def allows(self) -> bool:
        return self.state is not BreakerState.OPEN

    def retry_after(self) -> float:
        return max(0.0, self.reset_timeout_s - self._elapsed())

    def record_success(self) -> None:
        if self.state is BreakerState.HALF_OPEN:
            self._successes += 1
            if self._successes >= self.half_open_successes:
                self._reset()
        else:
            self._failures = 0

    def record_failure(self) -> None:
        if self.state is BreakerState.HALF_OPEN:
            self._trip()
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._trip()

    def _trip(self) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = self.clock()
        self._successes = 0

    def _reset(self) -> None:
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._successes = 0

    async def call(self, operation: Callable[[], Awaitable[T]]) -> T:
        if not self.allows():
            raise CircuitOpenError(self.name, self.retry_after())
        try:
            result = await operation()
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result


class BreakerRegistry:
    """One breaker per camera/dependency, created lazily."""

    def __init__(self, failure_threshold: int = 5, reset_timeout_s: float = 30.0) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}
        self._threshold = failure_threshold
        self._timeout = reset_timeout_s

    def get(self, name: str) -> CircuitBreaker:
        breaker = self._breakers.get(name)
        if breaker is None:
            breaker = CircuitBreaker(name=name, failure_threshold=self._threshold,
                                     reset_timeout_s=self._timeout)
            self._breakers[name] = breaker
        return breaker

    def forget(self, name: str) -> None:
        self._breakers.pop(name, None)

    def snapshot(self) -> dict[str, str]:
        return {name: b.state.value for name, b in self._breakers.items()}
