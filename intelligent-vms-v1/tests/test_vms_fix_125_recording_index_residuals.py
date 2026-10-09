"""Regression locks for FIX-017 residuals REV-017-001 through REV-017-005.

The recording index is ClickHouse. These tests use the query double and a
fake writer client. They do not open a database.
"""

import asyncio
import importlib.util
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from app.core.auth import Principal
from app.routers import recordings
from app.services.recording_index import RecordingIndexClient
from app.services.search_page import RecordingIndexPage


def _load_index_double():
    spec = importlib.util.spec_from_file_location(
        "fix017_index_double",
        Path(__file__).with_name("test_vms_fix_017_covering_segment.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_DOUBLE = _load_index_double()
IndexStore = _DOUBLE.IndexStore
_install = _DOUBLE._install
_lookup = _DOUBLE._lookup
_segment = _DOUBLE._segment


ROOT = Path(__file__).parents[1]
WRITER_PATH = ROOT / "services" / "event-writer" / "main.py"
YEAR_ONE = datetime(1, 1, 1, tzinfo=timezone.utc)


class _ClickHouseResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class _ClickHouse:
    def __init__(self, column_line=None):
        self.column_line = column_line
        self.bodies = []

    async def post(self, _url, params=None, content=None):
        chunks = []
        if params and isinstance(params.get("query"), str):
            chunks.append(params["query"])
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, bytes):
            chunks.append(content.decode("utf-8"))
        body = "\n".join(chunks)
        self.bodies.append(body)
        text = self.column_line if "system.columns" in body and self.column_line else ""
        return _ClickHouseResponse(text)


def _load_event_writer():
    if "aiokafka" not in sys.modules:
        fake = types.ModuleType("aiokafka")
        fake.AIOKafkaConsumer = object
        sys.modules["aiokafka"] = fake
    spec = importlib.util.spec_from_file_location("vms_event_writer_fix_125", WRITER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _request():
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/recordings/cameras/cam-1/timeline",
            "raw_path": b"/api/v1/recordings/cameras/cam-1/timeline",
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


def _arm_camera(monkeypatch):
    async def camera(*args, **kwargs):
        return SimpleNamespace(id="cam-1", tenant_id="tenant-1", site_id="site-1")

    async def policy(*args, **kwargs):
        return SimpleNamespace(record_stream_key="cam-record", recording_node_id="node-current")

    monkeypatch.setattr(recordings.settings, "placement_execution_enabled", True)
    monkeypatch.setattr(recordings, "authorized_camera", camera)
    monkeypatch.setattr(recordings, "_policy_for_camera", policy)


def _versions(start):
    stale = _segment("seg-1", "node-stale", start, duration=120)
    stale["indexed_at"] = (start + timedelta(seconds=1)).isoformat()
    fresh = _segment("seg-1", "node-fresh", start, duration=30)
    fresh["indexed_at"] = (start + timedelta(seconds=2)).isoformat()
    return [stale, fresh]


def test_recording_segments_keep_microseconds_beside_the_sort_key():
    """REV-017-001: %f is stored beside the DateTime64(3) sort key, not inside it."""
    ddl = WRITER_PATH.read_text(encoding="utf-8")
    assert "segment_start DateTime64(3, 'UTC')" in ddl
    assert "segment_start_exact Nullable(DateTime64(6, 'UTC'))" in ddl
    order = ddl.split("ORDER BY (tenant_id, site_id, camera_id, segment_start, segment_id)", 1)[1]
    order = order.split("TTL", 1)[0]
    assert "segment_start_exact" not in order


def test_point_lookup_projects_exact_start_after_version_collapse(monkeypatch):
    """REV-017-001: the fence uses the microsecond column after argMax."""
    boundary = datetime(2026, 9, 26, 4, 5, 6, 250001, tzinfo=timezone.utc)
    store = IndexStore([_segment("boundary", "node-old", boundary, duration=30)])
    _install(monkeypatch, store)
    instant = boundary + timedelta(seconds=30) - timedelta(microseconds=1)

    selected = _lookup(store, instant)

    assert selected is not None
    assert selected["segment_id"] == "boundary"
    query = store.queries[0]
    assert "segment_start_exact" in query
    assert "toDateTime64(segment_start, 6)" in query
    assert "toIntervalMillisecond(1)" in query
    assert "argMax(duration_seconds, indexed_at)" in query
    assert query.index("GROUP BY") < query.index("duration_seconds <=")
    assert "segment_start DESC" in query


def test_timeline_query_projects_exact_start_after_version_collapse(monkeypatch):
    """REV-017-001: listing uses the same exact start and collapses versions first."""
    start = datetime(2026, 9, 26, 4, 5, 6, 250001, tzinfo=timezone.utc)
    store = IndexStore([_segment("boundary", "node-old", start, duration=30)])
    _install(monkeypatch, store)
    client = RecordingIndexClient()

    page = asyncio.run(
        client.segments(
            tenant_id="tenant-1",
            site_id="site-1",
            camera_id="cam-1",
            start=start,
            end=start + timedelta(seconds=30),
            limit=5,
        )
    )

    assert [row["segment_id"] for row in page] == ["boundary"]
    query = store.queries[0]
    assert "segment_start_exact" in query
    assert "toDateTime64(segment_start, 6)" in query
    assert "argMax(duration_seconds, indexed_at)" in query
    assert query.index("GROUP BY") < query.index("duration_seconds <=")
    assert "segment_start ASC" in query


def test_writer_row_keeps_sub_millisecond_segment_start():
    """REV-017-001: the writer copies %f into segment_start_exact."""
    writer = _load_event_writer()
    start = "2026-09-26T04:05:06.250001+00:00"
    row = writer.recording_row(
        {
            "segment_id": "seg-1",
            "tenant_id": "tenant-1",
            "site_id": "site-1",
            "camera_id": "cam-1",
            "recording_node_id": "node-1",
            "record_stream_key": "cam-record",
            "segment_path": "/recordings/1.mp4",
            "segment_start": start,
            "duration_seconds": 30,
            "completed_at": start,
        }
    )
    assert row["segment_start"] == start
    assert row["segment_start_exact"] == start


def test_missing_exact_start_column_is_added():
    """REV-017-001: an existing table gains the column. The sort key is not modified."""
    writer = _load_event_writer()
    sql = writer.recording_exact_start_alter_sql("vms", None)
    assert sql is not None
    assert "ADD COLUMN IF NOT EXISTS segment_start_exact Nullable(DateTime64(6, 'UTC'))" in sql
    assert "MODIFY COLUMN" not in sql
    assert "segment_start DateTime64" not in sql


def test_exact_start_column_already_at_microseconds_is_not_rewritten():
    """REV-017-001: a column that already keeps microseconds is left in place."""
    writer = _load_event_writer()
    assert writer.recording_exact_start_alter_sql("vms", "Nullable(DateTime64(6, 'UTC'))") is None
    assert writer.recording_exact_start_alter_sql("vms", "Nullable(DateTime64(6,'UTC'))") is None


def test_unexpected_exact_start_type_is_rejected():
    """REV-017-001: a different column type is not altered or narrowed."""
    writer = _load_event_writer()
    with pytest.raises(RuntimeError, match="unexpected type"):
        writer.recording_exact_start_alter_sql("vms", "DateTime64(3, 'UTC')")


def test_exact_start_alter_rejects_an_unsafe_database_name():
    """REV-017-001: the ALTER interpolates only a simple identifier."""
    writer = _load_event_writer()
    with pytest.raises(RuntimeError, match="identifier"):
        writer.recording_exact_start_alter_sql("vms;drop", None)


def test_writer_startup_adds_exact_start_when_the_column_is_missing():
    """REV-017-001: writer startup issues the additive ALTER for an old table."""
    writer = _load_event_writer()
    client = _ClickHouse()
    asyncio.run(writer.init_clickhouse(client))
    assert any(
        "ADD COLUMN IF NOT EXISTS segment_start_exact Nullable(DateTime64(6, 'UTC'))" in body
        for body in client.bodies
    )
    assert all("MODIFY COLUMN" not in body for body in client.bodies)


def test_unmerged_longer_version_loses_inside_the_current_segment(monkeypatch):
    """REV-017-002: at +10s the current 30s version wins, not the unmerged 120s row."""
    start = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    store = IndexStore(_versions(start))
    _install(monkeypatch, store)

    selected = _lookup(store, start + timedelta(seconds=10))

    assert "argMax(duration_seconds, indexed_at)" in store.queries[0]
    assert store.queries[0].index("GROUP BY") < store.queries[0].index("duration_seconds <=")
    assert selected is not None
    assert selected["recording_node_id"] == "node-fresh"
    assert selected["duration_seconds"] == 30


def test_unmerged_longer_version_does_not_cover_past_the_current_end(monkeypatch):
    """REV-017-002: at +60s the replaced 120s row must not win."""
    start = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    store = IndexStore(_versions(start))
    _install(monkeypatch, store)

    selected = _lookup(store, start + timedelta(seconds=60))

    assert "argMax(duration_seconds, indexed_at)" in store.queries[0]
    assert selected is None
    assert all(row.get("recording_node_id") != "node-stale" for row in store.returned)


def test_index_double_drops_other_scope_and_overlong_duration(monkeypatch):
    """REV-017-003: the double enforces tenant, site, camera, and the duration cap."""
    instant = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    in_scope = _segment("in-scope", "node-ok", instant - timedelta(seconds=20), duration=30)
    other_tenant = _segment("other-tenant", "node-x", instant - timedelta(seconds=5), duration=30)
    other_tenant["tenant_id"] = "other-tenant"
    other_site = _segment("other-site", "node-x", instant - timedelta(seconds=4), duration=30)
    other_site["site_id"] = "other-site"
    other_camera = _segment("other-camera", "node-x", instant - timedelta(seconds=3), duration=30)
    other_camera["camera_id"] = "other-camera"
    over_cap = _segment("over-cap", "node-x", instant - timedelta(seconds=2), duration=24 * 60 * 60 + 1)
    store = IndexStore([in_scope, other_tenant, other_site, other_camera, over_cap])
    _install(monkeypatch, store)

    selected = _lookup(store, instant)

    query = store.queries[0]
    assert "tenant_id = {tenant:String}" in query
    assert "site_id = {site:String}" in query
    assert "camera_id = {camera:String}" in query
    assert "duration_seconds <=" in query
    assert {row["segment_id"] for row in store.returned} == {"in-scope"}
    assert selected is not None
    assert selected["segment_id"] == "in-scope"


def test_timeline_cap_is_partial_when_the_index_page_is_not_exhausted(monkeypatch):
    """REV-017-004: a 168-hour page that stops at the segment cap is partial."""
    _arm_camera(monkeypatch)
    end = datetime(2026, 9, 26, tzinfo=timezone.utc)
    start = end - timedelta(hours=168)
    monkeypatch.setattr(recordings.settings, "recording_query_max_window_hours", 168)
    monkeypatch.setattr(recordings.settings, "recording_query_max_segments", 10000)

    async def segments(**kwargs):
        assert kwargs["limit"] == 10000
        assert kwargs["end"] - kwargs["start"] == timedelta(hours=168)
        return RecordingIndexPage(
            [
                {
                    "segment_id": "seg-1",
                    "segment_start": start,
                    "segment_end": start + timedelta(minutes=1),
                    "recording_node_id": "node-1",
                }
            ],
            skipped_rows=0,
            exhausted=False,
            next_after_segment_start=start,
            next_after_segment_id="seg-1",
        )

    async def playback(*args, **kwargs):
        class _Client:
            async def list_timespans(self, path, span_start, span_end):
                return []

        return _Client()

    monkeypatch.setattr(recordings.recording_index, "segments", segments)
    monkeypatch.setattr(recordings, "_playback_for_policy", playback)
    response = Response()

    body = asyncio.run(
        recordings.timeline(
            response,
            "cam-1",
            start=start,
            end=end,
            session=SimpleNamespace(),
            principal=_principal(),
        )
    )

    assert len(body) == 1
    assert response.headers["X-VMS-Partial"] == "true"
    assert response.headers["X-VMS-Skipped-Rows"] == "0"


def test_year_one_timeline_is_a_client_error(monkeypatch):
    """REV-017-005: a year-1 timeline does not raise OverflowError."""
    _arm_camera(monkeypatch)
    response = Response()
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            recordings.timeline(
                response,
                "cam-1",
                start=YEAR_ONE,
                end=YEAR_ONE + timedelta(hours=1),
                session=SimpleNamespace(),
                principal=_principal(),
            )
        )
    assert caught.value.status_code == 422
    assert "outside the supported range" in str(caught.value.detail)


def test_year_one_playback_is_a_client_error(monkeypatch):
    """REV-017-005: a year-1 play instant does not raise OverflowError."""
    _arm_camera(monkeypatch)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            recordings.play(
                _request(),
                "cam-1",
                start=YEAR_ONE,
                duration=1,
                session=SimpleNamespace(),
                principal=_principal(),
            )
        )
    assert caught.value.status_code == 422
    assert "outside the supported range" in str(caught.value.detail)


def test_year_one_export_is_a_client_error(monkeypatch):
    """REV-017-005: a year-1 export does not raise OverflowError."""
    _arm_camera(monkeypatch)
    camera = SimpleNamespace(id="cam-1", tenant_id="tenant-1", site_id="site-1")
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            recordings.stream_recording_clip(
                _request(),
                camera,
                YEAR_ONE,
                1,
                SimpleNamespace(),
            )
        )
    assert caught.value.status_code == 422
    assert caught.value.detail == "recording time is outside the supported range"
