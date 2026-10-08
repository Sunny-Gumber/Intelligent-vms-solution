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


def _cli_script(body: str, *, csv: bool = False) -> str:
    csv_args = ", '--output-csv', sys.argv[3]" if csv else ""
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
        "            '--timeout', '0.2', '--sample-interval', '30', '--output-json', sys.argv[2]"
        f"{csv_args}]\n"
        "bench.main()\n"
    )


def _run_cli(script: str, output: Path, *extra: Path) -> subprocess.CompletedProcess[str]:
    """Run the benchmark CLI in a child process with a hard timeout."""
    return subprocess.run(
        [sys.executable, "-c", script, str(TOOLS), str(output), *[str(item) for item in extra]],
        check=False,
        capture_output=True,
        text=True,
        timeout=HARD_TIMEOUT_SECONDS,
    )


def _assert_cli_failed_without_evidence(completed: subprocess.CompletedProcess[str], output: Path) -> None:
    """A failed CLI run exits non-zero and does not leave JSON evidence behind."""
    assert completed.returncode != 0
    assert not output.exists()
    assert not Path(f"{output}.partial").exists()
    assert "ok=" not in completed.stdout


def test_cli_cancelled_error_exits_nonzero_without_writing_json(tmp_path: Path) -> None:
    """A CancelledError from attempt must not exit 0 or leave JSON evidence."""
    output = tmp_path / "reconnect.json"
    output.write_text("ORIGINAL_JSON", encoding="utf-8")
    script = _cli_script(
        "async def boom(*_args, **_kwargs):\n"
        "    raise __import__('asyncio').CancelledError('benchmark bug')\n"
        "bench.attempt = boom\n"
    )
    completed = _run_cli(script, output)
    _assert_cli_failed_without_evidence(completed, output)


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
    _assert_cli_failed_without_evidence(completed, output)


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


_HEALTHY_SAMPLE: dict[str, object] = {
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

_HEALTHY_ATTEMPT = (
    "async def ok_attempt(*_args, **_kwargs):\n"
    "    return True, 0.0\n"
    "bench.attempt = ok_attempt\n"
)


@pytest.mark.parametrize("exit_code", [0, None, False])
def test_qa_030_101_sampler_success_system_exit_cancels_inflight_attempts(
    monkeypatch: pytest.MonkeyPatch,
    exit_code: object,
) -> None:
    """SystemExit(0), SystemExit(), and SystemExit(False) from the sampler fail the run."""
    calls = _count_results(monkeypatch)
    entered = {"n": 0}
    cancelled = {"n": 0}

    def sample(self: object) -> dict[str, object]:
        if entered["n"] >= 2:
            raise SystemExit(exit_code)
        return dict(_HEALTHY_SAMPLE)

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
        with pytest.raises(RuntimeError, match="sampler raised SystemExit\\(0\\)"):
            await asyncio.wait_for(
                reconnect_bench.run(_args(concurrency=2, attempts=2, sample_interval=0.1)),
                timeout=RUN_TIMEOUT_SECONDS,
            )
        assert calls["n"] == 0
        assert cancelled["n"] == 2
        assert _pending_tasks(before) == []

    _run_bounded(body())


def test_qa_030_101_sampler_cancelled_error_keeps_its_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CancelledError raised by the sampler is the sampler's failure, not an empty cancel."""
    calls = _count_results(monkeypatch)

    def sample(self: object) -> dict[str, object]:
        raise asyncio.CancelledError("sampler cancelled itself")

    async def parked_attempt(*_args: object, **_kwargs: object) -> tuple[bool, float]:
        await asyncio.Event().wait()
        return True, 0.01

    monkeypatch.setattr(reconnect_bench.SystemSampler, "sample", sample)
    monkeypatch.setattr(reconnect_bench, "attempt", parked_attempt)

    async def body() -> None:
        before = set(asyncio.all_tasks())
        with pytest.raises(asyncio.CancelledError, match="sampler cancelled itself"):
            await asyncio.wait_for(
                reconnect_bench.run(_args(concurrency=1, attempts=1)),
                timeout=RUN_TIMEOUT_SECONDS,
            )
        assert calls["n"] == 0
        assert _pending_tasks(before) == []

    _run_bounded(body())


def test_qa_030_101_sampler_keyboard_interrupt_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """KeyboardInterrupt from the sampler stays a cancellation and publishes nothing."""
    calls = _count_results(monkeypatch)

    def sample(self: object) -> dict[str, object]:
        raise KeyboardInterrupt("sampler interrupt")

    async def parked_attempt(*_args: object, **_kwargs: object) -> tuple[bool, float]:
        await asyncio.Event().wait()
        return True, 0.01

    monkeypatch.setattr(reconnect_bench.SystemSampler, "sample", sample)
    monkeypatch.setattr(reconnect_bench, "attempt", parked_attempt)

    async def body() -> None:
        before = set(asyncio.all_tasks())
        with pytest.raises(KeyboardInterrupt, match="sampler interrupt"):
            await asyncio.wait_for(
                reconnect_bench.run(_args(concurrency=1, attempts=1)),
                timeout=RUN_TIMEOUT_SECONDS,
            )
        assert calls["n"] == 0
        assert _pending_tasks(before) == []

    _run_bounded(body())


@pytest.mark.parametrize(
    ("raised", "label"),
    [
        ("SystemExit(0)", "zero"),
        ("SystemExit()", "bare"),
        ("SystemExit(False)", "false"),
        ("SystemExit(1)", "one"),
        ('SystemExit("benchmark bug")', "message"),
    ],
)
def test_qa_030_101_cli_sampler_system_exit_removes_stale_json(tmp_path: Path, raised: str, label: str) -> None:
    """Any SystemExit from the sampler exits non-zero and deletes stale JSON evidence."""
    del label
    output = tmp_path / "reconnect.json"
    csv_path = tmp_path / "reconnect.csv"
    output.write_text("ORIGINAL_JSON", encoding="utf-8")
    csv_path.write_text("ORIGINAL_CSV", encoding="utf-8")
    script = _cli_script(
        "def sample(self):\n"
        f"    raise {raised}\n"
        "bench.SystemSampler.sample = sample\n"
        "async def parked(*_args, **_kwargs):\n"
        "    import asyncio\n"
        "    await asyncio.Event().wait()\n"
        "bench.attempt = parked\n",
        csv=True,
    )
    completed = _run_cli(script, output, csv_path)
    _assert_cli_failed_without_evidence(completed, output)
    assert csv_path.read_text(encoding="utf-8") == "ORIGINAL_CSV"
    if raised in {"SystemExit(0)", "SystemExit()", "SystemExit(False)"}:
        assert "sampler raised SystemExit(0)" in completed.stderr


@pytest.mark.parametrize(
    "body",
    [
        "class BoomSampler:\n"
        "    def __init__(self):\n"
        "        raise SystemExit(0)\n"
        "bench.SystemSampler = BoomSampler\n",
        "def boom_clock():\n"
        "    raise SystemExit(0)\n"
        "bench.utc_iso = boom_clock\n"
        + _HEALTHY_ATTEMPT,
        "import asyncio as aio\n"
        "class BadQueue(aio.Queue):\n"
        "    async def put(self, item):\n"
        "        raise SystemExit(0)\n"
        "bench.asyncio.Queue = BadQueue\n"
        + _HEALTHY_ATTEMPT,
    ],
    ids=["sampler-init", "utc-iso", "enqueue"],
)
def test_qa_030_101_cli_setup_system_exit_zero_removes_stale_json(tmp_path: Path, body: str) -> None:
    """SystemExit(0) while setting up the driver exits non-zero and drops stale JSON."""
    output = tmp_path / "reconnect.json"
    output.write_text("ORIGINAL_JSON", encoding="utf-8")
    completed = _run_cli(_cli_script(body), output)
    _assert_cli_failed_without_evidence(completed, output)
    assert "setup raised SystemExit(0)" in completed.stderr


def test_qa_030_101_cli_teardown_system_exit_zero_removes_stale_json(tmp_path: Path) -> None:
    """SystemExit(0) from drive teardown exits non-zero and drops stale JSON."""
    output = tmp_path / "reconnect.json"
    output.write_text("ORIGINAL_JSON", encoding="utf-8")
    script = _cli_script(
        "def boom_drain(_queue):\n"
        "    raise SystemExit(0)\n"
        "bench._drain_queue = boom_drain\n"
        + _HEALTHY_ATTEMPT
    )
    completed = _run_cli(script, output)
    _assert_cli_failed_without_evidence(completed, output)
    assert "teardown raised SystemExit(0)" in completed.stderr


def test_qa_030_101_cli_build_result_system_exit_zero_removes_stale_json(tmp_path: Path) -> None:
    """SystemExit(0) from build_result exits non-zero and drops stale JSON."""
    output = tmp_path / "reconnect.json"
    output.write_text("ORIGINAL_JSON", encoding="utf-8")
    script = _cli_script(
        "def boom_build(**_kwargs):\n"
        "    raise SystemExit(0)\n"
        "bench.build_result = boom_build\n"
        + _HEALTHY_ATTEMPT
    )
    completed = _run_cli(script, output)
    _assert_cli_failed_without_evidence(completed, output)
    assert "result raised SystemExit(0)" in completed.stderr


def test_qa_030_101_cli_write_result_system_exit_zero_removes_stale_json(tmp_path: Path) -> None:
    """SystemExit(0) from write_result exits non-zero and drops stale JSON."""
    output = tmp_path / "reconnect.json"
    output.write_text("ORIGINAL_JSON", encoding="utf-8")
    script = _cli_script(
        "def boom_write(*_args, **_kwargs):\n"
        "    raise SystemExit(0)\n"
        "bench.write_result = boom_write\n"
        + _HEALTHY_ATTEMPT
    )
    completed = _run_cli(script, output)
    _assert_cli_failed_without_evidence(completed, output)
    assert "publish raised SystemExit(0)" in completed.stderr


def test_qa_030_101_clean_cli_publishes_json_then_failed_run_removes_it(tmp_path: Path) -> None:
    """A later failed run must not leave the previous run's JSON in --output-json."""
    output = tmp_path / "reconnect.json"
    output.write_text("ORIGINAL_JSON", encoding="utf-8")
    healthy = _run_cli(_cli_script(_HEALTHY_ATTEMPT), output)
    assert healthy.returncode == 0
    assert output.exists()
    published = output.read_text(encoding="utf-8")
    assert "phase8-benchmark-v1" in published
    assert not Path(f"{output}.partial").exists()
    assert "ok=4 failed=0" in healthy.stdout

    failed = _run_cli(
        _cli_script(
            "def sample(self):\n"
            "    raise SystemExit(0)\n"
            "bench.SystemSampler.sample = sample\n"
            "async def parked(*_args, **_kwargs):\n"
            "    import asyncio\n"
            "    await asyncio.Event().wait()\n"
            "bench.attempt = parked\n"
        ),
        output,
    )
    _assert_cli_failed_without_evidence(failed, output)


def test_qa_030_101_sampler_init_system_exit_fails_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """SystemExit(0) from SystemSampler() must not return a result or leak tasks."""
    calls = _count_results(monkeypatch)

    class BoomSampler:
        def __init__(self) -> None:
            raise SystemExit(0)

    monkeypatch.setattr(reconnect_bench, "SystemSampler", BoomSampler)

    async def body() -> None:
        before = set(asyncio.all_tasks())
        with pytest.raises(RuntimeError, match="setup raised SystemExit\\(0\\)"):
            await asyncio.wait_for(
                reconnect_bench.run(_args(concurrency=1, attempts=1)),
                timeout=RUN_TIMEOUT_SECONDS,
            )
        assert calls["n"] == 0
        assert _pending_tasks(before) == []

    _run_bounded(body())


def test_qa_030_101_clock_system_exit_fails_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """SystemExit(0) from utc_iso() must not return a result or leak tasks."""
    _patch_sampler(monkeypatch)
    calls = _count_results(monkeypatch)

    def boom_clock() -> str:
        raise SystemExit(0)

    monkeypatch.setattr(reconnect_bench, "utc_iso", boom_clock)

    async def body() -> None:
        before = set(asyncio.all_tasks())
        with pytest.raises(RuntimeError, match="setup raised SystemExit\\(0\\)"):
            await asyncio.wait_for(
                reconnect_bench.run(_args(concurrency=1, attempts=1)),
                timeout=RUN_TIMEOUT_SECONDS,
            )
        assert calls["n"] == 0
        assert _pending_tasks(before) == []

    _run_bounded(body())


def test_qa_030_101_teardown_system_exit_fails_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """SystemExit(0) from queue drain must not publish a result."""
    _patch_sampler(monkeypatch)
    calls = _count_results(monkeypatch)

    async def healthy_attempt(*_args: object, **_kwargs: object) -> tuple[bool, float]:
        return True, 0.0

    def boom_drain(_queue: object) -> None:
        raise SystemExit(0)

    monkeypatch.setattr(reconnect_bench, "attempt", healthy_attempt)
    monkeypatch.setattr(reconnect_bench, "_drain_queue", boom_drain)

    async def body() -> None:
        before = set(asyncio.all_tasks())
        with pytest.raises(RuntimeError, match="teardown raised SystemExit\\(0\\)"):
            await asyncio.wait_for(
                reconnect_bench.run(_args(concurrency=1, attempts=1)),
                timeout=RUN_TIMEOUT_SECONDS,
            )
        assert calls["n"] == 0
        assert _pending_tasks(before) == []

    _run_bounded(body())


def test_qa_030_101_build_result_system_exit_fails_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """SystemExit(0) from build_result must not look like a finished run."""
    _patch_sampler(monkeypatch)

    async def healthy_attempt(*_args: object, **_kwargs: object) -> tuple[bool, float]:
        return True, 0.0

    def boom_build(**_kwargs: object) -> dict:
        raise SystemExit(0)

    monkeypatch.setattr(reconnect_bench, "attempt", healthy_attempt)
    monkeypatch.setattr(reconnect_bench, "build_result", boom_build)

    async def body() -> None:
        before = set(asyncio.all_tasks())
        with pytest.raises(RuntimeError, match="result raised SystemExit\\(0\\)"):
            await asyncio.wait_for(
                reconnect_bench.run(_args(concurrency=1, attempts=1)),
                timeout=RUN_TIMEOUT_SECONDS,
            )
        assert _pending_tasks(before) == []

    _run_bounded(body())


def test_qa_030_101_cli_help_exits_zero_without_touching_evidence(tmp_path: Path) -> None:
    """argparse --help stays a successful exit and does not remove evidence files."""
    output = tmp_path / "reconnect.json"
    output.write_text("ORIGINAL_JSON", encoding="utf-8")
    script = (
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import phase8_reconnect_benchmark as bench\n"
        "sys.argv = ['phase8_reconnect_benchmark', '--help']\n"
        "bench.main()\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(TOOLS)],
        check=False,
        capture_output=True,
        text=True,
        timeout=HARD_TIMEOUT_SECONDS,
    )
    assert completed.returncode == 0
    assert "output-json" in completed.stdout
    assert output.read_text(encoding="utf-8") == "ORIGINAL_JSON"


def test_qa_030_201_lost_replace_keeps_the_other_runs_json(tmp_path: Path) -> None:
    """A failed os.replace must not delete JSON another run already published."""
    output = tmp_path / "reconnect.json"
    csv_path = tmp_path / "reconnect.csv"
    output.write_text("STALE_JSON", encoding="utf-8")
    csv_path.write_text("ORIGINAL_CSV", encoding="utf-8")
    script = _cli_script(
        "def lose_replace(src, dst):\n"
        "    from pathlib import Path\n"
        "    Path(dst).write_text('WINNER_JSON')\n"
        "    raise FileNotFoundError(2, 'No such file or directory', str(src))\n"
        "bench.os.replace = lose_replace\n"
        + _HEALTHY_ATTEMPT,
        csv=True,
    )
    completed = _run_cli(script, output, csv_path)
    assert completed.returncode != 0
    assert output.read_text(encoding="utf-8") == "WINNER_JSON"
    assert csv_path.read_text(encoding="utf-8") == "ORIGINAL_CSV"
    assert "ok=" not in completed.stdout
    assert not Path(f"{output}.partial").exists()


def test_qa_030_202_failed_replace_does_not_append_csv(tmp_path: Path) -> None:
    """CSV grows only after os.replace commits the JSON."""
    output = tmp_path / "reconnect.json"
    csv_path = tmp_path / "reconnect.csv"
    output.write_text("ORIGINAL_JSON", encoding="utf-8")
    csv_path.write_text("ORIGINAL_CSV", encoding="utf-8")
    script = _cli_script(
        "def lose_replace(src, dst):\n"
        "    raise OSError('replace failed')\n"
        "bench.os.replace = lose_replace\n"
        + _HEALTHY_ATTEMPT,
        csv=True,
    )
    completed = _run_cli(script, output, csv_path)
    assert completed.returncode != 0
    assert csv_path.read_text(encoding="utf-8") == "ORIGINAL_CSV"
    assert "ok=" not in completed.stdout


def test_qa_030_202_write_result_system_exit_does_not_append_csv(tmp_path: Path) -> None:
    """SystemExit after write_result returns must not leave a new CSV row."""
    output = tmp_path / "reconnect.json"
    csv_path = tmp_path / "reconnect.csv"
    output.write_text("ORIGINAL_JSON", encoding="utf-8")
    csv_path.write_text("ORIGINAL_CSV", encoding="utf-8")
    script = _cli_script(
        "real_write = bench.write_result\n"
        "def wrapped(*args, **kwargs):\n"
        "    real_write(*args, **kwargs)\n"
        "    raise SystemExit(0)\n"
        "bench.write_result = wrapped\n"
        + _HEALTHY_ATTEMPT,
        csv=True,
    )
    completed = _run_cli(script, output, csv_path)
    assert completed.returncode != 0
    assert csv_path.read_text(encoding="utf-8") == "ORIGINAL_CSV"
    assert "ok=" not in completed.stdout


def test_qa_030_203_partial_directory_does_not_delete_json(tmp_path: Path) -> None:
    """A directory at <name>.partial must fail closed before the JSON is removed."""
    output = tmp_path / "reconnect.json"
    partial = Path(f"{output}.partial")
    output.write_text("ORIGINAL_JSON", encoding="utf-8")
    partial.mkdir()
    completed = _run_cli(_cli_script(_HEALTHY_ATTEMPT), output)
    assert completed.returncode != 0
    assert output.read_text(encoding="utf-8") == "ORIGINAL_JSON"
    assert partial.is_dir()
    assert "directory" in completed.stderr
    assert "ok=" not in completed.stdout


def test_qa_030_203_partial_symlink_is_left_in_place(tmp_path: Path) -> None:
    """A symlink at <name>.partial stays, and its target is not rewritten."""
    output = tmp_path / "reconnect.json"
    target = tmp_path / "target.txt"
    partial = Path(f"{output}.partial")
    target.write_text("SYMLINK_TARGET", encoding="utf-8")
    partial.symlink_to(target)
    output.write_text("ORIGINAL_JSON", encoding="utf-8")
    completed = _run_cli(_cli_script(_HEALTHY_ATTEMPT), output)
    assert completed.returncode == 0
    assert partial.is_symlink()
    assert target.read_text(encoding="utf-8") == "SYMLINK_TARGET"
    assert "phase8-benchmark-v1" in output.read_text(encoding="utf-8")
    assert "ok=4 failed=0" in completed.stdout
