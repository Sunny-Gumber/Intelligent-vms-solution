"""Storage benchmark byte accounting for VMS-FIX-031 (finding F33).

Fake raw handles replace disk files. Each call is bounded by a daemon-thread
join so a zero-write spin fails the test instead of hanging the suite. The
tests do not use the network and do not benchmark a real volume.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

TOOLS = Path(__file__).parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import phase8_benchmark_common as common
import phase8_storage_benchmark as storage_bench

MIB = 1024 * 1024
STREAM_BYTES = 32
CHUNK_BYTES = 16
ONE_SHORT_ACCEPT = 7
MANY_SHORT_ACCEPT = 1
LOOP_TIMEOUT_SECONDS = 2.0
RUN_TIMEOUT_SECONDS = 20.0
STALL_ERROR = "StorageBenchmarkWriteError"
TOP_LEVEL_KEYS = {
    "schema_version",
    "benchmark_id",
    "environment",
    "workload",
    "result",
    "resources",
}
WORKLOAD_KEYS = {
    "type",
    "config",
    "started_at",
    "warmup_seconds",
    "duration_seconds",
}
RESULT_KEYS = {
    "operations_ok",
    "operations_failed",
    "failure_rate",
    "throughput_ops_s",
    "latency",
    "bytes_written",
    "aggregate_write_mbps",
    "aggregate_write_MBps",
}
LATENCY_KEYS = {"count", "p50_ms", "p95_ms", "p99_ms", "max_ms", "mean_ms"}
CONFIG_KEYS = {
    "path",
    "streams",
    "mib_per_stream",
    "chunk_mib",
    "fsync_each_chunk",
}


class RecordingHandle:
    """Fake raw binary handle that records only the bytes ``write`` accepts."""

    def __init__(self, script=(), default="all"):
        """Store scripted write results for one opened stream.

        Args:
            script: Outcomes consumed in order. An int is a byte count, None is
                a no-progress raw return, and an exception instance is raised.
            default: Outcome used after ``script`` is exhausted. ``"all"``
                accepts the entire submitted buffer.

        Returns:
            None.

        Raises:
            None.
        """
        self.script = list(script)
        self.default = default
        self.received = bytearray()
        self.calls = 0
        self.submitted_lengths = []
        self.closed = False

    def write(self, payload):
        """Accept a scripted prefix of ``payload`` and record that prefix.

        Args:
            payload: Bytes the benchmark submitted for this raw write.

        Returns:
            The scripted count. ``"all"`` returns ``len(payload)``. A non-positive
            count or None is returned without recording bytes.

        Raises:
            Exception: The scripted exception instance, when that is the next outcome.
        """
        self.calls += 1
        self.submitted_lengths.append(len(payload))
        action = self.script.pop(0) if self.script else self.default
        if isinstance(action, BaseException):
            raise action
        if action is None:
            return None
        count = len(payload) if action == "all" else action
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            return count
        count = min(count, len(payload))
        self.received.extend(payload[:count])
        return count

    def fileno(self):
        """Return a dummy descriptor so a patched ``os.fsync`` can be counted.

        Args:
            None.

        Returns:
            Zero. Tests never pass this descriptor to the real ``os.fsync``.

        Raises:
            None.
        """
        return 0

    def __enter__(self):
        """Enter the context manager the benchmark uses for the stream file.

        Args:
            None.

        Returns:
            This handle.

        Raises:
            None.
        """
        return self

    def __exit__(self, exc_type, exc, tb):
        """Mark the handle closed without hiding a write failure.

        Args:
            exc_type: Exception type being propagated, if any.
            exc: Exception instance being propagated, if any.
            tb: Traceback being propagated, if any.

        Returns:
            False, so a write error still fails the benchmark run.

        Raises:
            None.
        """
        self.closed = True
        return False


def shrinking_submissions(total_bytes, chunk_bytes, accepted_each):
    """Build the payload sizes a correct short-write loop submits.

    Args:
        total_bytes: Bytes that must be accepted.
        chunk_bytes: Maximum submitted chunk size.
        accepted_each: Positive byte count returned by every raw write.

    Returns:
        Submitted lengths. Within a chunk the length shrinks by ``accepted_each``.

    Raises:
        ValueError: If ``accepted_each`` is less than 1.
    """
    if accepted_each < 1:
        raise ValueError("accepted_each must be positive")
    lengths = []
    remaining = total_bytes
    while remaining > 0:
        chunk_remaining = min(chunk_bytes, remaining)
        while chunk_remaining > 0:
            lengths.append(chunk_remaining)
            step = min(accepted_each, chunk_remaining)
            chunk_remaining -= step
            remaining -= step
    return lengths


def benchmark_args(path, *, streams, total_bytes, chunk_bytes, fsync=False):
    """Build ``run`` arguments that resolve to exact byte counts.

    Args:
        path: Directory passed as the benchmark target.
        streams: Concurrent stream count.
        total_bytes: Requested bytes per stream.
        chunk_bytes: Requested chunk size in bytes.
        fsync: Whether each completed chunk is fsynced.

    Returns:
        SimpleNamespace compatible with ``phase8_storage_benchmark.run``.

    Raises:
        ValueError: If a byte count is not an exact MiB float for this tool.
    """
    mib_per_stream = total_bytes / MIB
    chunk_mib = chunk_bytes / MIB
    if int(mib_per_stream * MIB) != total_bytes:
        raise ValueError(f"total_bytes {total_bytes} is not an exact MiB quantity")
    if int(chunk_mib * MIB) != chunk_bytes:
        raise ValueError(f"chunk_bytes {chunk_bytes} is not an exact MiB quantity")
    return SimpleNamespace(
        path=str(path),
        streams=streams,
        mib_per_stream=mib_per_stream,
        chunk_mib=chunk_mib,
        fsync=fsync,
        keep_files=False,
        sample_interval=30.0,
    )


def install_handles(monkeypatch, factory):
    """Replace unbuffered ``wb`` opens with recording handles.

    Environment probes and other reads keep the real ``Path.open``.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        factory: Callable returning a new ``RecordingHandle`` for each stream.

    Returns:
        Handles created so far, in open order.

    Raises:
        OSError: If a delegated real open fails.
    """
    handles = []
    gate = threading.Lock()
    original_open = Path.open

    def open_raw(self, mode="r", buffering=-1, *args, **kwargs):
        if mode == "wb" and buffering == 0:
            handle = factory()
            with gate:
                handles.append(handle)
            return handle
        return original_open(self, mode, buffering, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_raw)
    return handles


async def inline_worker(func, /, *func_args, **func_kwargs):
    """Run one storage worker on the calling thread.

    ``asyncio.to_thread`` would leave a non-daemon pool thread behind if a
    zero write spun. Running inline keeps that spin inside the timed caller.

    Args:
        func: Worker normally passed to ``asyncio.to_thread``.
        *func_args: Positional worker arguments.
        **func_kwargs: Keyword worker arguments.

    Returns:
        The worker's return value.

    Raises:
        Exception: Any exception raised by the worker.
    """
    return func(*func_args, **func_kwargs)


@pytest.fixture
def inline_workers(monkeypatch):
    """Force benchmark workers onto the caller for the duration of one test.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None. The fixture is used for its side effect.

    Raises:
        None.
    """
    monkeypatch.setattr(storage_bench.asyncio, "to_thread", inline_worker)


def call_bounded(operation, timeout_seconds=LOOP_TIMEOUT_SECONDS):
    """Run ``operation`` and fail if it exceeds ``timeout_seconds``.

    The worker is a daemon thread. A regression that spins fails this assertion
    and does not block process exit.

    Args:
        operation: Zero-argument callable under test.
        timeout_seconds: Real-time join limit.

    Returns:
        The value returned by ``operation``.

    Raises:
        AssertionError: If ``operation`` is still running when the join expires.
        Exception: Any exception raised by ``operation``.
    """
    outcome = {}

    def target():
        try:
            outcome["value"] = operation()
        except Exception as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=target, name="vms-fix-031-timeout", daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive():
        raise AssertionError(f"storage benchmark exceeded the {timeout_seconds:.1f}s hard timeout")
    worker.join()
    if "error" in outcome:
        error = outcome["error"]
        raise error.with_traceback(error.__traceback__)
    return outcome["value"]


def failure_detail(value):
    """Describe a benchmark call that returned instead of failing.

    Args:
        value: Return value from the stream writer or from ``run``.

    Returns:
        A short assertion message including any reported byte count.

    Raises:
        None.
    """
    if isinstance(value, tuple) and value:
        return f"writer returned written={value[0]!r} latencies={len(value[1])}"
    if isinstance(value, dict):
        reported = value.get("result", {}).get("bytes_written")
        return f"benchmark result reported bytes_written={reported!r}"
    return f"call returned {value!r}"


def assert_stall(operation):
    """Assert ``operation`` fails on a zero-progress write and does not succeed.

    Args:
        operation: Zero-argument callable that performs the write or the run.

    Returns:
        The raised stall exception.

    Raises:
        AssertionError: If the call hangs, returns a result, or raises a
            different error.
    """
    try:
        value = call_bounded(operation)
    except AssertionError:
        raise
    except Exception as exc:
        assert type(exc).__name__ == STALL_ERROR, type(exc).__name__ + ": " + str(exc)
        return exc
    raise AssertionError(failure_detail(value))


def test_one_short_write_counts_only_accepted_bytes(monkeypatch, tmp_path):
    """One short raw write must be finished from the unaccepted tail.

    Args:
        monkeypatch: Pytest monkeypatch fixture used to install the fake opener.
        tmp_path: Temporary directory. The benchmark does not write this path.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If the call hangs, resubmits accepted bytes, or reports
            a count other than the bytes the fake accepted.
    """
    fsync_calls = []
    monkeypatch.setattr(storage_bench.os, "fsync", lambda fd: fsync_calls.append(fd))
    handles = install_handles(
        monkeypatch,
        lambda: RecordingHandle([ONE_SHORT_ACCEPT], default="all"),
    )
    path = tmp_path / "one-short.bin"
    written, latencies = call_bounded(
        lambda: storage_bench._write_stream(path, STREAM_BYTES, STREAM_BYTES, False)
    )
    handle = handles[0]
    assert written == STREAM_BYTES
    assert written == len(handle.received)
    assert handle.submitted_lengths == [STREAM_BYTES, STREAM_BYTES - ONE_SHORT_ACCEPT]
    assert len(latencies) == 1
    assert handle.closed is True
    assert fsync_calls == []


def test_many_short_writes_count_only_accepted_bytes(monkeypatch, tmp_path):
    """Many one-byte raw writes must add up to the requested stream size.

    Args:
        monkeypatch: Pytest monkeypatch fixture used to install the fake opener.
        tmp_path: Temporary directory. The benchmark does not write this path.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If the call hangs or the reported count disagrees with
            the fake handle.
    """
    handles = install_handles(
        monkeypatch,
        lambda: RecordingHandle((), default=MANY_SHORT_ACCEPT),
    )
    path = tmp_path / "many-short.bin"
    written, latencies = call_bounded(
        lambda: storage_bench._write_stream(path, STREAM_BYTES, CHUNK_BYTES, False)
    )
    handle = handles[0]
    assert written == STREAM_BYTES
    assert written == len(handle.received)
    assert handle.submitted_lengths == shrinking_submissions(
        STREAM_BYTES, CHUNK_BYTES, MANY_SHORT_ACCEPT
    )
    assert len(latencies) == STREAM_BYTES // CHUNK_BYTES
    assert handle.calls == STREAM_BYTES // MANY_SHORT_ACCEPT


@pytest.mark.parametrize("stall", [0, None], ids=["zero-count", "none-return"])
def test_zero_write_fails_without_spinning(monkeypatch, tmp_path, stall):
    """A write that accepts nothing fails on the first return and cannot spin.

    Args:
        monkeypatch: Pytest monkeypatch fixture used to install the fake opener.
        tmp_path: Temporary directory. The benchmark does not write this path.
        stall: Raw return that means no progress. ``0`` and ``None`` both stall.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If the call hangs, succeeds, or retries the zero write.
    """
    handles = install_handles(monkeypatch, lambda: RecordingHandle((), default=stall))
    path = tmp_path / "zero.bin"
    assert_stall(lambda: storage_bench._write_stream(path, STREAM_BYTES, STREAM_BYTES, False))
    handle = handles[0]
    assert handle.calls == 1
    assert handle.received == b""
    assert handle.closed is True


def test_oserror_on_write_fails_the_stream(monkeypatch, tmp_path):
    """An ``OSError`` from ``write`` fails the stream instead of counting bytes.

    Args:
        monkeypatch: Pytest monkeypatch fixture used to install the fake opener.
        tmp_path: Temporary directory. The benchmark does not write this path.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If the call hangs or returns a byte count.
        OSError: Expected from the fake handle and asserted by pytest.
    """
    handles = install_handles(
        monkeypatch,
        lambda: RecordingHandle([OSError("disk full")], default="all"),
    )
    path = tmp_path / "oserror.bin"
    with pytest.raises(OSError, match="disk full"):
        call_bounded(lambda: storage_bench._write_stream(path, STREAM_BYTES, STREAM_BYTES, False))
    assert handles[0].received == b""
    assert handles[0].closed is True


def test_clean_run_keeps_result_shape(monkeypatch, tmp_path, inline_workers):
    """A full-write run keeps the phase-8 result shape and the requested count.

    Args:
        monkeypatch: Pytest monkeypatch fixture used to install the fake opener.
        tmp_path: Temporary directory passed as the benchmark path.
        inline_workers: Fixture that keeps workers on the timed caller.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If the call hangs or the result shape or byte count changes.
    """
    del inline_workers
    streams = 2
    handles = install_handles(monkeypatch, lambda: RecordingHandle((), default="all"))
    args = benchmark_args(
        tmp_path,
        streams=streams,
        total_bytes=STREAM_BYTES,
        chunk_bytes=CHUNK_BYTES,
        fsync=False,
    )
    result = call_bounded(lambda: asyncio.run(storage_bench.run(args)), RUN_TIMEOUT_SECONDS)
    common.validate_result(result)
    assert set(result) == TOP_LEVEL_KEYS
    assert set(result["workload"]) == WORKLOAD_KEYS
    assert set(result["workload"]["config"]) == CONFIG_KEYS
    assert set(result["result"]) == RESULT_KEYS
    assert set(result["result"]["latency"]) == LATENCY_KEYS
    assert result["schema_version"] == "phase8-benchmark-v1"
    assert result["workload"]["type"] == "synthetic-storage-write"
    assert result["workload"]["config"]["fsync_each_chunk"] is False
    assert result["result"]["operations_failed"] == 0
    assert result["result"]["failure_rate"] == 0
    expected = streams * STREAM_BYTES
    assert result["result"]["bytes_written"] == expected
    assert result["result"]["bytes_written"] == sum(len(handle.received) for handle in handles)
    assert result["result"]["operations_ok"] == streams * (STREAM_BYTES // CHUNK_BYTES)
    assert len(handles) == streams


def test_reported_bytes_match_bytes_received_by_the_fake(monkeypatch, tmp_path, inline_workers):
    """The published byte count equals the bytes the fake handles accepted.

    Args:
        monkeypatch: Pytest monkeypatch fixture used to install the fake opener.
        tmp_path: Temporary directory passed as the benchmark path.
        inline_workers: Fixture that keeps workers on the timed caller.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If the call hangs or ``bytes_written`` disagrees with the fakes.
    """
    del inline_workers
    streams = 2
    handles = install_handles(
        monkeypatch,
        lambda: RecordingHandle((), default=MANY_SHORT_ACCEPT),
    )
    args = benchmark_args(
        tmp_path,
        streams=streams,
        total_bytes=STREAM_BYTES,
        chunk_bytes=CHUNK_BYTES,
        fsync=False,
    )
    result = call_bounded(lambda: asyncio.run(storage_bench.run(args)), LOOP_TIMEOUT_SECONDS)
    accepted = sum(len(handle.received) for handle in handles)
    assert result["result"]["bytes_written"] == accepted
    assert accepted == streams * STREAM_BYTES
    assert result["result"]["operations_failed"] == 0


@pytest.mark.parametrize("stall", [0, None], ids=["zero-count", "none-return"])
def test_zero_write_is_not_a_successful_benchmark(monkeypatch, tmp_path, inline_workers, stall):
    """A zero-progress write must not publish a benchmark result.

    Args:
        monkeypatch: Pytest monkeypatch fixture used to install the fake opener.
        tmp_path: Temporary directory passed as the benchmark path.
        inline_workers: Fixture that keeps workers on the timed caller.
        stall: Raw return that means no progress.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If the call hangs or ``run`` returns a result.
    """
    del inline_workers
    handles = install_handles(monkeypatch, lambda: RecordingHandle((), default=stall))
    args = benchmark_args(
        tmp_path,
        streams=1,
        total_bytes=STREAM_BYTES,
        chunk_bytes=STREAM_BYTES,
        fsync=False,
    )
    assert_stall(lambda: asyncio.run(storage_bench.run(args)))
    assert handles[0].calls == 1
    assert handles[0].received == b""


def test_oserror_is_not_a_successful_benchmark(monkeypatch, tmp_path, inline_workers):
    """A ``write`` ``OSError`` must not publish a benchmark result.

    Args:
        monkeypatch: Pytest monkeypatch fixture used to install the fake opener.
        tmp_path: Temporary directory passed as the benchmark path.
        inline_workers: Fixture that keeps workers on the timed caller.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If the call hangs or ``run`` returns a result.
        OSError: Expected from the fake handle and asserted by pytest.
    """
    del inline_workers
    handles = install_handles(
        monkeypatch,
        lambda: RecordingHandle([OSError("disk full")], default="all"),
    )
    args = benchmark_args(
        tmp_path,
        streams=1,
        total_bytes=STREAM_BYTES,
        chunk_bytes=STREAM_BYTES,
        fsync=False,
    )
    with pytest.raises(OSError, match="disk full"):
        call_bounded(lambda: asyncio.run(storage_bench.run(args)))
    assert handles[0].received == b""


def test_fsync_stays_once_per_completed_chunk(monkeypatch, tmp_path):
    """``fsync`` still runs once per completed chunk, after the bytes are accepted.

    Args:
        monkeypatch: Pytest monkeypatch fixture used to install the fake opener.
        tmp_path: Temporary directory. The benchmark does not write this path.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If fsync follows every short write, or the byte count is wrong.
    """
    fsync_calls = []
    monkeypatch.setattr(storage_bench.os, "fsync", lambda fd: fsync_calls.append(fd))
    handles = install_handles(
        monkeypatch,
        lambda: RecordingHandle((), default=MANY_SHORT_ACCEPT),
    )
    path = tmp_path / "fsync.bin"
    written, latencies = call_bounded(
        lambda: storage_bench._write_stream(path, STREAM_BYTES, CHUNK_BYTES, True)
    )
    chunks = STREAM_BYTES // CHUNK_BYTES
    assert written == len(handles[0].received) == STREAM_BYTES
    assert len(latencies) == chunks
    assert fsync_calls == [0] * chunks
    assert handles[0].calls == STREAM_BYTES // MANY_SHORT_ACCEPT


def test_partial_stream_is_not_a_successful_benchmark(monkeypatch, tmp_path, inline_workers):
    """A stream that returns fewer bytes than requested is not a successful run.

    Args:
        monkeypatch: Pytest monkeypatch fixture used to replace the stream writer.
        tmp_path: Temporary directory passed as the benchmark path.
        inline_workers: Fixture that keeps workers on the timed caller.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If ``run`` publishes a result for a short stream.
    """
    del inline_workers

    def short_stream(path, total_bytes, chunk_bytes, fsync):
        del path, chunk_bytes, fsync
        return (total_bytes - 1, [0.01])

    monkeypatch.setattr(storage_bench, "_write_stream", short_stream)
    args = benchmark_args(
        tmp_path,
        streams=1,
        total_bytes=STREAM_BYTES,
        chunk_bytes=CHUNK_BYTES,
        fsync=False,
    )
    error = assert_stall(lambda: asyncio.run(storage_bench.run(args)))
    assert "accepted" in str(error)
