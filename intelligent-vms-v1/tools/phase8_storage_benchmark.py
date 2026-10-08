#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextvars
import errno
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

from phase8_benchmark_common import SystemSampler, build_result, utc_iso, write_result

_LOG = logging.getLogger(__name__)
# How long a cancelled run waits for writers that are still inside the stream
# writer. Idle default-executor threads are not writers and are not waited on.
# asyncio task cancellation does not stop a running to_thread worker.
_WORKER_JOIN_SECONDS = 5.0


class _WriterState:
    """Per-run stop flag, active writers, and files this run created.

    Args:
        None.

    Returns:
        None.
    """

    def __init__(self) -> None:
        """Create an empty writer set and an unset stop flag.

        Args:
            None.

        Returns:
            None.

        Raises:
            None.
        """
        self.cancel = threading.Event()
        self.keep_files = False
        self.created: list[Path] = []
        self.active: set[threading.Thread] = set()
        self.lock = threading.Lock()


# Copied into each writer thread by asyncio.to_thread. The object is shared.
_WRITER_STATE: contextvars.ContextVar[_WriterState | None] = contextvars.ContextVar(
    "phase8_storage_writer_state",
    default=None,
)


class StorageBenchmarkWriteError(RuntimeError):
    """The storage benchmark stopped before the requested bytes were accepted.

    Short raw writes are retried until the submitted chunk is accepted. The
    first write that returns 0 or None fails the run immediately. That return
    does not shrink the remainder, so it is not retried and cannot spin. A
    stream total other than the requested size fails here as well, before a
    result dictionary is built. ``OSError`` from the handle is not converted
    into this type.

    Args:
        message: Why this run cannot be reported as a successful benchmark.

    Returns:
        None. Instances are raised.

    Raises:
        StorageBenchmarkWriteError: Propagated to the benchmark caller.
    """


class _SiblingWriteCancelled(StorageBenchmarkWriteError):
    """A stream returned because a sibling stream had already failed.

    ``run`` still raises the original stream error after every sibling returns.
    This type exists so that cooperative stop is not reported in place of that
    error.

    Args:
        message: Why this stream stopped.

    Returns:
        None. Instances are raised.

    Raises:
        _SiblingWriteCancelled: Propagated to the stream gatherer.
    """


def _raise_if_sibling_failed() -> None:
    """Stop this stream when another stream in the same run has already failed.

    Args:
        None.

    Returns:
        None when this stream should keep writing.

    Raises:
        _SiblingWriteCancelled: Another stream has failed, or the run was
            cancelled, and the shared stop event is set.
    """
    state = _WRITER_STATE.get()
    if state is not None and state.cancel.is_set():
        raise _SiblingWriteCancelled("storage stream stopped because another stream failed")


def _remember_created_stream(path: Path) -> None:
    """Record a stream file this run created, so cleanup can unlink it.

    A path that already existed, including a planted symlink, is not recorded.
    Callers record a path only after this run creates it.

    Args:
        path: Stream file this run created.

    Returns:
        None.

    Raises:
        None.
    """
    state = _WRITER_STATE.get()
    if state is None:
        return
    with state.lock:
        if path not in state.created:
            state.created.append(path)


def _unlink_stream_file(path: Path) -> None:
    """Unlink one stream file, logging and continuing on failure.

    ``Path.unlink`` removes a directory entry and does not follow a symlink.
    Cleanup passes only paths this run created, which are regular files.

    Args:
        path: File to remove.

    Returns:
        None.

    Raises:
        None. Unlink failures are logged.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        _LOG.warning("failed to remove storage benchmark file %s: %s", path, exc)


def _release_stream_files(paths: list[Path], keep_files: bool) -> None:
    """Remove stream files this run created.

    Missing files are ignored. An unlink failure, including a Windows
    open-handle error, is logged and does not stop cleanup of the other paths.
    Paths this run did not create are not in ``paths`` and are left alone,
    including a pre-existing file or symlink outside the benchmark directory.

    Args:
        paths: Stream files this run created.
        keep_files: When true, leave every path on disk.

    Returns:
        None.

    Raises:
        None. Unlink failures are logged.
    """
    if keep_files:
        return
    for path in paths:
        _unlink_stream_file(path)


def _unlink_if_cancelled(path: Path) -> None:
    """Remove a file this worker created when the run is already stopping.

    ``open`` or ``fsync`` can return after the run's cleanup pass. The worker
    then removes the file it just created. A path it did not create is not
    passed here. ``--keep-files`` leaves the file in place.

    Args:
        path: Stream file this worker created.

    Returns:
        None.

    Raises:
        None. Unlink failures are logged.
    """
    state = _WRITER_STATE.get()
    if state is None or state.keep_files or not state.cancel.is_set():
        return
    _unlink_stream_file(path)


def _as_failed_run(error: BaseException, *, source: str) -> BaseException:
    """Return the failure that must fail the process.

    ``SystemExit(0)``, ``SystemExit()``, and ``SystemExit(False)`` share a
    success status. Letting one escape ends the CLI with status 0, and a JSON
    file left from an earlier run then looks like this run succeeded.
    ``False == 0``, so ``SystemExit(False)`` is in that set. Genuine external
    cancellation (``CancelledError`` and ``KeyboardInterrupt``) is unchanged.

    Args:
        error: Failure from a worker, the sampler, setup, teardown, or
            publishing. This run's own shutdown cancellation is not rewritten.
        source: Short origin used in the rewritten message.

    Returns:
        A ``RuntimeError`` when ``error`` is a successful ``SystemExit``.
        Otherwise the original failure, including ``CancelledError``,
        ``KeyboardInterrupt``, and a non-zero ``SystemExit``.

    Raises:
        None.
    """
    if isinstance(error, SystemExit) and error.code in (None, 0):
        return RuntimeError(f"storage benchmark {source} raised SystemExit(0)")
    return error


def _raise_failed(error: BaseException, *, source: str) -> None:
    """Raise ``error`` as a failed run, never as a successful ``SystemExit``.

    Args:
        error: Failure that escaped a benchmark step.
        source: Short origin passed to ``_as_failed_run``.

    Returns:
        This function does not return.

    Raises:
        RuntimeError: When ``error`` is ``SystemExit`` with a success status.
            The ``SystemExit`` is chained as the cause.
        BaseException: The original failure for every other type, including
            ``KeyboardInterrupt``, ``CancelledError``, and a non-zero
            ``SystemExit``.
    """
    published = _as_failed_run(error, source=source)
    if published is not error:
        raise published from error
    raise error


def _raise_primary(failure: BaseException, secondary: BaseException | None) -> None:
    """Surface the original stream error, chaining a later failure.

    A cancellation while waiting for the sampler must not replace the stream
    error. The stream error propagates, and the later failure is its cause.
    A successful ``SystemExit`` is still rewritten so the process cannot exit 0.

    Args:
        failure: Original stream failure.
        secondary: Sampler or cancellation error observed while finishing the
            run, or None when the sampler completed.

    Returns:
        This function does not return.

    Raises:
        RuntimeError: When ``failure`` is ``SystemExit`` with a success status.
        BaseException: ``failure`` for every other type.
    """
    published = _as_failed_run(failure, source="worker")
    if secondary is None:
        if published is not failure:
            raise published from failure
        raise failure
    if published is not failure:
        failure.__cause__ = secondary
        raise published from failure
    raise failure from secondary


def _join_write_workers(state: _WriterState, timeout: float) -> None:
    """Wait until active writers leave the stream writer.

    Membership in ``state.active`` is the signal, not ``Thread.is_alive``.
    A default-executor thread stays alive in the pool after ``_write_stream``
    returns, so joining that thread would wait out ``timeout`` and log a
    writer that is already idle. The calling thread is ignored so an inline
    worker cannot deadlock the event-loop thread.

    A worker still inside ``_write_stream`` at the deadline is logged, and
    cleanup continues. ``os.open`` and ``os.fsync`` are not interrupted. The
    directory entry of a file this run already created is still removed. If
    ``open`` creates a file after that pass, the worker unlinks it before
    returning when the stop event is set. The process can still wait for the
    syscall itself while the default executor shuts down; this wait does not
    extend to idle pool threads.

    Args:
        state: Writer state for this run. Active threads are those currently
            inside ``_tracked_write``.
        timeout: Seconds allowed for every active writer to leave the writer.

    Returns:
        None.

    Raises:
        None. A writer still active at ``timeout`` is logged.
    """
    deadline = time.monotonic() + timeout
    while True:
        with state.lock:
            pending = [thread for thread in state.active if thread is not threading.current_thread()]
        if not pending:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            for thread in pending:
                _LOG.warning(
                    "storage benchmark worker %s is still writing after %ss; "
                    "stream files will still be removed",
                    thread.name,
                    timeout,
                )
            return
        time.sleep(min(0.01, remaining))


def _mark_writer_active(thread: threading.Thread) -> None:
    """Record that ``thread`` is inside the stream writer.

    Args:
        thread: Thread entering ``_tracked_write``.

    Returns:
        None.

    Raises:
        None.
    """
    state = _WRITER_STATE.get()
    if state is None:
        return
    with state.lock:
        state.active.add(thread)


def _mark_writer_inactive(thread: threading.Thread) -> None:
    """Record that ``thread`` has left the stream writer.

    Args:
        thread: Thread leaving ``_tracked_write``.

    Returns:
        None.

    Raises:
        None.
    """
    state = _WRITER_STATE.get()
    if state is None:
        return
    with state.lock:
        state.active.discard(thread)


def _tracked_write(
    path: Path,
    total_bytes: int,
    chunk_bytes: int,
    fsync: bool,
) -> tuple[int, list[float]]:
    """Mark this thread active, write one stream, and remember a new file.

    The thread is active only while this call is on the stack. An idle pool
    thread is not active. A file that appears during the call and was not
    already present is recorded for cleanup. A pre-existing path, including a
    symlink, is not recorded and is not deleted.

    Args:
        path: Destination file. The parent directory must already exist.
        total_bytes: Exact number of bytes this stream must accept.
        chunk_bytes: Maximum payload submitted for one chunk.
        fsync: When true, fsync the handle after each completed chunk.

    Returns:
        Bytes actually accepted, and one latency sample per completed chunk.

    Raises:
        StorageBenchmarkWriteError: A raw write accepted no bytes or returned an
            unusable count, the path already existed, or another stream already
            failed.
        OSError: A write or fsync failed.
    """
    thread = threading.current_thread()
    already_there = path.is_symlink() or path.exists()
    _mark_writer_active(thread)
    try:
        return _write_stream(path, total_bytes, chunk_bytes, fsync)
    finally:
        if not already_there and not path.is_symlink() and path.exists():
            _remember_created_stream(path)
        _mark_writer_inactive(thread)


def _stream_open_flags() -> int:
    """Return exclusive unbuffered create flags for a new stream file.

    ``O_CREAT | O_EXCL | O_WRONLY`` fails if the path exists, including when
    the final component is a symlink, and does not truncate the target.
    ``O_NOFOLLOW`` is included where the platform provides it. ``O_BINARY``
    is included on Windows so the raw bytes are unchanged.

    Args:
        None.

    Returns:
        The ``os.open`` flag mask.

    Raises:
        None.
    """
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _open_stream_file(path: Path):
    """Create a new unbuffered stream file without following a final symlink.

    An existing path is refused. The foreign target of a planted symlink is
    not opened, truncated, or deleted. The new file is recorded for cleanup.

    Args:
        path: Stream path this run will create. The parent must already exist.

    Returns:
        A binary handle opened with ``buffering=0``.

    Raises:
        StorageBenchmarkWriteError: ``path`` already exists or is a symlink.
            The path is not recorded and is not unlinked.
        OSError: The file could not be created for another reason.
    """
    try:
        fd = os.open(path, _stream_open_flags(), 0o644)
    except OSError as exc:
        if exc.errno in (errno.EEXIST, errno.ELOOP):
            raise StorageBenchmarkWriteError(
                f"storage benchmark refused pre-existing stream path {path}"
            ) from exc
        raise
    try:
        handle = os.fdopen(fd, "wb", buffering=0)
    except Exception:
        os.close(fd)
        _remember_created_stream(path)
        raise
    _remember_created_stream(path)
    return handle


def _primary_stream_error(outcomes: list[object]) -> BaseException | None:
    """Choose the stream error that must fail the run.

    A sibling that stopped after another failure is not the primary error.

    Args:
        outcomes: Per-stream results from ``asyncio.gather``. Tuples are
            successes. Exceptions are failures.

    Returns:
        The original stream failure, or None when every stream succeeded.

    Raises:
        None.
    """
    failures = [item for item in outcomes if isinstance(item, BaseException)]
    if not failures:
        return None
    for failure in failures:
        if not isinstance(failure, _SiblingWriteCancelled):
            return failure
    return failures[0]


def _accept_payload(handle, payload: bytes) -> int:
    """Write one chunk, counting only bytes the raw handle accepts.

    Files are opened with buffering disabled. ``RawIOBase.write`` may accept a
    short count, so the unaccepted tail is submitted again. Each accepted call
    moves the tail forward by at least one byte, which bounds the retry by the
    chunk length. The first 0 or None fails the run. There is no retry of a
    stalled write.

    Args:
        handle: Binary handle opened with ``buffering=0``.
        payload: Bytes that must all be accepted before this call returns.

    Returns:
        The number of accepted bytes, equal to ``len(payload)``.

    Raises:
        StorageBenchmarkWriteError: The handle made no progress, or returned a
            count that is not a positive integer inside the submitted slice.
        OSError: ``handle.write`` failed. Already-accepted bytes are not success.
    """
    accepted = 0
    while accepted < len(payload):
        _raise_if_sibling_failed()
        count = handle.write(payload[accepted:])
        if count is None or count == 0:
            raise StorageBenchmarkWriteError(
                "raw write returned 0 or None; the first stalled write fails the run"
            )
        if isinstance(count, bool) or not isinstance(count, int):
            raise StorageBenchmarkWriteError(f"raw write returned a non-integer count: {count!r}")
        remaining = len(payload) - accepted
        if count < 0 or count > remaining:
            raise StorageBenchmarkWriteError(
                f"raw write returned {count}, outside the {remaining} bytes submitted"
            )
        accepted += count
    return accepted


def _write_stream(path: Path, total_bytes: int, chunk_bytes: int, fsync: bool) -> tuple[int, list[float]]:
    """Write one stream and return bytes actually accepted plus chunk latencies.

    ``buffering=0`` keeps the measurement on raw I/O. A buffered ``write`` would
    report a full count before the kernel accepted the bytes and would move
    ``fsync`` relative to that buffer. When ``fsync`` is true it still runs once
    per completed chunk, after that chunk's bytes have been accepted. One
    latency sample is recorded per chunk, including its short-write retries.
    The first 0 or None from ``write`` fails this stream. If another stream
    in the same run has already failed, or the run has been cancelled, this
    stream stops instead of opening or continuing. The file is created with
    exclusive unbuffered flags, so a pre-existing path is refused. If this
    call creates a file after the run has already started cleanup, that file
    is unlinked before this call returns when the stop event is set. A
    ``fsync`` already in progress is not interrupted; the directory entry is
    removed by cleanup while the syscall finishes.

    Args:
        path: Destination file. The parent directory must already exist.
        total_bytes: Exact number of bytes this stream must accept.
        chunk_bytes: Maximum payload submitted for one chunk.
        fsync: When true, fsync the handle after each completed chunk.

    Returns:
        Bytes actually accepted, and one latency sample per completed chunk.

    Raises:
        StorageBenchmarkWriteError: A raw write accepted no bytes or returned an
            unusable count, or the path already existed. The stream does not
            return a partial total.
        OSError: A write or fsync failed.
    """
    _raise_if_sibling_failed()
    written = 0
    latencies = []
    block = b"\0" * chunk_bytes
    opened = False
    # buffering=0 is required so short raw writes stay visible to _accept_payload.
    handle = _open_stream_file(path)
    opened = True
    try:
        _raise_if_sibling_failed()
        with handle:
            while written < total_bytes:
                _raise_if_sibling_failed()
                payload = block[: min(chunk_bytes, total_bytes - written)]
                started = time.perf_counter()
                accepted = _accept_payload(handle, payload)
                if fsync:
                    os.fsync(handle.fileno())
                latencies.append(time.perf_counter() - started)
                written += accepted
        return written, latencies
    finally:
        if opened:
            _unlink_if_cancelled(path)


async def run(args) -> dict:
    """Run the synthetic concurrent storage-write baseline.

    Args:
        args: Parsed storage benchmark arguments.

    Returns:
        Phase-8 benchmark result dictionary with write throughput/latency evidence.
        Returned only after every requested byte was accepted.

    Raises:
        ValueError: If stream or chunk byte sizes are non-positive.
        StorageBenchmarkWriteError: A raw write stalled, a pre-existing stream
            path was refused, a sibling was stopped after another stream failed,
            or the accepted total differs from the requested size. No result
            dictionary is returned.
        OSError: A stream write or fsync failed. No result dictionary is returned.
        RuntimeError: A worker, the sampler, or result assembly raised
            ``SystemExit`` with a success status. The ``SystemExit`` is chained
            as the cause. The process must not exit 0.
        asyncio.CancelledError: The run was cancelled, including a keyboard
            interrupt delivered through ``asyncio.run``, at any await from the
            start of the writers through the sampler wait and the return.
            Active writers are asked to stop and waited on within a bound, and
            every stream file this run created is removed, before this
            exception propagates. It is not converted into a successful result.
            A cancellation while a stream error is already in hand surfaces
            that stream error instead, with this cancellation chained as its
            cause.
    """
    target_dir = Path(args.path)
    target_dir.mkdir(parents=True, exist_ok=True)
    bytes_per_stream = int(args.mib_per_stream * 1024 * 1024)
    chunk_bytes = int(args.chunk_mib * 1024 * 1024)
    if bytes_per_stream <= 0 or chunk_bytes <= 0:
        raise ValueError("mib-per-stream and chunk-mib must be positive")

    sampler = SystemSampler()
    samples = []
    stop = asyncio.Event()

    async def sample_loop():
        while not stop.is_set():
            samples.append(sampler.sample())
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(0.1, args.sample_interval))
            except asyncio.TimeoutError:
                pass

    paths = [
        target_dir / f"phase8-storage-{os.getpid()}-{index:04d}.bin"
        for index in range(args.streams)
    ]
    state = _WriterState()
    state.keep_files = bool(args.keep_files)
    sample_task: asyncio.Task[None] | None = None
    token = _WRITER_STATE.set(state)
    try:
        sample_task = asyncio.create_task(sample_loop())
        started_at = utc_iso()
        started = time.perf_counter()

        async def run_stream(path: Path):
            try:
                return await asyncio.to_thread(
                    _tracked_write,
                    path,
                    bytes_per_stream,
                    chunk_bytes,
                    args.fsync,
                )
            except BaseException:
                # Ask siblings to stop. gather still waits for every stream
                # unless this task itself is being cancelled. Cancelling the
                # asyncio task would abandon the thread, so the stop event is
                # how a sibling returns.
                state.cancel.set()
                raise

        # return_exceptions waits for every stream, including one that opens
        # after the first worker has already failed. Cancellation of this
        # task still raises CancelledError here. Cleanup is the finally below,
        # which also covers the sampler await after gather returns.
        outcomes = await asyncio.gather(
            *(run_stream(path) for path in paths),
            return_exceptions=True,
        )
        failure = _primary_stream_error(outcomes)
        if failure is not None:
            state.cancel.set()
            stop.set()
            secondary: BaseException | None = None
            try:
                await sample_task
            except BaseException as exc:
                secondary = exc
            _raise_primary(failure, secondary)

        results = outcomes
        duration = time.perf_counter() - started
        stop.set()
        try:
            await sample_task
        except BaseException as exc:
            _raise_failed(exc, source="sampler")

        total_bytes = sum(item[0] for item in results)
        latencies = [latency for _, items in results for latency in items]
        expected_bytes = bytes_per_stream * len(paths)
        if total_bytes != expected_bytes:
            raise StorageBenchmarkWriteError(
                f"storage benchmark accepted {total_bytes} bytes, expected {expected_bytes}"
            )

        result = build_result(
            workload_type="synthetic-storage-write",
            workload_config={
                "path": str(target_dir.resolve()),
                "streams": args.streams,
                "mib_per_stream": args.mib_per_stream,
                "chunk_mib": args.chunk_mib,
                "fsync_each_chunk": args.fsync,
            },
            started_at=started_at,
            duration_seconds=duration,
            warmup_seconds=0.0,
            operations_ok=len(latencies),
            operations_failed=0,
            latencies_seconds=latencies,
            samples=samples,
            storage_path=str(target_dir),
            extra_metrics={
                "bytes_written": total_bytes,
                "aggregate_write_mbps": (total_bytes * 8 / duration / 1_000_000) if duration > 0 else None,
                "aggregate_write_MBps": (total_bytes / duration / 1_000_000) if duration > 0 else None,
            },
        )
        return result
    except BaseException as exc:
        # A successful SystemExit from any phase must not become the process
        # status. External cancellation is re-raised after finally cleans up.
        if isinstance(exc, SystemExit) and exc.code in (None, 0):
            raise RuntimeError("storage benchmark run raised SystemExit(0)") from exc
        raise
    finally:
        state.cancel.set()
        stop.set()
        _join_write_workers(state, _WORKER_JOIN_SECONDS)
        with state.lock:
            created = list(state.created)
        _release_stream_files(created, state.keep_files)
        if sample_task is not None and not sample_task.done():
            sample_task.cancel()
        _WRITER_STATE.reset(token)


def _staging_json_path(json_path: Path) -> Path:
    """Return the temporary JSON path published with ``os.replace``.

    Args:
        json_path: Final ``--output-json`` path.

    Returns:
        A sibling path whose name ends in ``.partial``.

    Raises:
        None.
    """
    return json_path.with_name(f"{json_path.name}.partial")


def _discard_benchmark_json(json_path: Path) -> None:
    """Remove this run's JSON evidence and any staging file.

    The optional CSV is an append-only log of publishes that completed, so
    this function does not remove it. Unlink does not follow a symlink, so a
    destination that points outside the directory is not truncated.

    Args:
        json_path: Configured ``--output-json`` path.

    Returns:
        None after both paths are absent or were already missing.

    Raises:
        OSError: If a present file cannot be removed.
    """
    json_path.unlink(missing_ok=True)
    _staging_json_path(json_path).unlink(missing_ok=True)


def _publish_benchmark_result(result: dict, json_path: Path, csv_path: str | None) -> None:
    """Publish JSON only after a successful run, by atomic replace.

    ``write_result`` validates and writes a sibling staging file, appending the
    optional CSV from that same call. ``os.replace`` then moves the staging
    file onto ``--output-json``. The destination is removed before the run, so
    a crash before the replace leaves it absent.

    Args:
        result: Completed benchmark result dictionary.
        json_path: Destination JSON evidence path.
        csv_path: Optional append-only CSV summary path.

    Returns:
        None after the destination JSON is this run's evidence.

    Raises:
        Exception: Validation or filesystem failures from ``write_result`` or
            ``os.replace``.
    """
    staging = _staging_json_path(json_path)
    staging.unlink(missing_ok=True)
    write_result(result, json_path=str(staging), csv_path=csv_path)
    os.replace(staging, json_path)


def main():
    """Parse CLI arguments, execute the storage benchmark, and write results.

    Evidence policy: ``--output-json`` is removed before the run. JSON is
    written only after ``run`` succeeds, to a sibling ``<name>.partial`` file,
    then ``os.replace`` moves that file onto the destination. A failed run,
    including cancellation and a successful ``SystemExit`` from a worker,
    leaves the destination absent, so an earlier run's JSON cannot be read as
    this run's result. ``--output-csv`` is an append-only log of publishes that
    completed and is not deleted when a later run fails. ``--help`` and
    argument errors still exit from ``argparse`` before any file is removed.

    Args:
        None. Arguments are read from the process command line.

    Returns:
        None after result files and the summary line are written.

    Raises:
        RuntimeError: A successful ``SystemExit`` from the run or from
            publishing is rewritten to this so the process cannot exit 0.
        Exception: Benchmark or output failures propagate and leave no JSON
            evidence for this run.
        KeyboardInterrupt: Propagates when the run is interrupted.
        asyncio.CancelledError: Propagates when the run task is cancelled.
    """
    parser = argparse.ArgumentParser(
        description="Phase 8 synthetic concurrent storage write baseline; not VMS recording certification"
    )
    parser.add_argument("--path", default=tempfile.gettempdir())
    parser.add_argument("--streams", type=int, default=4)
    parser.add_argument("--mib-per-stream", type=float, default=256)
    parser.add_argument("--chunk-mib", type=float, default=1)
    parser.add_argument("--fsync", action="store_true")
    parser.add_argument("--keep-files", action="store_true")
    parser.add_argument("--sample-interval", type=float, default=1.0)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-csv")
    args = parser.parse_args()
    if args.streams < 1:
        raise SystemExit("streams must be >= 1")
    json_path = Path(args.output_json)
    _discard_benchmark_json(json_path)
    try:
        result = asyncio.run(run(args))
        _publish_benchmark_result(result, json_path, args.output_csv)
    except BaseException as exc:
        _discard_benchmark_json(json_path)
        _raise_failed(exc, source="publish")
    print(
        f"bytes={result['result']['bytes_written']} "
        f"write_MBps={result['result']['aggregate_write_MBps']:.2f} "
        f"p95_chunk_ms={result['result']['latency']['p95_ms']}"
    )


if __name__ == "__main__":
    main()
