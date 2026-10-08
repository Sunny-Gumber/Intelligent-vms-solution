"""VMS-FIX-030: an unexpected reconnect-benchmark worker must not hang the run.

``drive`` used to enqueue one sentinel per worker and then block on
``queue.join()``. A worker that dies on an unexpected ``attempt`` error never
takes its sentinel, so that join waits forever. These tests inject that failure
with fakes only. Each case has a hard timeout so a regression fails instead of
hanging CI.

Expected per-attempt TCP failures are a different path: ``attempt`` turns
``OSError`` and ``asyncio.TimeoutError`` into measured failures. Those must
still produce a normal result, not a failed run.
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

TOOLS = Path(__file__).parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import phase8_reconnect_benchmark as reconnect_bench


RUN_TIMEOUT_SECONDS = 2.0
HARD_TIMEOUT_SECONDS = 8.0
BENCHMARK_BUG = "benchmark bug"
RESULT_KEYS = {
    "schema_version",
    "benchmark_id",
    "environment",
    "workload",
    "result",
    "resources",
}
WORKLOAD_KEYS = {"type", "config", "started_at", "warmup_seconds", "duration_seconds"}
CONFIG_KEYS = {"host", "port", "attempts", "concurrency", "timeout_seconds"}
MEASURED_RESULT_KEYS = {
    "operations_ok",
    "operations_failed",
    "failure_rate",
    "throughput_ops_s",
    "latency",
}
LATENCY_KEYS = {"count", "p50_ms", "p95_ms", "p99_ms", "max_ms", "mean_ms"}


def _args(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 9,
        "attempts": 4,
        "warmup_attempts": 0,
        "concurrency": 1,
        "timeout": 0.2,
        "sample_interval": 30.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _patch_sampler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep resource sampling local and instantaneous."""

    def sample(self: object) -> dict[str, object]:
        return {
            "monotonic": 0.0,
            "cpu_pct": 1.0,
            "cpu_freq_mhz": None,
            "cpu_freq_max_mhz": None,
            "max_temperature_c": None,
            "temperatures": [],
            "ram_used_bytes": 1024,
            "ram_pct": 1.0,
            "net_rx_mbps": 0.0,
            "net_tx_mbps": 0.0,
            "disk_read_mbps": 0.0,
            "disk_write_mbps": 0.0,
            "gpu": [],
        }

    monkeypatch.setattr(reconnect_bench.SystemSampler, "sample", sample)


def _run_bounded(coro: object) -> object:
    """Run one coroutine with a hard timeout that CI cannot hang past.

    ``asyncio.wait_for`` bounds a cooperative hang. ``faulthandler`` exits the
    process if that timeout is swallowed, because a stuck worker must fail the
    test rather than block the suite.
    """
    faulthandler.dump_traceback_later(HARD_TIMEOUT_SECONDS, exit=True)
    try:
        return asyncio.run(coro)  # type: ignore[arg-type]
    finally:
        faulthandler.cancel_dump_traceback_later()


def _pending_tasks(before: set[asyncio.Task]) -> list[asyncio.Task]:
    current = asyncio.current_task()
    return [
        task
        for task in asyncio.all_tasks()
        if task is not current and task not in before and not task.done()
    ]


def _thread_ids() -> set[int | None]:
    return {thread.ident for thread in threading.enumerate() if thread.is_alive()}


async def _expect_worker_bug(args: SimpleNamespace) -> None:
    """Require a bounded run failure and no leftover worker or sampler task."""
    before = set(asyncio.all_tasks())
    try:
        await asyncio.wait_for(reconnect_bench.run(args), timeout=RUN_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        pytest.fail(f"reconnect benchmark hung after unexpected worker exception: {exc}")
    except RuntimeError as exc:
        assert BENCHMARK_BUG in str(exc)
    else:
        pytest.fail("reconnect benchmark returned a result after an unexpected worker exception")
    assert _pending_tasks(before) == []


def _assert_no_new_threads(before: set[int | None]) -> None:
    spawned = [
        thread.name
        for thread in threading.enumerate()
        if thread.is_alive() and thread.ident not in before
    ]
    assert spawned == []


def test_unexpected_exception_with_concurrency_one_fails_run(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One worker that dies must fail the run before the queue can block forever."""
    _patch_sampler(monkeypatch)

    async def broken_attempt(*_args: object, **_kwargs: object) -> tuple[bool, float]:
        raise RuntimeError(BENCHMARK_BUG)

    monkeypatch.setattr(reconnect_bench, "attempt", broken_attempt)
    args = _args(concurrency=1, attempts=8)

    async def body() -> None:
        with caplog.at_level(logging.ERROR, logger=reconnect_bench.__name__):
            await _expect_worker_bug(args)

    before_threads = _thread_ids()
    _run_bounded(body())
    _assert_no_new_threads(before_threads)
    assert "reconnect_benchmark_worker_failed" in caplog.text


def test_unexpected_exception_with_concurrency_above_one_cancels_siblings(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed worker must cancel in-flight siblings so they cannot stay parked."""
    _patch_sampler(monkeypatch)
    concurrency = 4
    entered = 0
    cancelled: set[int] = set()
    gate = asyncio.Event()
    parked = asyncio.Event()
    lock = asyncio.Lock()

    async def broken_attempt(*_args: object, **_kwargs: object) -> tuple[bool, float]:
        nonlocal entered
        async with lock:
            entered += 1
            current = entered
            if entered >= concurrency:
                gate.set()
        try:
            await gate.wait()
            if current == 1:
                raise RuntimeError(BENCHMARK_BUG)
            await parked.wait()
            raise AssertionError("sibling attempt finished without cancellation")
        except asyncio.CancelledError:
            if current != 1:
                cancelled.add(current)
            raise

    monkeypatch.setattr(reconnect_bench, "attempt", broken_attempt)
    args = _args(concurrency=concurrency, attempts=concurrency)

    async def body() -> None:
        with caplog.at_level(logging.ERROR, logger=reconnect_bench.__name__):
            await _expect_worker_bug(args)
        assert cancelled == set(range(2, concurrency + 1))

    before_threads = _thread_ids()
    _run_bounded(body())
    _assert_no_new_threads(before_threads)
    assert "reconnect_benchmark_worker_failed" in caplog.text


def test_unexpected_exception_on_first_attempt_fails_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first measured attempt raising must fail the run, not record a partial success."""
    _patch_sampler(monkeypatch)
    calls = 0
    lock = asyncio.Lock()

    async def broken_attempt(*_args: object, **_kwargs: object) -> tuple[bool, float]:
        nonlocal calls
        async with lock:
            calls += 1
            current = calls
        if current == 1:
            raise RuntimeError(BENCHMARK_BUG)
        return True, 0.01

    monkeypatch.setattr(reconnect_bench, "attempt", broken_attempt)
    args = _args(concurrency=2, attempts=6)

    _run_bounded(_expect_worker_bug(args))


def test_unexpected_exception_on_last_attempt_fails_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bug on the final attempt must fail the run after earlier attempts were measured."""
    _patch_sampler(monkeypatch)
    attempts = 6
    calls = 0
    lock = asyncio.Lock()

    async def broken_attempt(*_args: object, **_kwargs: object) -> tuple[bool, float]:
        nonlocal calls
        async with lock:
            calls += 1
            current = calls
        if current == attempts:
            raise RuntimeError(BENCHMARK_BUG)
        return True, 0.01

    monkeypatch.setattr(reconnect_bench, "attempt", broken_attempt)
    args = _args(concurrency=2, attempts=attempts)

    _run_bounded(_expect_worker_bug(args))


def test_clean_run_returns_the_same_result_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run with no worker bug still returns Phase 8 evidence and ignores warmup."""
    _patch_sampler(monkeypatch)

    async def healthy_attempt(*_args: object, **_kwargs: object) -> tuple[bool, float]:
        return True, 0.01

    monkeypatch.setattr(reconnect_bench, "attempt", healthy_attempt)
    args = _args(concurrency=2, attempts=4, warmup_attempts=2)

    async def body() -> dict:
        before = set(asyncio.all_tasks())
        result = await asyncio.wait_for(reconnect_bench.run(args), timeout=RUN_TIMEOUT_SECONDS)
        assert _pending_tasks(before) == []
        return result

    before_threads = _thread_ids()
    result = _run_bounded(body())
    _assert_no_new_threads(before_threads)

    assert set(result) == RESULT_KEYS
    assert result["schema_version"] == "phase8-benchmark-v1"
    assert set(result["workload"]) == WORKLOAD_KEYS
    assert result["workload"]["type"] == "tcp-reconnect-storm"
    assert set(result["workload"]["config"]) == CONFIG_KEYS
    assert result["workload"]["config"] == {
        "host": "127.0.0.1",
        "port": 9,
        "attempts": 4,
        "concurrency": 2,
        "timeout_seconds": 0.2,
    }
    assert result["workload"]["warmup_seconds"] >= 0.0
    assert set(result["result"]) == MEASURED_RESULT_KEYS
    assert result["result"]["operations_ok"] == 4
    assert result["result"]["operations_failed"] == 0
    assert result["result"]["failure_rate"] == 0.0
    assert set(result["result"]["latency"]) == LATENCY_KEYS
    assert result["result"]["latency"]["count"] == 4
    assert isinstance(result["resources"], dict)
    assert "samples" in result["resources"]


def test_expected_per_attempt_failures_stay_measurements(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """OSError and timeout from a connect are counts, not a failed benchmark run."""
    _patch_sampler(monkeypatch)
    calls = 0
    lock = asyncio.Lock()

    class _Writer:
        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    async def open_connection(*_args: object, **_kwargs: object) -> tuple[object, _Writer]:
        nonlocal calls
        async with lock:
            calls += 1
            current = calls
        if current % 3 == 1:
            return object(), _Writer()
        if current % 3 == 2:
            raise ConnectionRefusedError("refused")
        raise TimeoutError("timed out")

    monkeypatch.setattr(reconnect_bench.asyncio, "open_connection", open_connection)
    args = _args(concurrency=3, attempts=6)

    async def body() -> dict:
        before = set(asyncio.all_tasks())
        with caplog.at_level(logging.ERROR, logger=reconnect_bench.__name__):
            result = await asyncio.wait_for(reconnect_bench.run(args), timeout=RUN_TIMEOUT_SECONDS)
        assert _pending_tasks(before) == []
        return result

    before_threads = _thread_ids()
    result = _run_bounded(body())
    _assert_no_new_threads(before_threads)

    assert "reconnect_benchmark_worker_failed" not in caplog.text
    assert set(result) == RESULT_KEYS
    assert result["result"]["operations_ok"] == 2
    assert result["result"]["operations_failed"] == 4
    assert result["result"]["failure_rate"] == pytest.approx(4 / 6)
    assert result["result"]["latency"]["count"] == 6
