"""Single-node export must use the shared playback timespan parser.

Upstream items are synthetic dictionaries. These tests do not open a socket,
a database, or a recorder. A bad span must not raise, and it must not be
treated as coverage that bridges a gap.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.routers import recordings


ROOT = Path(__file__).parents[1]
RECORDINGS_SOURCE = (
    ROOT / "services" / "control-api" / "app" / "routers" / "recordings.py"
).read_text(encoding="utf-8")
COVERAGE_DETAIL = "Requested clip does not have continuous recording coverage"
MALFORMED_MARKER = "synthetic-malformed-span"
WINDOW_SECONDS = 60.0
EXPORT_TIMEOUT_SECONDS = 17.0
PLAYBACK_READ_TIMEOUT_SECONDS = 8.0
BAD_KINDS = ("naive", "nan", "inf", "negative_inf", "overflowing", "malformed")


def _single_node_export_branch(source: str) -> str:
    body = source.split("async def stream_recording_clip", 1)[1]
    body = body.split("def _bounded_segment_seconds", 1)[0]
    marker = "else:\n        playback = await _playback_for_policy(session, policy)"
    branch = body[body.index(marker):]
    return branch[: branch.index("await asyncio.wait_for(_export_slots.acquire()")]


def _window() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=2)


def _aware(moment: datetime) -> str:
    return moment.isoformat()


def _naive(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def _bad_item(kind: str, moment: datetime, duration: float) -> dict:
    """One upstream item that must not become coverage."""
    aware = _aware(moment)
    if kind == "naive":
        return {"start": _naive(moment), "duration": duration}
    if kind == "nan":
        return {"start": aware, "duration": float("nan")}
    if kind == "inf":
        return {"start": aware, "duration": float("inf")}
    if kind == "negative_inf":
        return {"start": aware, "duration": float("-inf")}
    if kind == "overflowing":
        # Finite as a float, but far outside the datetime range.
        return {"start": aware, "duration": 1e20}
    if kind == "malformed":
        return {"start": MALFORMED_MARKER, "duration": duration}
    raise AssertionError(kind)


class _Upstream:
    status_code = 200
    headers = {"content-type": "video/mp4", "content-length": "4"}

    async def aiter_bytes(self):
        yield b"mp4!"

    async def aclose(self):
        return None


class _Client:
    async def aclose(self):
        return None


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/recordings/cameras/cam-1/export",
            "raw_path": b"/api/v1/recordings/cameras/cam-1/export",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 9),
            "server": ("test", 80),
        }
    )


async def _drain(response) -> bytes:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return b"".join(chunks)


def _arm(monkeypatch, items: list[dict], *, allow_stream: bool):
    calls: list[tuple] = []
    listed: dict = {}

    async def list_timespans(path, span_start, end):
        listed["path"] = path
        listed["start"] = span_start
        listed["end"] = end
        return items

    async def open_stream(*args, **kwargs):
        calls.append((args, kwargs))
        if not allow_stream:
            raise AssertionError("export opened a clip without continuous coverage")
        return _Client(), _Upstream()

    playback = SimpleNamespace(list_timespans=list_timespans, open_stream=open_stream)

    async def policy(*args, **kwargs):
        return SimpleNamespace(
            record_stream_key="cam-record",
            recording_node_id="node-local",
            camera_id="cam-1",
        )

    async def playback_for_policy(*args, **kwargs):
        return playback

    monkeypatch.setattr(recordings.settings, "placement_execution_enabled", False)
    monkeypatch.setattr(
        recordings.settings,
        "recording_export_io_timeout_seconds",
        EXPORT_TIMEOUT_SECONDS,
    )
    monkeypatch.setattr(
        recordings.settings,
        "recording_playback_read_timeout_seconds",
        PLAYBACK_READ_TIMEOUT_SECONDS,
    )
    monkeypatch.setattr(recordings, "_policy_for_camera", policy)
    monkeypatch.setattr(recordings, "_playback_for_policy", playback_for_policy)
    return calls, listed


async def _export(start: datetime):
    camera = SimpleNamespace(id="cam-1", tenant_id="tenant-1", site_id="site-1")
    return await recordings.stream_recording_clip(
        _request(),
        camera,
        start,
        WINDOW_SECONDS,
        SimpleNamespace(),
    )


def _assert_controlled_gap(caught: pytest.ExceptionInfo, listed: dict) -> None:
    assert caught.value.status_code == 409
    assert caught.value.detail == COVERAGE_DETAIL
    assert MALFORMED_MARKER not in str(caught.value.detail)
    assert listed["path"] == "cam-record"


def _assert_export_stream(response, body: bytes, calls: list[tuple], start: datetime) -> None:
    assert response.status_code == 200
    assert response.media_type == "video/mp4"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "cam-record" not in disposition
    assert MALFORMED_MARKER not in disposition
    assert body == b"mp4!"
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs == {}
    assert args[0] == "cam-record"
    assert args[1] == start
    assert args[2] == WINDOW_SECONDS
    assert args[3] == "mp4"
    assert args[4] is None
    assert args[5] == EXPORT_TIMEOUT_SECONDS
    assert args[5] != PLAYBACK_READ_TIMEOUT_SECONDS


def test_single_node_export_uses_shared_timespan_parser():
    """The single-node branch must call the playback parser and not its own."""
    branch = _single_node_export_branch(RECORDINGS_SOURCE)
    assert "_playback_timespans(" in branch
    assert "datetime.fromisoformat" not in branch
    assert 'float(item["duration"])' not in branch


@pytest.mark.parametrize("kind", BAD_KINDS)
def test_bad_upstream_span_does_not_bridge_a_gap(monkeypatch, kind):
    """A partial recording plus one bad span stays a gap and stays controlled."""
    start = _window()
    items = [
        _bad_item(kind, start + timedelta(seconds=20), 40),
        {"start": _aware(start), "duration": 20},
    ]
    calls, listed = _arm(monkeypatch, items, allow_stream=False)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(_export(start))

    _assert_controlled_gap(caught, listed)
    assert calls == []


@pytest.mark.parametrize("kind", BAD_KINDS)
def test_bad_upstream_span_alone_is_a_controlled_gap(monkeypatch, kind, caplog):
    """A window whose only span is unusable is a coverage failure, not a clip."""
    start = _window()
    calls, listed = _arm(
        monkeypatch,
        [_bad_item(kind, start, WINDOW_SECONDS)],
        allow_stream=False,
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(HTTPException) as caught:
            asyncio.run(_export(start))

    _assert_controlled_gap(caught, listed)
    assert calls == []
    assert MALFORMED_MARKER not in caplog.text


@pytest.mark.parametrize("kind", BAD_KINDS)
def test_bad_span_does_not_hide_real_continuous_coverage(monkeypatch, kind):
    """One rejected span does not discard a span that really covers the window."""
    start = _window()
    items = [
        _bad_item(kind, start, WINDOW_SECONDS),
        {"start": _aware(start).replace("+00:00", "Z"), "duration": WINDOW_SECONDS},
    ]
    calls, listed = _arm(monkeypatch, items, allow_stream=True)

    async def scenario():
        response = await _export(start)
        body = await _drain(response)
        return response, body

    response, body = asyncio.run(scenario())
    assert listed["path"] == "cam-record"
    _assert_export_stream(response, body, calls, start)


def test_valid_contiguous_spans_still_export_with_export_timeout(monkeypatch):
    """Valid aware spans still export, and the export timeout still governs I/O."""
    start = _window()
    items = [
        {"start": _aware(start).replace("+00:00", "Z"), "duration": 30},
        {"start": _aware(start + timedelta(seconds=30)), "duration": "30"},
    ]
    calls, listed = _arm(monkeypatch, items, allow_stream=True)

    async def scenario():
        response = await _export(start)
        body = await _drain(response)
        return response, body

    response, body = asyncio.run(scenario())
    assert listed["path"] == "cam-record"
    _assert_export_stream(response, body, calls, start)


def test_real_gap_between_valid_spans_stays_a_gap(monkeypatch):
    """Two aware spans with a hole between them do not become one clip."""
    start = _window()
    items = [
        {"start": _aware(start), "duration": 20},
        {"start": _aware(start + timedelta(seconds=40)), "duration": 20},
    ]
    calls, listed = _arm(monkeypatch, items, allow_stream=False)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(_export(start))

    _assert_controlled_gap(caught, listed)
    assert calls == []
