"""Covering-segment lookup must filter before LIMIT.

The recording index lives in ClickHouse, not PostgreSQL. `recording_segments`
is already ordered by (tenant_id, site_id, camera_id, segment_start, segment_id),
so these tests do not open a database. The store double applies the SQL
predicate, order, and limit the client actually sends.
"""

import asyncio
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.core.auth import Principal
from app.routers import recordings
from app.services.recording_index import (
    RecordingIndexClient,
    RecordingIndexError,
    _MAX_SEGMENT_DURATION_SECONDS,
)


ORIGIN = datetime(2026, 9, 26, tzinfo=timezone.utc)
SEGMENT_SECONDS = 30
HISTORY = 200


class _Response:
    def __init__(self, rows):
        self.text = "\n".join(json.dumps(row) for row in rows)

    def raise_for_status(self):
        return None


def _parse(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _end(row):
    start = _parse(row["segment_start"])
    microseconds = int(round(float(row["duration_seconds"]) * 1_000_000))
    return start + timedelta(microseconds=microseconds)


def _key(row):
    return (_parse(row["segment_start"]), str(row["segment_id"]))


def _covers(row, instant):
    start = _parse(row["segment_start"])
    return start <= instant < _end(row)


def _overlaps(row, start, end):
    segment_start = _parse(row["segment_start"])
    segment_end = _end(row)
    return segment_start < end and segment_end > start


class IndexStore:
    """ClickHouse double that honors the predicate, order, and limit it is sent."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.queries = []
        self.returned = []

    def reset_capture(self):
        self.queries = []
        self.returned = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, _url, params=None, content=None):
        params = params or {}
        query = content or ""
        self.queries.append(query)
        where = query.split("WHERE", 1)[1].split("ORDER BY", 1)[0]
        order = query.split("ORDER BY", 1)[1].split("LIMIT", 1)[0]
        # Collapse versions before the duration and overlap filters when the
        # SQL aggregates by indexed_at. Otherwise a replaced longer row still
        # matches the instant.
        rows = self.rows
        if "argMax" in query and "indexed_at" in query:
            rows = _collapse_versions(rows)
        matched = [row for row in rows if _matches(row, params, where)]
        matched.sort(key=_key, reverse="segment_start DESC" in order)
        if "LIMIT 1 BY segment_id" in query:
            deduped = []
            seen = set()
            for row in matched:
                if row["segment_id"] in seen:
                    continue
                seen.add(row["segment_id"])
                deduped.append(row)
            matched = deduped
        if "param_after_start" in params:
            after = (_parse(params["param_after_start"]), params.get("param_after_segment_id", ""))
            matched = [row for row in matched if _key(row) > after]
        if "param_before_start" in params:
            before = (_parse(params["param_before_start"]), params.get("param_before_segment_id", ""))
            matched = [row for row in matched if _key(row) < before]
        offset = int(params.get("param_offset", "0"))
        limit = int(params["param_limit"])
        page = matched[offset:offset + limit]
        self.returned.extend(page)
        return _Response(page)


def _scope_allowed(row, params, where):
    """Honor tenant, site, and camera predicates when the SQL binds them."""
    checks = (
        ("tenant_id = {tenant:String}", "tenant_id", "param_tenant"),
        ("site_id = {site:String}", "site_id", "param_site"),
        ("camera_id = {camera:String}", "camera_id", "param_camera"),
    )
    for clause, field, param in checks:
        if clause in where and row.get(field) != params.get(param):
            return False
    return True


def _duration_allowed(row, where):
    """Honor the finite one-day duration cap when the SQL states it."""
    if "duration_seconds <=" not in where:
        return True
    duration = row.get("duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        return False
    if not math.isfinite(duration):
        return False
    return 0 < duration <= _MAX_SEGMENT_DURATION_SECONDS


def _indexed_at(row):
    raw = row.get("indexed_at")
    if raw is None or raw == "":
        return datetime.min.replace(tzinfo=timezone.utc)
    return _parse(raw)


def _collapse_versions(rows):
    """Keep the newest indexed_at row for each ReplacingMergeTree sort key."""
    best = {}
    order = []
    for row in rows:
        key = (
            row.get("tenant_id"),
            row.get("site_id"),
            row.get("camera_id"),
            str(row.get("segment_start")),
            str(row.get("segment_id")),
        )
        current = best.get(key)
        if current is None:
            best[key] = row
            order.append(key)
            continue
        if _indexed_at(row) >= _indexed_at(current):
            best[key] = row
    return [best[key] for key in order]


def _matches(row, params, where):
    if not _scope_allowed(row, params, where):
        return False
    if not _duration_allowed(row, where):
        return False
    start = _parse(row["segment_start"])
    end = _end(row)
    if "toIntervalMicrosecond" in where and "param_instant" in params:
        instant = _parse(params["param_instant"])
        not_before = _parse(params["param_not_before"])
        return not_before <= start <= instant and end > instant
    if "toIntervalMicrosecond" in where and "param_window_start" in params:
        window_start = _parse(params["param_window_start"])
        window_end = _parse(params["param_end"])
        lower = _parse(params["param_start"])
        return lower <= start < window_end and end > window_start
    lower = _parse(params["param_start"])
    upper = _parse(params["param_end"])
    return lower <= start < upper


def _install(monkeypatch, store):
    import app.services.recording_index as module

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda *args, **kwargs: store)


def _assert_point_query(store):
    assert store.queries, "point lookup did not query the index"
    for query in store.queries:
        where, limit_and_order = query.split("WHERE", 1)[1].split("ORDER BY", 1)
        assert "toIntervalMicrosecond" in where
        assert query.index("toIntervalMicrosecond") < query.index("LIMIT")
        assert "segment_start DESC" in limit_and_order
        assert "segment_id DESC" in limit_and_order


def _segment(segment_id, node, start, duration=SEGMENT_SECONDS):
    return {
        "segment_id": segment_id,
        "recording_node_id": node,
        "record_stream_key": "cam-record",
        "segment_path": f"/recordings/{segment_id}.mp4",
        "segment_start": start.isoformat(),
        "tenant_id": "tenant-1",
        "site_id": "site-1",
        "camera_id": "cam-1",
        "duration_seconds": duration,
        "completed_at": (start + timedelta(seconds=duration)).isoformat(),
        "storage_tier": "hot",
        "object_uri": None,
    }


def _history(count=HISTORY, node="node-old", origin=ORIGIN):
    return [
        _segment(f"seg-{index:04d}", node, origin + timedelta(seconds=index * SEGMENT_SECONDS))
        for index in range(count)
    ]


def _lookup(store, instant):
    store.reset_capture()
    client = RecordingIndexClient()
    return asyncio.run(
        client.segment_for_start(
            tenant_id="tenant-1",
            site_id="site-1",
            camera_id="cam-1",
            start=instant,
        )
    )


def _assert_filtered(store, instant):
    assert store.returned, "covering lookup returned no candidate rows"
    assert all(_covers(row, instant) for row in store.returned)
    _assert_point_query(store)


def test_latest_of_more_than_50_segments_is_the_covering_segment(monkeypatch):
    rows = _history()
    instant = ORIGIN + timedelta(seconds=(HISTORY - 1) * SEGMENT_SECONDS + 15)
    store = IndexStore(rows)
    _install(monkeypatch, store)

    selected = _lookup(store, instant)

    assert selected is not None
    assert selected["segment_id"] == "seg-0199"
    assert selected["recording_node_id"] == "node-old"
    _assert_filtered(store, instant)
    assert len(store.returned) < 50


def test_earliest_of_more_than_50_segments_is_the_covering_segment(monkeypatch):
    rows = _history()
    instant = ORIGIN + timedelta(seconds=10)
    store = IndexStore(rows)
    _install(monkeypatch, store)

    selected = _lookup(store, instant)

    assert selected is not None
    assert selected["segment_id"] == "seg-0000"
    assert selected["recording_node_id"] == "node-old"
    _assert_filtered(store, instant)


def test_middle_of_more_than_50_segments_is_the_covering_segment(monkeypatch):
    rows = _history()
    instant = ORIGIN + timedelta(seconds=100 * SEGMENT_SECONDS + 10)
    store = IndexStore(rows)
    _install(monkeypatch, store)

    selected = _lookup(store, instant)

    assert selected is not None
    assert selected["segment_id"] == "seg-0100"
    assert selected["recording_node_id"] == "node-old"
    _assert_filtered(store, instant)


def test_gap_returns_no_covering_segment(monkeypatch):
    rows = _history(80)
    gap_at = ORIGIN + timedelta(seconds=80 * SEGMENT_SECONDS + 15)
    rows.append(_segment("after-gap", "node-new", gap_at + timedelta(minutes=2)))
    store = IndexStore(rows)
    _install(monkeypatch, store)

    selected = _lookup(store, gap_at)

    assert selected is None
    assert store.returned == []
    _assert_point_query(store)


def test_exact_boundaries_follow_the_half_open_fence(monkeypatch):
    """Equal to the start is inside. Equal to the end, and one microsecond later, is outside."""
    boundary_start = datetime(2026, 9, 26, 4, 5, 6, 250001, tzinfo=timezone.utc)
    rows = [
        _segment(
            f"pre-{index:04d}",
            "node-old",
            boundary_start - timedelta(seconds=(80 - index) * SEGMENT_SECONDS),
        )
        for index in range(80)
    ]
    rows.append(_segment("boundary", "node-old", boundary_start))
    boundary_end = boundary_start + timedelta(seconds=SEGMENT_SECONDS)
    store = IndexStore(rows)
    _install(monkeypatch, store)

    at_start = _lookup(store, boundary_start)
    before_start = _lookup(store, boundary_start - timedelta(microseconds=1))
    inside_end = _lookup(store, boundary_end - timedelta(microseconds=1))
    at_end = _lookup(store, boundary_end)
    after_end = _lookup(store, boundary_end + timedelta(microseconds=1))

    assert at_start["segment_id"] == "boundary"
    assert before_start["segment_id"] == "pre-0079"
    assert inside_end["segment_id"] == "boundary"
    assert at_end is None
    assert after_end is None
    assert store.returned == []
    _assert_point_query(store)


def test_segment_longer_than_the_old_six_hour_lookback_still_covers(monkeypatch):
    """A completed segment may last up to one day, so a six-hour lookback is too short."""
    instant = datetime(2026, 9, 26, 18, 0, tzinfo=timezone.utc)
    start = instant - timedelta(hours=7)
    rows = [_segment("long", "node-old", start, duration=8 * 60 * 60)]
    store = IndexStore(rows)
    _install(monkeypatch, store)

    selected = _lookup(store, instant)

    assert selected is not None
    assert selected["segment_id"] == "long"
    assert selected["recording_node_id"] == "node-old"
    _assert_filtered(store, instant)


def test_old_owner_segment_is_returned_after_a_placement_move(monkeypatch):
    rows = _history(HISTORY, node="node-old")
    last_end = ORIGIN + timedelta(seconds=HISTORY * SEGMENT_SECONDS)
    rows.append(_segment("seg-new", "node-new", last_end + timedelta(seconds=5)))
    instant = ORIGIN + timedelta(seconds=(HISTORY - 1) * SEGMENT_SECONDS + 12)
    store = IndexStore(rows)
    _install(monkeypatch, store)

    selected = _lookup(store, instant)

    assert selected is not None
    assert selected["segment_id"] == "seg-0199"
    assert selected["recording_node_id"] == "node-old"
    _assert_filtered(store, instant)


def test_more_than_50_overlapping_owners_prefer_the_latest_start(monkeypatch):
    instant = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    rows = []
    for index in range(60):
        start = instant - timedelta(seconds=60 - index)
        node = "node-new" if index == 59 else "node-old"
        rows.append(_segment(f"ov-{index:04d}", node, start, duration=120))
    store = IndexStore(rows)
    _install(monkeypatch, store)

    selected = _lookup(store, instant)

    assert selected is not None
    assert selected["segment_id"] == "ov-0059"
    assert selected["recording_node_id"] == "node-new"
    _assert_filtered(store, instant)


def test_equal_segment_starts_prefer_the_greater_segment_id(monkeypatch):
    start = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    instant = start + timedelta(seconds=5)
    rows = [
        _segment("seg-a", "node-old", start),
        _segment("seg-m", "node-new", start),
    ]
    store = IndexStore(rows)
    _install(monkeypatch, store)

    selected = _lookup(store, instant)

    assert selected["segment_id"] == "seg-m"
    assert selected["recording_node_id"] == "node-new"
    _assert_filtered(store, instant)


def test_timeline_and_export_listing_applies_overlap_before_limit(monkeypatch):
    window_start = ORIGIN + timedelta(hours=2)
    rows = _history(100, origin=window_start - timedelta(seconds=100 * SEGMENT_SECONDS))
    inside = [
        _segment(f"in-{index:04d}", "node-old", window_start + timedelta(seconds=index * SEGMENT_SECONDS))
        for index in range(10)
    ]
    rows.extend(inside)
    store = IndexStore(rows)
    _install(monkeypatch, store)
    client = RecordingIndexClient()
    window_end = window_start + timedelta(seconds=10 * SEGMENT_SECONDS)

    page = asyncio.run(
        client.segments(
            tenant_id="tenant-1",
            site_id="site-1",
            camera_id="cam-1",
            start=window_start,
            end=window_end,
            limit=5,
        )
    )

    assert [row["segment_id"] for row in page] == [f"in-{index:04d}" for index in range(5)]
    assert page.next_after_segment_id == "in-0004"
    assert all(_overlaps(row, window_start, window_end) for row in store.returned)
    query = store.queries[0]
    where = query.split("WHERE", 1)[1].split("ORDER BY", 1)[0]
    assert "toIntervalMicrosecond" in where
    assert query.index("toIntervalMicrosecond") < query.index("LIMIT")
    assert "segment_start ASC" in query
    source = Path(__file__).parents[1].joinpath(
        "services/control-api/app/routers/recordings.py"
    ).read_text(encoding="utf-8")
    assert source.count("recording_index.segments(") >= 2


def test_recording_segments_primary_key_indexes_camera_and_segment_start():
    """The MergeTree key already supports the covering range. No migration is added."""
    ddl = Path(__file__).parents[1].joinpath("services/event-writer/main.py").read_text(encoding="utf-8")
    assert "ORDER BY (tenant_id, site_id, camera_id, segment_start, segment_id)" in ddl


def test_point_lookup_rejects_a_naive_instant():
    client = RecordingIndexClient()
    with pytest.raises(RecordingIndexError, match="timezone"):
        asyncio.run(
            client.segment_for_start(
                tenant_id="tenant-1",
                site_id="site-1",
                camera_id="cam-1",
                start=datetime(2026, 9, 26, 1, 0, 0),
            )
        )


def _request():
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/recordings/cameras/cam-1/play",
            "raw_path": b"/api/v1/recordings/cameras/cam-1/play",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 9),
            "server": ("test", 80),
        }
    )


def _principal():
    return Principal(
        subject="viewer-1",
        roles=frozenset({"viewer"}),
        tenant_id="tenant-1",
        site_ids=frozenset({"site-1"}),
    )


class _Upstream:
    status_code = 200
    headers = {"content-type": "video/mp4"}

    async def aiter_bytes(self):
        if False:
            yield b""

    async def aclose(self):
        return None


class _Playback:
    def __init__(self, items):
        self.items = items
        self.listed = 0
        self.opened = 0

    async def list_timespans(self, path, start, end):
        self.listed += 1
        return self.items

    async def open_stream(self, path, start, duration, fmt, range_header, timeout_seconds=None):
        self.opened += 1
        return SimpleNamespace(aclose=self._aclose), _Upstream()

    async def _aclose(self):
        return None


def _arm_play(monkeypatch, segment, items):
    playback = _Playback(items)
    calls = {"historical": [], "current": 0}

    async def camera(*args, **kwargs):
        return SimpleNamespace(id="cam-1", tenant_id="tenant-1", site_id="site-1")

    async def policy(*args, **kwargs):
        return SimpleNamespace(record_stream_key="cam-record", recording_node_id="node-current")

    async def segment_for_start(**kwargs):
        return segment

    async def current_playback(*args, **kwargs):
        calls["current"] += 1
        return playback

    async def get_node(_session, node_id, required_role="recording"):
        calls["historical"].append(node_id)
        return SimpleNamespace(id=node_id)

    async def node_playback(node):
        return playback

    monkeypatch.setattr(recordings.settings, "placement_execution_enabled", True)
    monkeypatch.setattr(recordings, "authorized_camera", camera)
    monkeypatch.setattr(recordings, "_policy_for_camera", policy)
    monkeypatch.setattr(recordings.recording_index, "segment_for_start", segment_for_start)
    monkeypatch.setattr(recordings, "_playback_for_policy", current_playback)
    monkeypatch.setattr(recordings, "get_node", get_node)
    monkeypatch.setattr(recordings.node_clients, "playback", node_playback)
    return playback, calls


def test_gap_falls_back_to_the_current_node_only_when_it_proves_coverage(monkeypatch):
    from fastapi import HTTPException

    start = ORIGIN + timedelta(hours=1)
    playback, calls = _arm_play(monkeypatch, None, [])
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            recordings.play(
                _request(),
                "cam-1",
                start=start,
                duration=20,
                session=SimpleNamespace(),
                principal=_principal(),
            )
        )

    assert caught.value.status_code == 404
    assert calls["current"] == 1
    assert calls["historical"] == []
    assert playback.listed == 1
    assert playback.opened == 0

    playback, calls = _arm_play(
        monkeypatch,
        None,
        [{"start": start.isoformat(), "duration": 20}],
    )
    response = asyncio.run(
        recordings.play(
            _request(),
            "cam-1",
            start=start,
            duration=20,
            session=SimpleNamespace(),
            principal=_principal(),
        )
    )

    assert response.status_code == 200
    assert calls["current"] == 1
    assert calls["historical"] == []
    assert playback.opened == 1


def test_historical_playback_uses_the_old_owner_node(monkeypatch):
    start = ORIGIN + timedelta(hours=1)
    segment = {
        "segment_id": "seg-old",
        "recording_node_id": "node-old",
        "segment_start": start,
        "segment_end": start + timedelta(seconds=30),
    }
    _playback, calls = _arm_play(monkeypatch, segment, [])

    response = asyncio.run(
        recordings.play(
            _request(),
            "cam-1",
            start=start,
            duration=20,
            session=SimpleNamespace(),
            principal=_principal(),
        )
    )

    assert response.status_code == 200
    assert calls["historical"] == ["node-old"]
    assert calls["current"] == 0
