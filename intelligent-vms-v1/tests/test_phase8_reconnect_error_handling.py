import asyncio
import logging
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import phase8_reconnect_benchmark as reconnect_bench


def _fixed_perf_counter(monkeypatch, *values: float) -> None:
    iterator = iter(values)
    monkeypatch.setattr(reconnect_bench.time, "perf_counter", lambda: next(iterator))


def test_expected_connection_failure_is_counted_without_exception(monkeypatch):
    """Count an expected TCP refusal as a failed attempt with deterministic latency."""
    async def refuse_connection(*_args, **_kwargs):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(reconnect_bench.asyncio, "open_connection", refuse_connection)
    _fixed_perf_counter(monkeypatch, 10.0, 10.25)

    success, elapsed = asyncio.run(reconnect_bench.attempt("127.0.0.1", 8554, 1.0))

    assert success is False
    assert elapsed == 0.25


def test_connection_close_reset_is_logged_but_connection_remains_success(monkeypatch, caplog):
    """Keep peer reset during close observable without reclassifying connection success."""
    class ResettingWriter:
        def close(self):
            return None

        async def wait_closed(self):
            raise ConnectionResetError("peer reset")

    async def open_connection(*_args, **_kwargs):
        return object(), ResettingWriter()

    monkeypatch.setattr(reconnect_bench.asyncio, "open_connection", open_connection)
    _fixed_perf_counter(monkeypatch, 20.0, 20.5)

    with caplog.at_level(logging.DEBUG, logger=reconnect_bench.__name__):
        success, elapsed = asyncio.run(reconnect_bench.attempt("127.0.0.1", 8554, 1.0))

    assert success is True
    assert elapsed == 0.5
    assert "tcp_reconnect_close_failed" in caplog.text


def test_unexpected_reconnect_bug_propagates(monkeypatch):
    """Do not convert an unexpected implementation failure into a network failure count."""
    async def broken_connection(*_args, **_kwargs):
        raise RuntimeError("benchmark bug")

    monkeypatch.setattr(reconnect_bench.asyncio, "open_connection", broken_connection)
    _fixed_perf_counter(monkeypatch, 30.0)

    with pytest.raises(RuntimeError, match="benchmark bug"):
        asyncio.run(reconnect_bench.attempt("127.0.0.1", 8554, 1.0))
