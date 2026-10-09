"""Ordinary playback must time out a stalled recorder.

The upstream is a local asyncio server. Nothing here contacts a third-party
host. Tests that should hang on the unbounded client use asyncio.wait_for so
the old behavior fails instead of stalling CI.
"""

import asyncio
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import ClientDisconnect, Request

from app.core.auth import Principal
from app.core.config import Settings, settings
from app.routers import recordings
from app.services import playback as playback_module
from app.services.playback import PlaybackClient, PlaybackError


HANG_GUARD_SECONDS = 3.0
ROOT = Path(__file__).parents[1]
PLAYBACK_SOURCE = (
    ROOT / "services" / "control-api" / "app" / "services" / "playback.py"
).read_text(encoding="utf-8")
RECORDINGS_SOURCE = (
    ROOT / "services" / "control-api" / "app" / "routers" / "recordings.py"
).read_text(encoding="utf-8")


class LocalUpstream:
    """Local HTTP server that stalls, returns a short body, or refuses playback."""

    def __init__(self, mode: str):
        self.mode = mode
        self.connections = 0
        self.closes = 0
        self.accepted = asyncio.Event()
        self.closed = asyncio.Event()
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []

    async def __aenter__(self) -> "LocalUpstream":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        for writer in list(self._writers):
            writer.close()
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=1)
            except TimeoutError:
                pass

    @property
    def base_url(self) -> str:
        assert self._server is not None and self._server.sockets
        host, port = self._server.sockets[0].getsockname()[:2]
        return f"http://{host}:{port}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writers.append(writer)
        try:
            while True:
                line = await reader.readline()
                if line in (b"", b"\r\n"):
                    break
            self.accepted.set()
            if self.mode == "silent":
                await reader.read()
            elif self.mode == "headers":
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: video/mp4\r\nContent-Length: 64\r\n\r\n"
                )
                await writer.drain()
                await reader.read()
            elif self.mode == "midbody":
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: video/mp4\r\n"
                    b"Transfer-Encoding: chunked\r\n"
                    b"\r\n"
                    b"4\r\npart\r\n"
                )
                await writer.drain()
                await reader.read()
            elif self.mode == "ok":
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: video/mp4\r\nContent-Length: 4\r\n\r\npart"
                )
                await writer.drain()
                await reader.read()
            elif self.mode == "missing":
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                await reader.read()
            else:
                raise AssertionError(self.mode)
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError, ConnectionAbortedError):
            pass
        finally:
            self.closes += 1
            self.closed.set()
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


def _spy_clients(monkeypatch):
    created: list[tuple[object, httpx.AsyncClient]] = []
    original = playback_module.httpx.AsyncClient

    def factory(*args, **kwargs):
        client = original(*args, **kwargs)
        created.append((kwargs.get("timeout"), client))
        return client

    monkeypatch.setattr(playback_module.httpx, "AsyncClient", factory)
    return created


def _use_playback_timeouts(monkeypatch, connect: float, read: float) -> None:
    if not hasattr(settings, "recording_playback_connect_timeout_seconds"):
        return
    monkeypatch.setattr(settings, "recording_playback_connect_timeout_seconds", connect)
    monkeypatch.setattr(settings, "recording_playback_read_timeout_seconds", read)


def _arm(monkeypatch, playback: PlaybackClient, start: datetime) -> None:
    async def camera(*args, **kwargs):
        return SimpleNamespace(id="cam-1", tenant_id="tenant-1", site_id="site-1")

    async def policy(*args, **kwargs):
        return SimpleNamespace(record_stream_key="cam-record", recording_node_id="node-current")

    async def current(*args, **kwargs):
        return playback

    async def list_timespans(path, span_start, end):
        del path, end
        return [{"start": span_start.isoformat(), "duration": 30}]

    playback.list_timespans = list_timespans
    monkeypatch.setattr(recordings.settings, "placement_execution_enabled", False)
    monkeypatch.setattr(recordings, "authorized_camera", camera)
    monkeypatch.setattr(recordings, "_policy_for_camera", policy)
    monkeypatch.setattr(recordings, "_playback_for_policy", current)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
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


def _principal() -> Principal:
    return Principal(
        subject="viewer-1",
        roles=frozenset({"viewer"}),
        tenant_id="tenant-1",
        site_ids=frozenset({"site-1"}),
    )


def _start() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=1)


async def _bounded(awaitable):
    try:
        return await asyncio.wait_for(awaitable, timeout=HANG_GUARD_SECONDS)
    except TimeoutError as exc:
        raise AssertionError(
            "playback hung on a local stalled upstream; the unbounded timeout is still in effect"
        ) from exc


def _scope() -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
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


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def _drive(response) -> list[dict]:
    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    await _bounded(response(_scope(), _receive, send))
    return messages


async def _closed_port() -> int:
    server = await asyncio.start_server(lambda reader, writer: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return port


def test_playback_timeout_defaults_are_positive_and_finite():
    """Defaults are 5s connect and 30s read, both finite and greater than zero."""
    cfg = Settings()
    connect = getattr(cfg, "recording_playback_connect_timeout_seconds", None)
    read = getattr(cfg, "recording_playback_read_timeout_seconds", None)
    assert connect == 5.0
    assert read == 30.0
    assert math.isfinite(connect) and connect > 0
    assert math.isfinite(read) and read > 0


def test_playback_timeout_settings_reject_non_positive_and_non_finite_values():
    """Zero, negative, NaN, infinity, bool, and values above 300 are rejected."""
    if not hasattr(Settings(), "recording_playback_connect_timeout_seconds"):
        pytest.fail("recording playback connect/read timeouts are not configured")
    for value in (0, -1, float("inf"), float("-inf"), float("nan"), 301, True, "inf"):
        with pytest.raises(ValidationError):
            Settings(recording_playback_connect_timeout_seconds=value)
        with pytest.raises(ValidationError):
            Settings(recording_playback_read_timeout_seconds=value)


def test_playback_timeout_settings_accept_fractional_ceiling_and_numeric_strings():
    """Sub-second values, the 300s ceiling, and numeric strings are valid."""
    cfg = Settings(
        recording_playback_connect_timeout_seconds="5.5",
        recording_playback_read_timeout_seconds=0.001,
    )
    assert getattr(cfg, "recording_playback_connect_timeout_seconds", None) == 5.5
    ceiling = Settings(
        recording_playback_connect_timeout_seconds=300,
        recording_playback_read_timeout_seconds=300,
    )
    assert getattr(ceiling, "recording_playback_read_timeout_seconds", None) == 300.0
    assert getattr(cfg, "recording_playback_read_timeout_seconds", None) == 0.001


def test_existing_record_path_validator_still_rejects_templates_without_fractional_seconds():
    """The pre-existing recording_path_template field validator still runs."""
    with pytest.raises(ValidationError):
        Settings(recording_path_template="/recordings/%path/%s")


def test_play_and_proxy_sources_have_no_unbounded_timeout():
    """No play or proxy AsyncClient is constructed with a None timeout."""
    assert "AsyncClient(" not in RECORDINGS_SOURCE
    assert "timeout=None" not in PLAYBACK_SOURCE
    assert "timeout=timeout_seconds" not in PLAYBACK_SOURCE
    assert PLAYBACK_SOURCE.count("AsyncClient(") == 2
    assert "httpx.Timeout(" in PLAYBACK_SOURCE
    assert "AsyncClient(timeout=10.0)" in PLAYBACK_SOURCE
    assert "settings.recording_export_io_timeout_seconds" in RECORDINGS_SOURCE


def test_non_finite_runtime_playback_timeout_fails_closed(monkeypatch):
    """A mutated infinite read timeout is rejected before a connection is opened."""
    if not hasattr(settings, "recording_playback_read_timeout_seconds"):
        pytest.fail("recording_playback_read_timeout_seconds is not configured")
    monkeypatch.setattr(settings, "recording_playback_read_timeout_seconds", float("inf"))
    playback = PlaybackClient("http://127.0.0.1:9")

    with pytest.raises(PlaybackError) as caught:
        asyncio.run(
            playback.open_stream("cam-record", _start(), 10, "fmp4"),
        )

    assert caught.value.status_code == 500


def test_unreachable_ordinary_play_is_gateway_error_not_unhandled(monkeypatch):
    """A refused local connection on /play is HTTP 502, not an uncaught client error."""

    async def scenario():
        port = await _closed_port()
        playback = PlaybackClient(f"http://127.0.0.1:{port}")
        _arm(monkeypatch, playback, _start())
        with pytest.raises(HTTPException) as caught:
            await recordings.play(
                _request(),
                "cam-1",
                start=_start(),
                duration=10,
                session=SimpleNamespace(),
                principal=_principal(),
            )
        assert caught.value.status_code == 502

    asyncio.run(scenario())


def test_export_connect_error_is_not_remapped():
    """Export still lets a connection error propagate from the explicit timeout path."""

    async def scenario():
        port = await _closed_port()
        playback = PlaybackClient(f"http://127.0.0.1:{port}")
        with pytest.raises(httpx.ConnectError):
            await playback.open_stream("cam-record", _start(), 10, "mp4", None, 1.0)

    asyncio.run(scenario())


def test_export_timeout_still_uses_one_timeout_and_raises_timeout_exception(monkeypatch):
    """Export keeps a single per-phase timeout and the httpx timeout exception."""

    async def scenario():
        created = _spy_clients(monkeypatch)
        async with LocalUpstream("silent") as server:
            playback = PlaybackClient(server.base_url)
            with pytest.raises(httpx.TimeoutException):
                await _bounded(
                    playback.open_stream("cam-record", _start(), 10, "mp4", None, 0.3),
                )
            await asyncio.wait_for(server.closed.wait(), 1)
        assert server.closes == server.connections == 1
        timeout, client = created[0]
        # Base passed a single float. Head passes httpx.Timeout with that same
        # duration on every phase. Both are the export contract.
        if isinstance(timeout, httpx.Timeout):
            assert timeout.connect == timeout.read == timeout.write == timeout.pool == 0.3
        else:
            assert timeout == 0.3
        assert client.is_closed

    asyncio.run(scenario())


def test_silent_upstream_play_returns_504_and_closes(monkeypatch):
    """A recorder that accepts and never answers becomes HTTP 504 and is closed."""

    async def scenario():
        created = _spy_clients(monkeypatch)
        _use_playback_timeouts(monkeypatch, 0.2, 0.4)
        start = _start()
        async with LocalUpstream("silent") as server:
            playback = PlaybackClient(server.base_url)
            _arm(monkeypatch, playback, start)

            async def attempt():
                with pytest.raises(HTTPException) as caught:
                    await recordings.play(
                        _request(),
                        "cam-1",
                        start=start,
                        duration=10,
                        session=SimpleNamespace(),
                        principal=_principal(),
                    )
                assert caught.value.status_code == 504
                assert "timed out" in str(caught.value.detail)
                await asyncio.wait_for(server.closed.wait(), 1)
                assert server.closes == server.connections == 1
                timeout, client = created[0]
                assert isinstance(timeout, httpx.Timeout)
                assert timeout.connect == 0.2
                assert timeout.read == timeout.write == timeout.pool == 0.4
                assert all(part is not None and math.isfinite(part) and part > 0 for part in (
                    timeout.connect, timeout.read, timeout.write, timeout.pool,
                ))
                assert client.is_closed

            try:
                await asyncio.wait_for(attempt(), timeout=HANG_GUARD_SECONDS)
            except TimeoutError as exc:
                seen = created[0][0] if created else "no-client"
                raise AssertionError(
                    f"ordinary playback hung on a stalled upstream; AsyncClient timeout={seen!r}"
                ) from exc

    asyncio.run(scenario())


def test_headers_then_stall_is_504_before_any_playback_byte(monkeypatch):
    """Headers followed by silence are HTTP 504, and the upstream connection closes."""

    async def scenario():
        created = _spy_clients(monkeypatch)
        _use_playback_timeouts(monkeypatch, 0.2, 0.4)
        start = _start()
        async with LocalUpstream("headers") as server:
            playback = PlaybackClient(server.base_url)
            _arm(monkeypatch, playback, start)
            response = await _bounded(
                recordings.play(
                    _request(),
                    "cam-1",
                    start=start,
                    duration=10,
                    session=SimpleNamespace(),
                    principal=_principal(),
                )
            )
            messages = await _drive(response)
            await asyncio.wait_for(server.closed.wait(), 1)
        start_message = next(item for item in messages if item["type"] == "http.response.start")
        body = b"".join(
            item.get("body", b"") for item in messages if item["type"] == "http.response.body"
        )
        assert start_message["status"] == 504
        payload = json.loads(body)
        assert payload["detail"] == "Recording playback upstream timed out"
        assert payload["error"]["code"] == "HTTP_504"
        assert server.closes == server.connections == 1
        assert created[0][1].is_closed
        assert messages[-1]["more_body"] is False

    asyncio.run(scenario())


def test_midbody_stall_ends_stream_without_an_uncaught_error(monkeypatch):
    """A stall after the first bytes ends the stream and closes both sides."""

    async def scenario():
        created = _spy_clients(monkeypatch)
        _use_playback_timeouts(monkeypatch, 0.2, 0.4)
        start = _start()
        async with LocalUpstream("midbody") as server:
            playback = PlaybackClient(server.base_url)
            _arm(monkeypatch, playback, start)
            response = await _bounded(
                recordings.play(
                    _request(),
                    "cam-1",
                    start=start,
                    duration=10,
                    session=SimpleNamespace(),
                    principal=_principal(),
                )
            )
            messages = await _drive(response)
            await asyncio.wait_for(server.closed.wait(), 1)
        start_message = next(item for item in messages if item["type"] == "http.response.start")
        body = b"".join(
            item.get("body", b"") for item in messages if item["type"] == "http.response.body"
        )
        assert start_message["status"] == 200
        assert b"part" in body
        assert messages[-1]["more_body"] is False
        assert server.closes == server.connections == 1
        assert created[0][1].is_closed

    asyncio.run(scenario())


def test_happy_path_proxies_bytes_and_closes(monkeypatch):
    """A short local recording is proxied and both sides are closed."""

    async def scenario():
        created = _spy_clients(monkeypatch)
        _use_playback_timeouts(monkeypatch, 0.2, 0.4)
        start = _start()
        async with LocalUpstream("ok") as server:
            playback = PlaybackClient(server.base_url)
            _arm(monkeypatch, playback, start)
            response = await _bounded(
                recordings.play(
                    _request(),
                    "cam-1",
                    start=start,
                    duration=10,
                    session=SimpleNamespace(),
                    principal=_principal(),
                )
            )
            messages = await _drive(response)
            await asyncio.wait_for(server.closed.wait(), 1)
        start_message = next(item for item in messages if item["type"] == "http.response.start")
        body = b"".join(
            item.get("body", b"") for item in messages if item["type"] == "http.response.body"
        )
        assert start_message["status"] == 200
        assert body == b"part"
        assert server.closes == server.connections == 1
        assert created[0][1].is_closed

    asyncio.run(scenario())


def test_upstream_404_is_still_playback_error_and_closes(monkeypatch):
    """An upstream 404 is still PlaybackError 404, with the client closed."""

    async def scenario():
        created = _spy_clients(monkeypatch)
        async with LocalUpstream("missing") as server:
            playback = PlaybackClient(server.base_url)
            with pytest.raises(PlaybackError) as caught:
                await _bounded(playback.open_stream("cam-record", _start(), 10, "fmp4"))
            await asyncio.wait_for(server.closed.wait(), 1)
        assert caught.value.status_code == 404
        assert server.closes == server.connections == 1
        assert created[0][1].is_closed

    asyncio.run(scenario())


def test_cancel_during_connect_closes_the_upstream_client(monkeypatch):
    """Cancelling ordinary play while the recorder is silent closes the client."""

    async def scenario():
        created = _spy_clients(monkeypatch)
        _use_playback_timeouts(monkeypatch, 1.0, 5.0)
        start = _start()
        async with LocalUpstream("silent") as server:
            playback = PlaybackClient(server.base_url)
            _arm(monkeypatch, playback, start)
            task = asyncio.create_task(
                recordings.play(
                    _request(),
                    "cam-1",
                    start=start,
                    duration=10,
                    session=SimpleNamespace(),
                    principal=_principal(),
                )
            )
            await asyncio.wait_for(server.accepted.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert created and created[0][1].is_closed
            await asyncio.wait_for(server.closed.wait(), 2)
        assert server.closes == server.connections == 1

    asyncio.run(scenario())


def test_client_disconnect_during_body_closes_upstream(monkeypatch):
    """Disconnecting after the first bytes closes the upstream connection."""

    async def scenario():
        created = _spy_clients(monkeypatch)
        _use_playback_timeouts(monkeypatch, 1.0, 5.0)
        start = _start()
        async with LocalUpstream("midbody") as server:
            playback = PlaybackClient(server.base_url)
            _arm(monkeypatch, playback, start)
            response = await recordings.play(
                _request(),
                "cam-1",
                start=start,
                duration=10,
                session=SimpleNamespace(),
                principal=_principal(),
            )
            released = asyncio.Event()

            async def send(message):
                if message["type"] == "http.response.body" and message.get("body"):
                    released.set()
                    raise OSError("client disconnected")

            task = asyncio.create_task(response(_scope(), _receive, send))
            await asyncio.wait_for(released.wait(), 2)
            try:
                await asyncio.wait_for(task, 2)
            except (OSError, ClientDisconnect):
                pass
            assert created and created[0][1].is_closed
            await asyncio.wait_for(server.closed.wait(), 2)
        assert server.closes == server.connections == 1

    asyncio.run(scenario())
