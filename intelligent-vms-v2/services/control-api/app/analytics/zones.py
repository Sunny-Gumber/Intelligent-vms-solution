"""Zone geometry and spatial rules (tripwire, intrusion, loitering).

These rules operate on DETECTIONS, whatever produced them: a camera-side
person detection, a server-side inference result, or an external engine. That
is why intrusion/tripwire/loitering are genuinely implemented even though no
detection model is bundled with this release.

All geometry uses normalized image coordinates (0..1), which makes rules
resolution-independent and survivable across stream-profile changes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable

Point = tuple[float, float]


def point_in_polygon(point: Point, polygon: list[Point]) -> bool:
    """Ray casting."""
    if len(polygon) < 3:
        return False
    x, y = point
    inside = False
    count = len(polygon)
    for index in range(count):
        x1, y1 = polygon[index]
        x2, y2 = polygon[(index + 1) % count]
        if (y1 > y) != (y2 > y):
            x_at_y = (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-12) + x1
            if x <= x_at_y:
                inside = not inside
    return inside


def segments_intersect(p1: Point, p2: Point, q1: Point, q2: Point) -> bool:
    def orientation(a: Point, b: Point, c: Point) -> int:
        value = (b[1] - a[1]) * (c[0] - b[0]) - (b[0] - a[0]) * (c[1] - b[1])
        return 0 if abs(value) < 1e-12 else (1 if value > 0 else 2)

    o1, o2 = orientation(p1, p2, q1), orientation(p1, p2, q2)
    o3, o4 = orientation(q1, q2, p1), orientation(q1, q2, p2)
    return o1 != o2 and o3 != o4


def bbox_anchor(bbox: list[float]) -> Point:
    """Bottom-centre of the box — where an object touches the ground."""
    x, y, w, h = bbox
    return (x + w / 2.0, y + h)


@dataclass
class Track:
    track_id: str
    first_seen: float
    last_seen: float
    last_point: Point
    inside_since: float | None = None
    crossed: bool = False
    history: list[Point] = field(default_factory=list)


@dataclass(frozen=True)
class ZoneHit:
    zone_id: str
    rule: str          # intrusion | tripwire | loitering | exit
    track_id: str
    dwell_s: float = 0.0
    direction: str | None = None


class ZoneEvaluator:
    """Stateful evaluator: one instance per camera."""

    def __init__(self, clock=time.monotonic, track_ttl_s: float = 30.0) -> None:
        self._tracks: dict[str, Track] = {}
        self._clock = clock
        self.track_ttl_s = track_ttl_s

    def evaluate(self, *, zone_id: str, kind: str, polygon: list[Point], bbox: list[float],
                 track_id: str, dwell_seconds: float = 0.0,
                 direction: str | None = None) -> list[ZoneHit]:
        now = self._clock()
        point = bbox_anchor(bbox)
        track = self._tracks.get(track_id)
        if track is None:
            track = Track(track_id=track_id, first_seen=now, last_seen=now, last_point=point)
            self._tracks[track_id] = track

        previous_point = track.last_point
        track.last_seen = now
        track.history.append(point)
        del track.history[:-50]

        hits: list[ZoneHit] = []

        if kind in ("region", "intrusion"):
            inside = point_in_polygon(point, polygon)
            if inside and track.inside_since is None:
                track.inside_since = now
                if dwell_seconds <= 0:
                    hits.append(ZoneHit(zone_id, "intrusion", track_id))
            elif inside and dwell_seconds > 0 and track.inside_since is not None:
                dwell = now - track.inside_since
                if dwell >= dwell_seconds and not track.crossed:
                    track.crossed = True
                    hits.append(ZoneHit(zone_id, "loitering", track_id, dwell_s=dwell))
            elif not inside and track.inside_since is not None:
                track.inside_since = None
                track.crossed = False
                hits.append(ZoneHit(zone_id, "exit", track_id))

        elif kind == "tripwire" and len(polygon) >= 2 and not track.crossed:
            if segments_intersect(previous_point, point, polygon[0], polygon[1]):
                crossing = _crossing_direction(previous_point, point, polygon[0], polygon[1])
                if direction in (None, "both", crossing):
                    track.crossed = True
                    hits.append(ZoneHit(zone_id, "tripwire", track_id, direction=crossing))

        track.last_point = point
        self._prune(now)
        return hits

    def _prune(self, now: float) -> None:
        for tid in [t for t, tr in self._tracks.items()
                    if now - tr.last_seen > self.track_ttl_s]:
            del self._tracks[tid]

    @property
    def active_tracks(self) -> int:
        return len(self._tracks)


def _crossing_direction(p1: Point, p2: Point, a: Point, b: Point) -> str:
    def side(point: Point) -> float:
        return (b[0] - a[0]) * (point[1] - a[1]) - (b[1] - a[1]) * (point[0] - a[0])

    return "in" if side(p2) > side(p1) else "out"


def validate_polygon(points: Iterable[Iterable[float]], kind: str) -> list[Point]:
    polygon = [(float(p[0]), float(p[1])) for p in points]
    minimum = 2 if kind == "tripwire" else 3
    if len(polygon) < minimum:
        raise ValueError(f"a {kind} needs at least {minimum} points")
    for x, y in polygon:
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError("zone points must be normalized coordinates between 0 and 1")
    return polygon
