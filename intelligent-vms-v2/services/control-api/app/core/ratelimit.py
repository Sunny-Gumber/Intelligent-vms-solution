"""In-process token-bucket rate limiting.

Scope note (honest limitation): this limiter is PER PROCESS. With multiple API
worker processes the effective limit is `limit x workers`. A shared backend
(PostgreSQL advisory counters or Redis) is required for exact global limits in
a multi-worker deployment; tracked in docs/LIMITATIONS.md. Login throttling
additionally relies on the database-backed lockout in core/security.py, which
IS global.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketLimiter:
    def __init__(self, rate_per_minute: int, burst: int | None = None,
                 clock=time.monotonic) -> None:
        self.capacity = float(burst if burst is not None else max(1, rate_per_minute))
        self.refill_per_s = max(rate_per_minute, 1) / 60.0
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def check(self, key: str, cost: float = 1.0) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds)."""
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self.capacity, updated_at=now)
                self._buckets[key] = bucket
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_per_s)
            bucket.updated_at = now
            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True, 0.0
            return False, (cost - bucket.tokens) / self.refill_per_s

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)

    def prune(self, max_idle_s: float = 3600.0) -> int:
        now = self._clock()
        with self._lock:
            stale = [k for k, b in self._buckets.items() if now - b.updated_at > max_idle_s]
            for key in stale:
                del self._buckets[key]
            return len(stale)


class LimiterRegistry:
    def __init__(self) -> None:
        self._limiters: dict[str, TokenBucketLimiter] = {}

    def configure(self, name: str, rate_per_minute: int, burst: int | None = None) -> None:
        self._limiters[name] = TokenBucketLimiter(rate_per_minute, burst)

    def get(self, name: str) -> TokenBucketLimiter | None:
        return self._limiters.get(name)

    def check(self, name: str, key: str, cost: float = 1.0) -> tuple[bool, float]:
        limiter = self._limiters.get(name)
        if limiter is None:
            return True, 0.0
        return limiter.check(key, cost)

    def reset_all(self) -> None:
        for limiter in self._limiters.values():
            limiter.reset()
