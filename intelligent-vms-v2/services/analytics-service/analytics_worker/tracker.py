"""Lightweight IoU tracker.

Zone rules (intrusion, tripwire, loitering) need stable identities across
frames. At the 1-2 fps this worker samples, a full Kalman/ReID tracker is
unnecessary complexity; greedy IoU association over a short memory is
sufficient and has no extra dependency.

Honest limitation: at low sample rates, fast-moving objects can be assigned a
new id. That produces duplicate counts rather than missed detections, and it
is recorded in docs/LIMITATIONS.md.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field

Box = tuple[float, float, float, float]   # x, y, w, h normalized


def iou(a: Box, b: Box) -> float:
    ax1, ay1, ax2, ay2 = a[0], a[1], a[0] + a[2], a[1] + a[3]
    bx1, by1, bx2, by2 = b[0], b[1], b[0] + b[2], b[1] + b[3]

    inter_w = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    inter_h = max(0.0, min(ay2, by2) - max(ay1, by1))
    intersection = inter_w * inter_h
    if intersection <= 0:
        return 0.0
    union = a[2] * a[3] + b[2] * b[3] - intersection
    return intersection / union if union > 0 else 0.0


@dataclass
class Track:
    track_id: str
    box: Box
    object_class: str
    first_seen: float
    last_seen: float
    hits: int = 1
    history: list[Box] = field(default_factory=list)


class IouTracker:
    def __init__(self, *, iou_threshold: float = 0.3, max_age_s: float = 5.0,
                 clock=time.monotonic) -> None:
        self.iou_threshold = iou_threshold
        self.max_age_s = max_age_s
        self._clock = clock
        self._tracks: dict[str, Track] = {}
        self._counter = itertools.count(1)

    def update(self, detections: list[tuple[str, Box]]) -> list[str]:
        """Associate detections with tracks. Returns a track id per detection."""
        now = self._clock()
        self._prune(now)

        unmatched = dict(self._tracks)
        assigned: list[str] = []

        for object_class, box in detections:
            best_id, best_score = None, self.iou_threshold
            for track_id, track in unmatched.items():
                if track.object_class != object_class:
                    continue
                score = iou(track.box, box)
                if score >= best_score:
                    best_id, best_score = track_id, score

            if best_id is None:
                track_id = f"t{next(self._counter)}"
                self._tracks[track_id] = Track(track_id=track_id, box=box,
                                               object_class=object_class,
                                               first_seen=now, last_seen=now,
                                               history=[box])
            else:
                track_id = best_id
                track = self._tracks[track_id]
                track.box = box
                track.last_seen = now
                track.hits += 1
                track.history.append(box)
                del track.history[:-50]
                unmatched.pop(track_id, None)
            assigned.append(track_id)

        return assigned

    def _prune(self, now: float) -> None:
        stale = [tid for tid, t in self._tracks.items()
                 if now - t.last_seen > self.max_age_s]
        for track_id in stale:
            del self._tracks[track_id]

    @property
    def active(self) -> int:
        return len(self._tracks)
