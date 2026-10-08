"""Storage benchmark byte accounting for VMS-FIX-031 (finding F33).

Most cases use fake raw handles. The sibling-cleanup and real-file cases use
a temporary directory and do not patch ``Path.open``. Each call is bounded by
a daemon-thread join so a zero-write spin fails the test instead of hanging.
The tests do not use the network and do not run a storage benchmark volume.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
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
PARTIAL_ACCEPT_BYTES = 7
SIBLING_OPEN_DELAY_SECONDS = 0.05
NONUNIFORM_PAYLOAD = bytes((index * 13 + 5) % 251 for index in range(64 * PARTIAL_ACCEPT_BYTES + 3))
LOOP_TIMEOUT_SECONDS = 2.0
RUN_TIMEOUT_SECONDS = 20.0
CANCEL_WRITE_DELAY_SECONDS = 0.02
CANCEL_TEST_TIMEOUT_SECONDS = 8.0
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
    """Replace exclusive stream opens with recording handles.

    The benchmark creates stream files through ``_open_stream_file``. This
    helper replaces that function. Environment probes keep their real opens.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        factory: Callable returning a new ``RecordingHandle`` for each stream.

    Returns:
        Handles created so far, in open order.

    Raises:
        None.
    """
    handles = []
    gate = threading.Lock()
    if hasattr(storage_bench, "_open_stream_file"):

        def open_raw(path):
            handle = factory()
            with gate:
                handles.append(handle)
            storage_bench._remember_created_stream(path)
            return handle

        monkeypatch.setattr(storage_bench, "_open_stream_file", open_raw)
        return handles

    original_open = Path.open

    def open_legacy(self, mode="r", buffering=-1, *args, **kwargs):
        if mode == "wb" and buffering == 0:
            handle = factory()
            with gate:
                handles.append(handle)
            return handle
        return original_open(self, mode, buffering, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_legacy)
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


class PartialDiskFile:
    """Real raw file that accepts at most a fixed prefix of each write."""

    def __init__(self, path, limit):
        """Open ``path`` unbuffered and remember the per-write cap.

        Args:
            path: Destination file created for this test.
            limit: Maximum bytes accepted from each submitted buffer.

        Returns:
            None.

        Raises:
            OSError: If the file cannot be opened.
        """
        self.limit = limit
        self.submitted = []
        self._raw = path.open("wb", buffering=0)
        self.closed = False

    def write(self, payload):
        """Accept at most ``limit`` bytes and write that prefix to disk.

        Args:
            payload: Bytes the benchmark submitted.

        Returns:
            The count the real raw write accepted, never more than ``limit``.

        Raises:
            OSError: If the underlying write fails.
            ValueError: If this handle is already closed.
        """
        if self.closed:
            raise ValueError("write on a closed partial disk file")
        data = bytes(payload)
        self.submitted.append(data)
        count = min(self.limit, len(data))
        return self._raw.write(data[:count])

    def fileno(self):
        """Return the real descriptor for the destination file.

        Args:
            None.

        Returns:
            The operating-system descriptor.

        Raises:
            ValueError: If this handle is already closed.
        """
        if self.closed:
            raise ValueError("fileno on a closed partial disk file")
        return self._raw.fileno()

    def __enter__(self):
        """Enter the context manager.

        Args:
            None.

        Returns:
            This handle.

        Raises:
            None.
        """
        return self

    def __exit__(self, exc_type, exc, tb):
        """Close the real file without hiding a write error.

        Args:
            exc_type: Exception type being propagated, if any.
            exc: Exception instance being propagated, if any.
            tb: Traceback being propagated, if any.

        Returns:
            False so a write failure still fails the test.

        Raises:
            OSError: If closing the real file fails.
        """
        self.closed = True
        self._raw.close()
        return False


def test_partial_raw_writes_keep_nonuniform_payload_bytes(tmp_path):
    """A 7-byte raw writer must land the exact non-uniform payload on disk.

    Args:
        tmp_path: Temporary directory that receives the real file.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If a resubmitted prefix changes the bytes on disk or
            the next submission is not the unaccepted tail.
    """
    path = tmp_path / "pattern.bin"
    handle = PartialDiskFile(path, PARTIAL_ACCEPT_BYTES)
    with handle:
        accepted = call_bounded(lambda: storage_bench._accept_payload(handle, NONUNIFORM_PAYLOAD))
    assert accepted == len(NONUNIFORM_PAYLOAD)
    assert path.read_bytes() == NONUNIFORM_PAYLOAD
    offset = 0
    for submitted in handle.submitted:
        assert submitted == NONUNIFORM_PAYLOAD[offset:]
        offset += min(PARTIAL_ACCEPT_BYTES, len(submitted))
    assert offset == len(NONUNIFORM_PAYLOAD)
    assert handle.closed is True


def test_real_file_bytes_match_the_requested_stream(tmp_path):
    """A real unbuffered stream file matches the bytes the writer reports.

    Args:
        tmp_path: Temporary directory that receives the stream file.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If the file size or contents disagree with the report.
    """
    path = tmp_path / "real-stream.bin"
    total_bytes = 128
    chunk_bytes = 32
    written, latencies = call_bounded(
        lambda: storage_bench._write_stream(path, total_bytes, chunk_bytes, False)
    )
    assert written == total_bytes
    assert path.read_bytes() == b"\0" * total_bytes
    assert len(latencies) == total_bytes // chunk_bytes
    assert path.stat().st_size == written


def test_failed_multi_stream_run_removes_the_sibling_file(monkeypatch, tmp_path):
    """QA-031-001: a late sibling must not remain after another stream fails.

    Stream 0 raises ``OSError`` errno 5. Stream 1 sleeps 50 ms before opening a
    real file. ``Path.open`` is not patched. ``keep_files`` is false.

    Args:
        monkeypatch: Pytest monkeypatch fixture used to replace the stream worker.
        tmp_path: Real temporary directory for the stream files.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If the call hangs, publishes a result, or leaves a
            ``phase8-storage-*.bin`` file behind.
        OSError: Expected errno 5 from stream 0, asserted by pytest.
    """
    build_calls = []

    def forbid_result(*args, **kwargs):
        del args, kwargs
        build_calls.append("build_result")
        raise AssertionError("build_result must not run after a stream failure")

    monkeypatch.setattr(storage_bench, "build_result", forbid_result)
    original = storage_bench._write_stream

    def wrapped(path, total_bytes, chunk_bytes, fsync, *extra):
        if path.name.endswith("-0000.bin"):
            with path.open("wb", buffering=0) as handle:
                del handle
            remember = getattr(storage_bench, "_remember_created_stream", None)
            if remember is not None:
                remember(path)
            raise OSError(errno.EIO, "Input/output error")
        if path.name.endswith("-0001.bin"):
            time.sleep(SIBLING_OPEN_DELAY_SECONDS)
        return original(path, total_bytes, chunk_bytes, fsync, *extra)

    monkeypatch.setattr(storage_bench, "_write_stream", wrapped)
    args = benchmark_args(
        tmp_path,
        streams=2,
        total_bytes=STREAM_BYTES,
        chunk_bytes=STREAM_BYTES,
        fsync=False,
    )
    with pytest.raises(OSError) as caught:
        call_bounded(lambda: asyncio.run(storage_bench.run(args)))
    assert caught.value.errno == errno.EIO
    assert build_calls == []
    assert list(tmp_path.glob("phase8-storage-*.bin")) == []


def test_unlink_failure_is_logged_and_other_files_are_removed(monkeypatch, tmp_path, caplog):
    """An unlink error is logged and does not skip the remaining stream files.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Real temporary directory for the stream files.
        caplog: Pytest log capture fixture.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If cleanup stops early, the warning is missing, or a
            result is published.
        OSError: Expected errno 5 from the failing stream, asserted by pytest.
    """
    gate = threading.Barrier(2)
    original_unlink = Path.unlink

    def unlink(self, missing_ok=False):
        if self.name.endswith("-0000.bin"):
            raise OSError(errno.EACCES, "file is open")
        return original_unlink(self, missing_ok=missing_ok)

    def wrapped(path, total_bytes, chunk_bytes, fsync):
        del chunk_bytes, fsync
        path.write_bytes(b"x" * 4)
        remember = getattr(storage_bench, "_remember_created_stream", None)
        if remember is not None:
            remember(path)
        gate.wait(timeout=LOOP_TIMEOUT_SECONDS)
        if path.name.endswith("-0000.bin"):
            raise OSError(errno.EIO, "Input/output error")
        return (total_bytes, [0.01])

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(storage_bench, "_write_stream", wrapped)
    args = benchmark_args(
        tmp_path,
        streams=2,
        total_bytes=STREAM_BYTES,
        chunk_bytes=STREAM_BYTES,
        fsync=False,
    )
    with caplog.at_level(logging.WARNING, logger="phase8_storage_benchmark"):
        with pytest.raises(OSError) as caught:
            call_bounded(lambda: asyncio.run(storage_bench.run(args)))
    assert caught.value.errno == errno.EIO
    first = list(tmp_path.glob("phase8-storage-*-0000.bin"))
    second = list(tmp_path.glob("phase8-storage-*-0001.bin"))
    assert len(first) == 1
    assert second == []
    assert "failed to remove storage benchmark file" in caplog.text
    assert first[0].name in caplog.text


class SlowRawFile:
    """Real raw file that accepts one byte and then waits."""

    def __init__(self, path, delay_seconds):
        """Open ``path`` and remember how long each write waits.

        Args:
            path: Destination file created for this test.
            delay_seconds: Sleep before the single accepted byte is written.

        Returns:
            None.

        Raises:
            OSError: If the file cannot be opened.
        """
        self.delay_seconds = delay_seconds
        self._raw = Path.open(path, "wb", buffering=0)
        self.closed = False

    def write(self, payload):
        """Accept one byte after ``delay_seconds``.

        Args:
            payload: Bytes the benchmark submitted.

        Returns:
            The count the real raw write accepted, or 0 when ``payload`` is empty.

        Raises:
            OSError: If the underlying write fails.
            ValueError: If this handle is already closed.
        """
        if self.closed:
            raise ValueError("write on a closed slow raw file")
        time.sleep(self.delay_seconds)
        data = bytes(payload[:1])
        if not data:
            return 0
        return self._raw.write(data)

    def fileno(self):
        """Return the real descriptor for the destination file.

        Args:
            None.

        Returns:
            The operating-system descriptor.

        Raises:
            ValueError: If this handle is already closed.
        """
        if self.closed:
            raise ValueError("fileno on a closed slow raw file")
        return self._raw.fileno()

    def __enter__(self):
        """Enter the context manager.

        Args:
            None.

        Returns:
            This handle.

        Raises:
            None.
        """
        return self

    def __exit__(self, exc_type, exc, tb):
        """Close the real file without hiding a write error.

        Args:
            exc_type: Exception type being propagated, if any.
            exc: Exception instance being propagated, if any.
            tb: Traceback being propagated, if any.

        Returns:
            False so a write failure still fails the caller.

        Raises:
            OSError: If closing the real file fails.
        """
        self.closed = True
        self._raw.close()
        return False


def install_slow_raw_files(monkeypatch, delay_seconds):
    """Replace exclusive stream opens with one-byte slow files.

    The real file is still created by ``_open_stream_file``, so a planted
    symlink is refused before the slow wrapper runs.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        delay_seconds: Sleep before each accepted byte.

    Returns:
        Slow files created so far, in open order.

    Raises:
        OSError: If the exclusive create fails.
        StorageBenchmarkWriteError: If the stream path already exists.
    """
    handles = []
    gate = threading.Lock()
    if hasattr(storage_bench, "_open_stream_file"):
        original_open = storage_bench._open_stream_file

        def open_raw(path):
            raw = original_open(path)
            handle = SlowRawFile.__new__(SlowRawFile)
            handle.delay_seconds = delay_seconds
            handle.closed = False
            handle._raw = raw
            with gate:
                handles.append(handle)
            return handle

        monkeypatch.setattr(storage_bench, "_open_stream_file", open_raw)
        return handles

    original_open = Path.open

    def open_legacy(self, mode="r", buffering=-1, *args, **kwargs):
        if mode == "wb" and buffering == 0:
            handle = SlowRawFile.__new__(SlowRawFile)
            handle.delay_seconds = delay_seconds
            handle.closed = False
            handle._raw = original_open(self, mode, buffering, *args, **kwargs)
            with gate:
                handles.append(handle)
            return handle
        return original_open(self, mode, buffering, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_legacy)
    return handles


def threads_inside_storage_write():
    """Return names of live threads currently inside the storage writer.

    Args:
        None.

    Returns:
        Thread names whose stack contains ``_write_stream`` or ``_accept_payload``.

    Raises:
        None.
    """
    interesting = {"_write_stream", "_accept_payload"}
    frames = sys._current_frames()
    names = []
    for thread in threading.enumerate():
        if not thread.is_alive():
            continue
        frame = frames.get(thread.ident)
        while frame is not None:
            if frame.f_code.co_name in interesting:
                names.append(thread.name)
                break
            frame = frame.f_back
    return names


def test_outer_cancel_stops_workers_and_removes_files(monkeypatch, tmp_path):
    """Cancelling ``run()`` must stop the writers and delete every stream file.

    Args:
        monkeypatch: Pytest monkeypatch fixture used to install slow raw files.
        tmp_path: Real temporary directory for the stream files.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If the call hangs, leaves a stream file, leaves a writer
            thread inside ``_write_stream``, or does not surface ``CancelledError``.
    """
    install_slow_raw_files(monkeypatch, CANCEL_WRITE_DELAY_SECONDS)
    args = benchmark_args(
        tmp_path,
        streams=2,
        total_bytes=STREAM_BYTES,
        chunk_bytes=STREAM_BYTES,
        fsync=False,
    )

    async def cancel_after_first_bytes():
        task = asyncio.create_task(storage_bench.run(args))
        deadline = time.monotonic() + CANCEL_TEST_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            files = list(tmp_path.glob("phase8-storage-*.bin"))
            if len(files) == 2 and min(path.stat().st_size for path in files) >= 1:
                break
            await asyncio.sleep(0.01)
        else:
            task.cancel()
            raise AssertionError("stream files did not start before the cancel")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        alive = threads_inside_storage_write()
        leftover = list(tmp_path.glob("phase8-storage-*.bin"))
        assert alive == []
        assert leftover == []

    call_bounded(lambda: asyncio.run(cancel_after_first_bytes()), CANCEL_TEST_TIMEOUT_SECONDS)


def test_keyboard_interrupt_during_asyncio_run_removes_files(tmp_path):
    """SIGINT during ``asyncio.run(run())`` must not leave stream files behind.

    The benchmark runs in a child process so the signal does not hit pytest.

    Args:
        tmp_path: Real temporary directory for the child and its stream files.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If the child hangs, exits successfully, rewrites the
            sentinel JSON, or leaves a ``phase8-storage-*.bin`` file.
    """
    sentinel = tmp_path / "result.json"
    sentinel.write_text('{"sentinel":"keep"}\n', encoding="utf-8")
    script = tmp_path / "cancel_child.py"
    script.write_text(
        "\n".join(
            [
                "import asyncio",
                "import sys",
                "import time",
                "from pathlib import Path",
                "sys.path.insert(0, sys.argv[1])",
                "import phase8_storage_benchmark as bench",
                "work = Path(sys.argv[2])",
                "delay = float(sys.argv[3])",
                "def attach_slow(raw):",
                "    class Slow:",
                "        def write(self, payload):",
                "            time.sleep(delay)",
                "            data = bytes(payload[:1])",
                "            if not data:",
                "                return 0",
                "            return raw.write(data)",
                "        def fileno(self):",
                "            return raw.fileno()",
                "        def __enter__(self):",
                "            return self",
                "        def __exit__(self, exc_type, exc, tb):",
                "            raw.close()",
                "            return False",
                "    return Slow()",
                "if hasattr(bench, '_open_stream_file'):",
                "    original_open = bench._open_stream_file",
                "    def open_raw(path):",
                "        return attach_slow(original_open(path))",
                "    bench._open_stream_file = open_raw",
                "else:",
                "    original_open = Path.open",
                "    def open_raw(self, mode='r', buffering=-1, *args, **kwargs):",
                "        if mode == 'wb' and buffering == 0:",
                "            return attach_slow(original_open(self, mode, buffering, *args, **kwargs))",
                "        return original_open(self, mode, buffering, *args, **kwargs)",
                "    Path.open = open_raw",
                "total = 32",
                "mib = total / (1024 * 1024)",
                "args = type('A', (), {})()",
                "args.path = str(work)",
                "args.streams = 2",
                "args.mib_per_stream = mib",
                "args.chunk_mib = mib",
                "args.fsync = False",
                "args.keep_files = False",
                "args.sample_interval = 30.0",
                "try:",
                "    asyncio.run(bench.run(args))",
                "    print('CHILD_OK', flush=True)",
                "except KeyboardInterrupt:",
                "    print('CHILD_EXC KeyboardInterrupt', flush=True)",
                "    raise SystemExit(130)",
                "except BaseException as exc:",
                "    print('CHILD_EXC ' + type(exc).__name__, flush=True)",
                "    raise",
                "",
            ]
        ),
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(script), str(TOOLS), str(tmp_path), str(CANCEL_WRITE_DELAY_SECONDS)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + CANCEL_TEST_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            files = list(tmp_path.glob("phase8-storage-*.bin"))
            if len(files) == 2 and min(path.stat().st_size for path in files) >= 1:
                break
            if proc.poll() is not None:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("child did not start writing before SIGINT")
        os.kill(proc.pid, signal.SIGINT)
        try:
            stdout, stderr = proc.communicate(timeout=CANCEL_TEST_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=CANCEL_TEST_TIMEOUT_SECONDS)
            raise AssertionError(f"child hung after SIGINT\nstdout={stdout}\nstderr={stderr}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=CANCEL_TEST_TIMEOUT_SECONDS)
    combined = (stdout or "") + (stderr or "")
    assert proc.returncode not in (0, None)
    assert "KeyboardInterrupt" in combined
    assert "CHILD_OK" not in combined
    assert sentinel.read_text(encoding="utf-8") == '{"sentinel":"keep"}\n'
    assert list(tmp_path.glob("phase8-storage-*.bin")) == []


def test_cancel_join_bound_is_logged_and_files_are_removed(monkeypatch, tmp_path, caplog):
    """A writer that ignores the stop event is logged, then its file is removed.

    The join bound is shortened so the case stays inside the hard timeout.
    The writer sleeps past that bound and does not create the file again.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Real temporary directory for the stream file.
        caplog: Pytest log capture fixture.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If the call hangs, skips the warning, leaves a stream
            file, or does not surface ``CancelledError``.
    """
    monkeypatch.setattr(storage_bench, "_WORKER_JOIN_SECONDS", 0.05, raising=False)

    def stuck(path, total_bytes, chunk_bytes, fsync):
        """Write one file and ignore the cancel event until the sleep ends.

        Args:
            path: Stream file this worker creates.
            total_bytes: Unused requested size.
            chunk_bytes: Unused chunk size.
            fsync: Unused fsync flag.

        Returns:
            A zero-byte result the cancelled run must not publish.

        Raises:
            None.
        """
        del total_bytes, chunk_bytes, fsync
        path.write_bytes(b"stuck")
        remember = getattr(storage_bench, "_remember_created_stream", None)
        if remember is not None:
            remember(path)
        time.sleep(0.4)
        return (0, [])

    monkeypatch.setattr(storage_bench, "_write_stream", stuck)
    args = benchmark_args(
        tmp_path,
        streams=1,
        total_bytes=STREAM_BYTES,
        chunk_bytes=STREAM_BYTES,
        fsync=False,
    )

    async def cancel_after_file_appears():
        task = asyncio.create_task(storage_bench.run(args))
        deadline = time.monotonic() + CANCEL_TEST_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            files = list(tmp_path.glob("phase8-storage-*.bin"))
            if files and files[0].stat().st_size >= 1:
                break
            await asyncio.sleep(0.01)
        else:
            task.cancel()
            raise AssertionError("stream file did not appear before the cancel")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert list(tmp_path.glob("phase8-storage-*.bin")) == []

    with caplog.at_level(logging.WARNING, logger="phase8_storage_benchmark"):
        call_bounded(
            lambda: asyncio.run(cancel_after_file_appears()),
            CANCEL_TEST_TIMEOUT_SECONDS,
        )
    assert "still writing after 0.05s" in caplog.text
    assert "stream files will still be removed" in caplog.text


def test_cancel_after_gather_while_waiting_for_sampler_removes_files(monkeypatch, tmp_path):
    """Cancellation after the writers finish must still delete the stream files.

    The cancel is delivered from ``stop.set()``, which is the await of the
    sampler after ``gather`` has returned.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Real temporary directory for the stream files.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If the sampler wait is not the point of cancellation,
            a stream file remains, or ``CancelledError`` does not propagate.
    """
    total = 64
    args = benchmark_args(
        tmp_path,
        streams=2,
        total_bytes=total,
        chunk_bytes=total,
        fsync=False,
    )
    fired = {"value": False}

    async def cancel_on_sampler_wait():
        original_set = asyncio.Event.set

        def hooked_set(self):
            original_set(self)
            if fired["value"]:
                return
            files = list(tmp_path.glob("phase8-storage-*.bin"))
            if len(files) == 2 and all(path.stat().st_size == total for path in files):
                fired["value"] = True
                asyncio.current_task().cancel()

        monkeypatch.setattr(asyncio.Event, "set", hooked_set)
        with pytest.raises(asyncio.CancelledError):
            await storage_bench.run(args)
        assert list(tmp_path.glob("phase8-storage-*.bin")) == []

    call_bounded(lambda: asyncio.run(cancel_on_sampler_wait()), CANCEL_TEST_TIMEOUT_SECONDS)
    assert fired["value"] is True


def test_cancel_during_sampler_wait_surfaces_the_stream_error(monkeypatch, tmp_path):
    """A cancel after a stream error must surface that error and delete files.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Real temporary directory for the stream files.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If the original ``OSError`` is replaced, its cause is
            not the cancellation, or a stream file remains.
    """

    def wrapped(path, total_bytes, chunk_bytes, fsync):
        """Write four bytes, then fail stream 0.

        Args:
            path: Stream path for this worker.
            total_bytes: Unused requested size.
            chunk_bytes: Unused chunk size.
            fsync: Unused fsync flag.

        Returns:
            A full-size result for the sibling stream.

        Raises:
            OSError: Errno 5 from stream 0.
        """
        del total_bytes, chunk_bytes, fsync
        path.write_bytes(b"FULL")
        if path.name.endswith("-0000.bin"):
            raise OSError(errno.EIO, "Input/output error")
        return (STREAM_BYTES, [0.01])

    monkeypatch.setattr(storage_bench, "_write_stream", wrapped)
    args = benchmark_args(
        tmp_path,
        streams=2,
        total_bytes=STREAM_BYTES,
        chunk_bytes=STREAM_BYTES,
        fsync=False,
    )
    fired = {"value": False}

    async def cancel_on_sampler_wait():
        original_set = asyncio.Event.set

        def hooked_set(self):
            original_set(self)
            if fired["value"]:
                return
            fired["value"] = True
            asyncio.current_task().cancel()

        monkeypatch.setattr(asyncio.Event, "set", hooked_set)
        caught_exc = None
        try:
            await storage_bench.run(args)
        except BaseException as exc:
            caught_exc = exc
        assert isinstance(caught_exc, OSError)
        assert caught_exc.errno == errno.EIO
        assert isinstance(caught_exc.__cause__, asyncio.CancelledError)
        assert list(tmp_path.glob("phase8-storage-*.bin")) == []

    call_bounded(lambda: asyncio.run(cancel_on_sampler_wait()), CANCEL_TEST_TIMEOUT_SECONDS)
    assert fired["value"] is True


def _write_child_script(path, lines):
    """Write a child script used to observe process status.

    Args:
        path: Destination ``.py`` file.
        lines: Script lines without trailing newlines.

    Returns:
        ``path``.

    Raises:
        OSError: If the script cannot be written.
    """
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _finish_child(proc, timeout_seconds):
    """Return the child's output, killing it when it ignores the timeout.

    Args:
        proc: Child process with captured text pipes.
        timeout_seconds: How long ``communicate`` may block.

    Returns:
        Standard output and standard error.

    Raises:
        AssertionError: If the child is still alive after the timeout.
    """
    try:
        return proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        raise AssertionError(f"child hung\nstdout={stdout}\nstderr={stderr}")


def test_keyboard_interrupt_after_gather_removes_files(tmp_path):
    """SIGINT while ``main`` waits on the sampler must not leave stream files.

    The child holds the sampler task after the writers finish so the signal
    arrives in that window. A pre-seeded JSON file must not remain as evidence.

    Args:
        tmp_path: Temporary directory for the child, its JSON, and stream files.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If the child exits 0, keeps the sentinel JSON, prints a
            success line, or leaves a stream file.
    """
    work = tmp_path / "streams"
    work.mkdir()
    output = tmp_path / "result.json"
    output.write_text('{"sentinel":"keep"}\n', encoding="utf-8")
    marker = tmp_path / "held"
    mib = 64 / MIB
    script = _write_child_script(
        tmp_path / "post_gather_child.py",
        [
            "import asyncio",
            "import sys",
            "from pathlib import Path",
            "sys.path.insert(0, sys.argv[1])",
            "import phase8_storage_benchmark as bench",
            "work = Path(sys.argv[2])",
            "output = sys.argv[3]",
            "marker = Path(sys.argv[4])",
            "mib = sys.argv[5]",
            "original_create_task = asyncio.create_task",
            "def create_task(coro, *args, **kwargs):",
            "    async def linger():",
            "        try:",
            "            await coro",
            "        finally:",
            "            marker.write_text('held', encoding='utf-8')",
            "            print('HELD', flush=True)",
            "            await asyncio.sleep(30)",
            "    return original_create_task(linger(), *args, **kwargs)",
            "asyncio.create_task = create_task",
            "sys.argv = [",
            "    'phase8_storage_benchmark.py',",
            "    '--path', str(work),",
            "    '--streams', '2',",
            "    '--mib-per-stream', mib,",
            "    '--chunk-mib', mib,",
            "    '--sample-interval', '30',",
            "    '--output-json', output,",
            "]",
            "bench.main()",
            "",
        ],
    )
    proc = subprocess.Popen(
        [sys.executable, str(script), str(TOOLS), str(work), str(output), str(marker), str(mib)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + CANCEL_TEST_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if marker.exists():
                break
            if proc.poll() is not None:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("child did not reach the post-gather sampler wait")
        if proc.poll() is None:
            os.kill(proc.pid, signal.SIGINT)
        stdout, stderr = _finish_child(proc, CANCEL_TEST_TIMEOUT_SECONDS)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=CANCEL_TEST_TIMEOUT_SECONDS)
    combined = (stdout or "") + (stderr or "")
    assert proc.returncode not in (0, None)
    assert "KeyboardInterrupt" in combined
    assert "bytes=" not in (stdout or "")
    assert not output.exists()
    assert list(work.glob("phase8-storage-*.bin")) == []


@pytest.mark.parametrize("exit_label", ["zero", "none", "empty", "false"])
def test_worker_successful_systemexit_is_a_failed_run(tmp_path, exit_label):
    """A successful ``SystemExit`` from a worker must not exit 0 or keep JSON.

    Args:
        tmp_path: Temporary directory for the child and the evidence file.
        exit_label: Which successful ``SystemExit`` form the worker raises.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If the child exits 0, leaves the sentinel JSON, prints
            a success line, or leaves a stream file.
    """
    work = tmp_path / "streams"
    work.mkdir()
    output = tmp_path / "result.json"
    output.write_text('{"sentinel":"stale","result":{"bytes_written":12345}}\n', encoding="utf-8")
    mib = STREAM_BYTES / MIB
    script = _write_child_script(
        tmp_path / "systemexit_child.py",
        [
            "import sys",
            "from pathlib import Path",
            "sys.path.insert(0, sys.argv[1])",
            "import phase8_storage_benchmark as bench",
            "label = sys.argv[4]",
            "original = bench._write_stream",
            "def wrapped(path, total_bytes, chunk_bytes, fsync):",
            "    if path.name.endswith('-0000.bin'):",
            "        path.write_bytes(b'x')",
            "        if label == 'zero':",
            "            raise SystemExit(0)",
            "        if label == 'none':",
            "            raise SystemExit(None)",
            "        if label == 'empty':",
            "            raise SystemExit()",
            "        raise SystemExit(False)",
            "    return original(path, total_bytes, chunk_bytes, fsync)",
            "bench._write_stream = wrapped",
            "sys.argv = [",
            "    'phase8_storage_benchmark.py',",
            "    '--path', sys.argv[2],",
            "    '--streams', '2',",
            "    '--mib-per-stream', sys.argv[5],",
            "    '--chunk-mib', sys.argv[5],",
            "    '--sample-interval', '30',",
            "    '--output-json', sys.argv[3],",
            "]",
            "bench.main()",
            "",
        ],
    )
    completed = subprocess.run(
        [sys.executable, str(script), str(TOOLS), str(work), str(output), exit_label, str(mib)],
        capture_output=True,
        text=True,
        timeout=CANCEL_TEST_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode not in (0, None)
    assert "bytes=" not in completed.stdout
    assert not output.exists()
    assert list(work.glob("phase8-storage-*.bin")) == []


def test_planted_stream_symlink_is_not_truncated(tmp_path):
    """A symlink at the stream name must not be followed or reported as success.

    Args:
        tmp_path: Temporary directory that holds the benchmark dir and the
            foreign file. Nothing outside ``tmp_path`` is used.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If the foreign bytes change, the symlink is removed,
            or ``run`` returns a result.
        StorageBenchmarkWriteError: Expected refusal, asserted by pytest.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret"
    secret.write_bytes(b"PLANTED-SECRET")
    work = tmp_path / "bench"
    work.mkdir()
    link = work / f"phase8-storage-{os.getpid()}-0000.bin"
    link.symlink_to(secret)
    args = benchmark_args(work, streams=1, total_bytes=16, chunk_bytes=16, fsync=False)
    with pytest.raises(storage_bench.StorageBenchmarkWriteError):
        call_bounded(lambda: asyncio.run(storage_bench.run(args)))
    assert secret.read_bytes() == b"PLANTED-SECRET"
    assert link.is_symlink()
    assert link.read_bytes() == b"PLANTED-SECRET"
    assert list(work.glob("phase8-storage-*.bin")) == [link]


def test_cli_planted_symlink_exits_nonzero_without_success_json(tmp_path):
    """The CLI must fail a planted stream symlink and must not publish JSON.

    Args:
        tmp_path: Temporary directory for the child, the foreign file, and JSON.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If the child exits 0, truncates the foreign file,
            prints a success line, or leaves evidence JSON.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret"
    secret.write_bytes(b"PLANTED-SECRET")
    work = tmp_path / "bench"
    work.mkdir()
    output = tmp_path / "result.json"
    output.write_text('{"sentinel":"stale","result":{"bytes_written":16}}\n', encoding="utf-8")
    mib = 16 / MIB
    script = _write_child_script(
        tmp_path / "symlink_child.py",
        [
            "import os",
            "import sys",
            "from pathlib import Path",
            "sys.path.insert(0, sys.argv[1])",
            "import phase8_storage_benchmark as bench",
            "secret = Path(sys.argv[2])",
            "work = Path(sys.argv[3])",
            "link = work / f'phase8-storage-{os.getpid()}-0000.bin'",
            "link.symlink_to(secret)",
            "sys.argv = [",
            "    'phase8_storage_benchmark.py',",
            "    '--path', str(work),",
            "    '--streams', '1',",
            "    '--mib-per-stream', sys.argv[5],",
            "    '--chunk-mib', sys.argv[5],",
            "    '--sample-interval', '30',",
            "    '--output-json', sys.argv[4],",
            "]",
            "bench.main()",
            "",
        ],
    )
    completed = subprocess.run(
        [sys.executable, str(script), str(TOOLS), str(secret), str(work), str(output), str(mib)],
        capture_output=True,
        text=True,
        timeout=CANCEL_TEST_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode not in (0, None)
    assert "bytes=" not in completed.stdout
    assert secret.read_bytes() == b"PLANTED-SECRET"
    assert not output.exists()
    links = list(work.glob("phase8-storage-*.bin"))
    assert len(links) == 1
    assert links[0].is_symlink()
    assert links[0].read_bytes() == b"PLANTED-SECRET"


def test_cancel_does_not_treat_idle_pool_threads_as_writers(monkeypatch, tmp_path, caplog):
    """An ordinary cancel must not wait out the join bound or log a stuck writer.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Real temporary directory for the stream files.
        caplog: Pytest log capture fixture.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If the call waits for idle pool threads, logs that a
            worker is still writing, or leaves a stream file.
    """
    install_slow_raw_files(monkeypatch, CANCEL_WRITE_DELAY_SECONDS)
    args = benchmark_args(
        tmp_path,
        streams=2,
        total_bytes=STREAM_BYTES,
        chunk_bytes=STREAM_BYTES,
        fsync=False,
    )

    async def cancel_after_first_bytes():
        task = asyncio.create_task(storage_bench.run(args))
        deadline = time.monotonic() + CANCEL_TEST_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            files = list(tmp_path.glob("phase8-storage-*.bin"))
            if len(files) == 2 and min(path.stat().st_size for path in files) >= 1:
                break
            await asyncio.sleep(0.01)
        else:
            task.cancel()
            raise AssertionError("stream files did not start before the cancel")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert list(tmp_path.glob("phase8-storage-*.bin")) == []

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="phase8_storage_benchmark"):
        call_bounded(lambda: asyncio.run(cancel_after_first_bytes()), CANCEL_TEST_TIMEOUT_SECONDS)
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert "still writing" not in caplog.text


def test_late_open_after_join_bound_is_removed(monkeypatch, tmp_path, caplog):
    """A file created after the join deadline must still be removed.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Real temporary directory for the stream file.
        caplog: Pytest log capture fixture.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If the call hangs, skips the stuck-writer warning, or
            leaves the stream file after the worker returns.
    """
    monkeypatch.setattr(storage_bench, "_WORKER_JOIN_SECONDS", 0.05, raising=False)
    real_path_open = Path.open
    if hasattr(storage_bench, "_open_stream_file"):
        original_open = storage_bench._open_stream_file

        def slow_exclusive(path):
            """Sleep, then create the stream file.

            Args:
                path: Stream path to create after the delay.

            Returns:
                The exclusive unbuffered handle.

            Raises:
                StorageBenchmarkWriteError: If ``path`` already exists.
                OSError: If the file cannot be created.
            """
            time.sleep(0.3)
            return original_open(path)

        monkeypatch.setattr(storage_bench, "_open_stream_file", slow_exclusive)

    def slow_path_open(self, mode="r", buffering=-1, *args, **kwargs):
        """Sleep before an unbuffered binary open.

        Args:
            mode: Open mode.
            buffering: Buffering argument.
            *args: Remaining positional open arguments.
            **kwargs: Remaining keyword open arguments.

        Returns:
            The real file handle.

        Raises:
            OSError: If the real open fails.
        """
        if mode == "wb" and buffering == 0:
            time.sleep(0.3)
        return real_path_open(self, mode, buffering, *args, **kwargs)

    monkeypatch.setattr(Path, "open", slow_path_open)
    args = benchmark_args(tmp_path, streams=1, total_bytes=16, chunk_bytes=16, fsync=False)

    async def cancel_during_open():
        task = asyncio.create_task(storage_bench.run(args))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.WARNING, logger="phase8_storage_benchmark"):
        call_bounded(lambda: asyncio.run(cancel_during_open()), CANCEL_TEST_TIMEOUT_SECONDS)
    assert list(tmp_path.glob("phase8-storage-*.bin")) == []
    assert "still writing" in caplog.text


def test_cli_replaces_stale_json_only_after_success(tmp_path):
    """A successful CLI run publishes JSON atomically and drops the staging file.

    Args:
        tmp_path: Temporary directory for the benchmark and the evidence file.

    Returns:
        None. Assertions fail the test.

    Raises:
        AssertionError: If the process fails, the stale document remains, the
            measured byte fields are missing, or the staging file remains.
    """
    output = tmp_path / "result.json"
    output.write_text('{"sentinel":"stale"}\n', encoding="utf-8")
    partial = tmp_path / "result.json.partial"
    partial.write_text("partial-stale", encoding="utf-8")
    data = tmp_path / "data"
    mib = STREAM_BYTES / MIB
    completed = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "phase8_storage_benchmark.py"),
            "--path",
            str(data),
            "--streams",
            "1",
            "--mib-per-stream",
            str(mib),
            "--chunk-mib",
            str(mib),
            "--sample-interval",
            "30",
            "--output-json",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=CANCEL_TEST_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["result"]["bytes_written"] == STREAM_BYTES
    assert "aggregate_write_mbps" in payload["result"]
    assert "aggregate_write_MBps" in payload["result"]
    assert "sentinel" not in output.read_text(encoding="utf-8")
    assert not partial.exists()
    assert list(data.glob("phase8-storage-*.bin")) == []
