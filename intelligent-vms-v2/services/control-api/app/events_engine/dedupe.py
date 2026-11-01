"""Event deduplication and suppression.

A single flapping camera can emit thousands of motion events per hour.
Without suppression the alarm list becomes unusable and the database grows
without bound. Two mechanisms:

  * Deduplication key: identical (type, camera, zone/rule) events within
    `window_s` collapse into the first, which records the repeat count.
  * Suppression: a rule may define its own, longer quiet period after firing.

Both are evaluated in-process with a bounded LRU map, so no database round
trip is needed on the hot ingestion path.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass


def dedupe_key(event_type: str, camera_id: str | None, discriminator: str | None = None) -> str:
    return "|".join([event_type, camera_id or "-", discriminator or "-"])


@dataclass
class DedupeDecision:
    suppressed: bool
    key: str
    repeat_count: int
    first_seen_at: float


class DedupeWindow:
    def __init__(self, window_s: int = 10, max_keys: int = 50_000,
                 clock=time.monotonic) -> None:
        self.window_s = window_s
        self.max_keys = max_keys
        self._clock = clock
        self._seen: OrderedDict[str, tuple[float, int]] = OrderedDict()

    def check(self, key: str, *, window_s: int | None = None) -> DedupeDecision:
        now = self._clock()
        window = window_s if window_s is not None else self.window_s
        entry = self._seen.get(key)

        if entry is not None and (now - entry[0]) <= window:
            first_seen, count = entry
            self._seen[key] = (first_seen, count + 1)
            self._seen.move_to_end(key)
            return DedupeDecision(True, key, count + 1, first_seen)

        self._seen[key] = (now, 1)
        self._seen.move_to_end(key)
        while len(self._seen) > self.max_keys:
            self._seen.popitem(last=False)
        return DedupeDecision(False, key, 1, now)

    def repeat_count(self, key: str) -> int:
        entry = self._seen.get(key)
        return entry[1] if entry else 0

    def purge_expired(self) -> int:
        now = self._clock()
        stale = [k for k, (seen, _) in self._seen.items() if now - seen > self.window_s * 4]
        for key in stale:
            del self._seen[key]
        return len(stale)


class SuppressionRegistry:
    """Per-rule quiet periods after a rule fires."""

    def __init__(self, clock=time.monotonic) -> None:
        self._until: dict[str, float] = {}
        self._clock = clock

    def is_suppressed(self, rule_id: str) -> bool:
        until = self._until.get(rule_id)
        return until is not None and self._clock() < until

    def suppress(self, rule_id: str, seconds: int) -> None:
        if seconds > 0:
            self._until[rule_id] = self._clock() + seconds

    def clear(self, rule_id: str | None = None) -> None:
        if rule_id is None:
            self._until.clear()
        else:
            self._until.pop(rule_id, None)
