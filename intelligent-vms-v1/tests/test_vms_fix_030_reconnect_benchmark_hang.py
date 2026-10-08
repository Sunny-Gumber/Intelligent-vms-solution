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
import subprocess
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


class Boom(BaseException):
    """Base exception used to prove non-Exception worker failures are not measured."""


def _count_results(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count build_result calls without changing a successful result."""
    calls = {"n": 0}
    real = reconnect_bench.build_result

    def wrapped(**kwargs: object) -> dict:
        calls["n"] += 1
        return real(**kwargs)

    monkeypatch.setattr(reconnect_bench, "build_result", wrapped)
    return calls


async def _expect_failure(args: SimpleNamespace, exc_type: type[BaseException], calls: dict[str, int]) -> None:
    """Require one injected worker failure to raise and leave no tasks or result."""
    before = set(asyncio.all_tasks())
    with pytest.raises(exc_type, match=BENCHMARK_BUG):
        await asyncio.wait_for(reconnect_bench.run(args), timeout=RUN_TIMEOUT_SECONDS)
    assert calls["n"] == 0
    assert _pending_tasks(before) == []


@pytest.mark.parametrize(
    "exc_type",
    [RuntimeError, asyncio.CancelledError, Boom, KeyboardInterrupt],
)
def test_injected_worker_exception_fails_run(
    monkeypatch: pytest.MonkeyPatch,
    exc_type: type[BaseException],
) -> None:
    """Runtime, cancellation, BaseException, and KeyboardInterrupt must not publish."""
    _patch_sampler(monkeypatch)
    calls = _count_results(monkeypatch)

    async def broken_attempt(*_args: object, **_kwargs: object) -> tuple[bool, float]:
        raise exc_type(BENCHMARK_BUG)

    monkeypatch.setattr(reconnect_bench, "attempt", broken_attempt)
    _run_bounded(_expect_failure(_args(concurrency=1, attempts=4), exc_type, calls))


def test_every_attempt_cancelled_error_fails_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrency 1: CancelledError from every attempt must not return ok=0 failed=0."""
    _patch_sampler(monkeypatch)
    calls = _count_results(monkeypatch)

    async def broken_attempt(*_args: object, **_kwargs: object) -> tuple[bool, float]:
        raise asyncio.CancelledError(BENCHMARK_BUG)

    monkeypatch.setattr(reconnect_bench, "attempt", broken_attempt)
    _run_bounded(_expect_failure(_args(concurrency=1, attempts=4), asyncio.CancelledError, calls))


def test_first_attempt_cancelled_error_fails_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrency 2: CancelledError on the first attempt must not publish the other three."""
    _patch_sampler(monkeypatch)
    calls = _count_results(monkeypatch)
    seen = 0
    lock = asyncio.Lock()

    async def broken_attempt(*_args: object, **_kwargs: object) -> tuple[bool, float]:
        nonlocal seen
        async with lock:
            seen += 1
            current = seen
        if current == 1:
            raise asyncio.CancelledError(BENCHMARK_BUG)
        return True, 0.01

    monkeypatch.setattr(reconnect_bench, "attempt", broken_attempt)
    _run_bounded(_expect_failure(_args(concurrency=2, attempts=4), asyncio.CancelledError, calls))


def _cli_script(body: str) -> str:
    return (
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import phase8_reconnect_benchmark as bench\n"
        "def sample(self):\n"
        "    return {'monotonic': 0.0, 'cpu_pct': 0.0, 'cpu_freq_mhz': None,\n"
        "            'cpu_freq_max_mhz': None, 'max_temperature_c': None, 'temperatures': [],\n"
        "            'ram_used_bytes': 1, 'ram_pct': 0.0, 'net_rx_mbps': 0.0, 'net_tx_mbps': 0.0,\n"
        "            'disk_read_mbps': 0.0, 'disk_write_mbps': 0.0, 'gpu': []}\n"
        "bench.SystemSampler.sample = sample\n"
        f"{body}\n"
        "sys.argv = ['phase8_reconnect_benchmark', '--host', '127.0.0.1', '--port', '9',\n"
        "            '--attempts', '4', '--concurrency', '1', '--warmup-attempts', '0',\n"
        "            '--timeout', '0.2', '--sample-interval', '30', '--output-json', sys.argv[2]]\n"
        "bench.main()\n"
    )


def _run_cli(script: str, output: Path) -> subprocess.CompletedProcess[str]:
    """Run the benchmark CLI in a child process with a hard timeout."""
    return subprocess.run(
        [sys.executable, "-c", script, str(TOOLS), str(output)],
        check=False,
        capture_output=True,
        text=True,
        timeout=HARD_TIMEOUT_SECONDS,
    )


def test_cli_cancelled_error_exits_nonzero_without_writing_json(tmp_path: Path) -> None:
    """A CancelledError from attempt must not exit 0 or replace evidence JSON."""
    output = tmp_path / "reconnect.json"
    output.write_text("ORIGINAL_JSON", encoding="utf-8")
    script = _cli_script(
        "async def boom(*_args, **_kwargs):\n"
        "    raise __import__('asyncio').CancelledError('benchmark bug')\n"
        "bench.attempt = boom\n"
    )
    completed = _run_cli(script, output)
    assert completed.returncode != 0
    assert output.read_text(encoding="utf-8") == "ORIGINAL_JSON"
    assert "ok=0 failed=0" not in completed.stdout


def test_system_exit_zero_from_attempt_is_not_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """SystemExit(0) inside attempt must not look like a successful CLI run."""
    _patch_sampler(monkeypatch)
    calls = _count_results(monkeypatch)

    async def broken_attempt(*_args: object, **_kwargs: object) -> tuple[bool, float]:
        raise SystemExit(0)

    monkeypatch.setattr(reconnect_bench, "attempt", broken_attempt)

    async def body() -> None:
        before = set(asyncio.all_tasks())
        with pytest.raises(RuntimeError, match="SystemExit\\(0\\)"):
            await asyncio.wait_for(
                reconnect_bench.run(_args(concurrency=1, attempts=4)),
                timeout=RUN_TIMEOUT_SECONDS,
            )
        assert calls["n"] == 0
        assert _pending_tasks(before) == []

    _run_bounded(body())

    output = tmp_path / "reconnect.json"
    output.write_text("ORIGINAL_JSON", encoding="utf-8")
    script = _cli_script(
        "def boom(*_args, **_kwargs):\n"
        "    raise SystemExit(0)\n"
        "async def attempt(*args, **kwargs):\n"
        "    return boom(*args, **kwargs)\n"
        "bench.attempt = attempt\n"
    )
    completed = _run_cli(script, output)
    assert completed.returncode != 0
    assert output.read_text(encoding="utf-8") == "ORIGINAL_JSON"


def test_sampler_exception_cancels_inflight_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sampler failure must cancel parked attempts instead of waiting them out."""
    calls = _count_results(monkeypatch)
    entered = {"n": 0}
    cancelled = {"n": 0}

    def sample(self: object) -> dict[str, object]:
        if entered["n"] >= 2:
            raise RuntimeError("sampler bug")
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

    async def parked_attempt(*_args: object, **_kwargs: object) -> tuple[bool, float]:
        entered["n"] += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled["n"] += 1
            raise
        return True, 0.01

    monkeypatch.setattr(reconnect_bench.SystemSampler, "sample", sample)
    monkeypatch.setattr(reconnect_bench, "attempt", parked_attempt)

    async def body() -> None:
        before = set(asyncio.all_tasks())
        with pytest.raises(RuntimeError, match="sampler bug"):
            await asyncio.wait_for(
                reconnect_bench.run(_args(concurrency=2, attempts=2, sample_interval=0.1)),
                timeout=RUN_TIMEOUT_SECONDS,
            )
        assert calls["n"] == 0
        assert cancelled["n"] == 2
        assert _pending_tasks(before) == []

    _run_bounded(body())


def test_external_run_cancellation_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """task.cancel() on the whole run stays a cancellation and publishes nothing."""
    _patch_sampler(monkeypatch)
    calls = _count_results(monkeypatch)
    started = asyncio.Event()

    async def parked_attempt(*_args: object, **_kwargs: object) -> tuple[bool, float]:
        started.set()
        await asyncio.Event().wait()
        return True, 0.01

    monkeypatch.setattr(reconnect_bench, "attempt", parked_attempt)

    async def body() -> None:
        before = set(asyncio.all_tasks())
        task = asyncio.create_task(reconnect_bench.run(_args(concurrency=1, attempts=2)))
        await asyncio.wait_for(started.wait(), timeout=RUN_TIMEOUT_SECONDS)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls["n"] == 0
        assert _pending_tasks(before) == []

    _run_bounded(body())
