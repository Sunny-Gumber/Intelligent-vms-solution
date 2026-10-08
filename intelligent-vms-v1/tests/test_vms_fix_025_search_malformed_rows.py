"""Malformed search and recording-index rows must not truncate a page."""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from app.models.schemas import EventHistoryPage
from app.services.event_search import EventSearchClient
from app.services.recording_index import RecordingIndexClient

SECRET_TEXT = "rtsp://user:super-secret@10.0.0.5/camera"
START = datetime(2026, 9, 26, tzinfo=timezone.utc)
END = datetime(2026, 9, 27, tzinfo=timezone.utc)


class _Response:
    def __init__(self, lines):
        self.text = "\n".join(lines)

    def raise_for_status(self):
        return None


class _StoreClient:
    """Fake ClickHouse that honors limit, offset, and the keyset cursor."""

    def __init__(self, lines, key_of, start_of):
        self.lines = list(lines)
        self.key_of = key_of
        self.start_of = start_of

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, _url, params=None, content=None):
        del content
        params = params or {}
        limit = int(params["param_limit"])
        offset = int(params.get("param_offset", "0"))
        start = self.start_of(self.lines, params)
        window = self.lines[start:]
        return _Response(window[offset:offset + limit])


def _install(monkeypatch, module, lines, key_of, start_of):
    client = _StoreClient(lines, key_of, start_of)

    def factory(*args, **kwargs):
        del args, kwargs
        return client

    monkeypatch.setattr(module.httpx, "AsyncClient", factory)
    return client


def _event_key(line):
    try:
        row = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(row, dict):
        return None
    event_id = row.get("event_id")
    timestamp = row.get("timestamp")
    if not isinstance(event_id, str) or not isinstance(timestamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed, event_id)


def _event_start(lines, params):
    if "param_before_timestamp" not in params:
        return 0
    before = (
        datetime.fromisoformat(params["param_before_timestamp"]),
        params.get("param_before_event_id", ""),
    )
    for index, line in enumerate(lines):
        key = _event_key(line)
        if key is None:
            continue
        if key == before:
            return index + 1
        if key < before:
            cursor = index
            while cursor > 0 and _event_key(lines[cursor - 1]) is None:
                cursor -= 1
            return cursor
    return len(lines)


def _segment_key(line):
    try:
        row = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(row, dict):
        return None
    segment_id = row.get("segment_id")
    raw_start = row.get("segment_start")
    if isinstance(segment_id, bool) or not isinstance(segment_id, (str, int)):
        return None
    if not isinstance(raw_start, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if not str(segment_id) or len(str(segment_id)) > 128:
        return None
    return (parsed, str(segment_id))


def _segment_start(lines, params):
    if "param_after_start" not in params:
        return 0
    after = (
        datetime.fromisoformat(params["param_after_start"]),
        params.get("param_after_segment_id", ""),
    )
    for index, line in enumerate(lines):
        key = _segment_key(line)
        if key is None:
            continue
        if key == after:
            return index + 1
        if key > after:
            cursor = index
            while cursor > 0 and _segment_key(lines[cursor - 1]) is None:
                cursor -= 1
            return cursor
    return len(lines)


def _event_line(event_id, hour):
    return json.dumps(
        {
            "event_id": event_id,
            "tenant_id": "tenant-1",
            "site_id": "site-1",
            "camera_id": "cam-1",
            "timestamp": f"2026-09-26T{hour:02d}:00:00+00:00",
            "event_type": "motion",
            "source": "camera",
            "severity": "info",
            "attributes_json": "{}",
        }
    )


def _bad_event_line(event_id, hour):
    payload = {
        "event_id": event_id,
        "tenant_id": "tenant-1",
        "site_id": "site-1",
        "camera_id": "cam-1",
        "timestamp": f"2026-09-26T{hour:02d}:30:00+00:00",
        "event_type": "motion",
        "source": "camera",
        "severity": "info",
        "attributes_json": '{"password":"' + SECRET_TEXT + '"',
    }
    return json.dumps(payload)


def _segment_line(segment_id, minute, duration=60):
    start = START + timedelta(minutes=minute)
    return json.dumps(
        {
            "segment_id": segment_id,
            "recording_node_id": "node-a",
            "record_stream_key": "cam-record",
            "segment_path": f"/recordings/{segment_id}.mp4",
            "segment_start": start.isoformat(),
            "duration_seconds": duration,
            "completed_at": start.isoformat(),
            "storage_tier": "hot",
            "object_uri": None,
        }
    )


def _bad_segment_line(segment_id, minute):
    start = START + timedelta(minutes=minute)
    return json.dumps(
        {
            "segment_id": segment_id,
            "segment_start": start.isoformat(),
            "segment_path": SECRET_TEXT,
        }
    )


def _counter(source, reason):
    from app.services.search_page import ROWS_SKIPPED

    total = 0
    for metric in ROWS_SKIPPED.collect():
        for sample in metric.samples:
            labels = sample.labels
            if labels.get("source") == source and labels.get("reason") == reason and sample.name.endswith("_total"):
                total += sample.value
    return total


def _search(client, *, limit, before=None, before_id=None):
    return client.search(
        tenant_id="tenant-1",
        allowed_sites=None,
        site_id="site-1",
        camera_id=None,
        event_type=None,
        severity=None,
        start=START,
        end=END,
        limit=limit,
        before_timestamp=before,
        before_event_id=before_id,
    )


def test_history_page_defaults_keep_the_existing_cursor_contract():
    page = EventHistoryPage()
    assert page.partial is False
    assert page.skipped_rows == 0
    assert page.next_before is None
    assert page.next_before_id is None
    from pathlib import Path

    root = Path(__file__).parents[1]
    events = (root / "services/control-api/app/routers/events.py").read_text(encoding="utf-8")
    recordings = (root / "services/control-api/app/routers/recordings.py").read_text(encoding="utf-8")
    assert "partial=partial" in events
    assert "skipped_rows=skipped" in events
    assert "apply_page_headers(response, rows)" in events
    assert "apply_page_headers(response, rows)" in recordings
    assert "apply_page_headers(response, None)" in recordings


def test_event_boundary_skip_does_not_look_like_the_last_page(monkeypatch):
    """A malformed probe row must not hide the valid row that follows it."""
    lines = [
        _event_line("e4", 4),
        _event_line("e3", 3),
        _bad_event_line("bad", 2),
        _event_line("e1", 1),
        "{not-json " + SECRET_TEXT,
        _event_line("e0", 0),
    ]
    import app.services.event_search as module

    _install(monkeypatch, module, lines, _event_key, _event_start)
    client = EventSearchClient()
    page_size = 2
    first = asyncio.run(_search(client, limit=page_size + 1))

    assert len(first) > page_size
    assert [row["event_id"] for row in first[:page_size]] == ["e4", "e3"]
    assert first.partial is True
    assert first.skipped_rows >= 1


def test_event_walk_returns_every_valid_row_once(monkeypatch, caplog):
    lines = [
        _event_line("e4", 4),
        _event_line("e3", 3),
        _bad_event_line("bad", 2),
        _event_line("e1", 1),
        "{not-json " + SECRET_TEXT,
        _event_line("e0", 0),
    ]
    import app.services.event_search as module

    _install(monkeypatch, module, lines, _event_key, _event_start)
    client = EventSearchClient()
    page_size = 2
    before = None
    before_id = None
    seen = []
    skipped = 0
    with caplog.at_level(logging.WARNING):
        for _ in range(8):
            page = asyncio.run(_search(client, limit=page_size + 1, before=before, before_id=before_id))
            assert page.skipped_rows >= 0
            skipped += page.skipped_rows
            has_more = len(page) > page_size
            items = list(page[:page_size])
            seen.extend(row["event_id"] for row in items)
            if not has_more:
                assert page.exhausted is True
                break
            last = items[-1]
            before = datetime.fromisoformat(last["timestamp"])
            before_id = last["event_id"]
        else:
            raise AssertionError("event cursor did not reach the end")

    assert seen == ["e4", "e3", "e1", "e0"]
    assert len(seen) == len(set(seen))
    assert skipped >= 2
    assert SECRET_TEXT not in caplog.text
    assert "bad" in caplog.text
    assert _counter("event_search", "invalid_row") >= 1
    assert _counter("event_search", "invalid_json") >= 1


def test_recording_boundary_skip_does_not_look_like_the_last_page(monkeypatch):
    lines = [
        _segment_line("a", 0),
        _bad_segment_line("bad", 1),
        _segment_line("b", 2),
        "{not-json " + SECRET_TEXT,
        _segment_line("c", 4),
    ]
    import app.services.recording_index as module

    _install(monkeypatch, module, lines, _segment_key, _segment_start)
    client = RecordingIndexClient()
    first = asyncio.run(
        client.segments(
            tenant_id="tenant-1",
            site_id="site-1",
            camera_id="cam-1",
            start=START,
            end=END,
            limit=2,
        )
    )

    assert [row["segment_id"] for row in first] == ["a", "b"]
    assert len(first) == 2
    assert first.partial is True
    assert first.skipped_rows >= 1
    assert first.next_after_segment_id == "b"


def test_recording_walk_returns_every_valid_segment_once(monkeypatch, caplog):
    lines = [
        _segment_line("a", 0),
        _bad_segment_line("bad", 1),
        _segment_line("b", 2),
        "{not-json " + SECRET_TEXT,
        _segment_line("c", 4),
    ]
    import app.services.recording_index as module

    _install(monkeypatch, module, lines, _segment_key, _segment_start)
    client = RecordingIndexClient()
    after_start = None
    after_id = None
    seen = []
    skipped = 0
    with caplog.at_level(logging.WARNING):
        for _ in range(8):
            page = asyncio.run(
                client.segments(
                    tenant_id="tenant-1",
                    site_id="site-1",
                    camera_id="cam-1",
                    start=START,
                    end=END,
                    limit=2,
                    after_segment_start=after_start,
                    after_segment_id=after_id,
                )
            )
            seen.extend(row["segment_id"] for row in page)
            skipped += page.skipped_rows
            if page.next_after_segment_id is None:
                assert page.exhausted is True
                break
            after_start = page.next_after_segment_start
            after_id = page.next_after_segment_id
        else:
            raise AssertionError("recording cursor did not reach the end")

    assert seen == ["a", "b", "c"]
    assert len(seen) == len(set(seen))
    assert skipped >= 2
    assert SECRET_TEXT not in caplog.text
    assert "bad" in caplog.text
    assert "/recordings/" not in caplog.text
    assert _counter("recording_index", "invalid_row") >= 1
    assert _counter("recording_index", "invalid_json") >= 1


def test_final_short_page_has_no_cursor_after_a_trailing_skip(monkeypatch):
    lines = [_segment_line("a", 0), _bad_segment_line("bad", 1)]
    import app.services.recording_index as module

    _install(monkeypatch, module, lines, _segment_key, _segment_start)
    client = RecordingIndexClient()
    page = asyncio.run(
        client.segments(
            tenant_id="tenant-1",
            site_id="site-1",
            camera_id="cam-1",
            start=START,
            end=END,
            limit=2,
        )
    )

    assert [row["segment_id"] for row in page] == ["a"]
    assert page.partial is True
    assert page.skipped_rows == 1
    assert page.exhausted is True
    assert page.next_after_segment_id is None
    assert page.next_after_segment_start is None
