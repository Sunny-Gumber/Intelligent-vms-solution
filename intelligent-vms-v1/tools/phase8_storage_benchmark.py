#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextvars
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

from phase8_benchmark_common import SystemSampler, build_result, utc_iso, write_result

_LOG = logging.getLogger(__name__)
# Shared by the threads of one run(). The first stalled or failed stream sets
# the event; siblings observe it and return. asyncio task cancellation does not
# join a running to_thread worker, so this event is how a sibling stops.
_WRITE_CANCEL: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
    "phase8_storage_write_cancel",
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
        _SiblingWriteCancelled: Another stream has failed and set the cancel event.
    """
    cancel = _WRITE_CANCEL.get()
    if cancel is not None and cancel.is_set():
        raise _SiblingWriteCancelled("storage stream stopped because another stream failed")


def _release_stream_files(paths: list[Path], keep_files: bool) -> None:
    """Remove every stream path this run may have created.

    Missing files are ignored. An unlink failure, including a Windows
    open-handle error, is logged and does not stop cleanup of the other paths.

    Args:
        paths: Stream files the run planned to create, whether or not they exist.
        keep_files: When true, leave every path on disk.

    Returns:
        None.

    Raises:
        None. Unlink failures are logged.
    """
    if keep_files:
        return
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            _LOG.warning("failed to remove storage benchmark file %s: %s", path, exc)


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
    in the same run has already failed, this stream stops instead of opening
    or continuing.

    Args:
        path: Destination file. The parent directory must already exist.
        total_bytes: Exact number of bytes this stream must accept.
        chunk_bytes: Maximum payload submitted for one chunk.
        fsync: When true, fsync the handle after each completed chunk.

    Returns:
        Bytes actually accepted, and one latency sample per completed chunk.

    Raises:
        StorageBenchmarkWriteError: A raw write accepted no bytes or returned an
            unusable count. The stream does not return a partial total.
        OSError: A write or fsync failed.
    """
    _raise_if_sibling_failed()
    written = 0
    latencies = []
    block = b"\0" * chunk_bytes
    # buffering=0 is required so short raw writes stay visible to _accept_payload.
    with path.open("wb", buffering=0) as handle:
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


async def run(args) -> dict:
    """Run the synthetic concurrent storage-write baseline.

    Args:
        args: Parsed storage benchmark arguments.

    Returns:
        Phase-8 benchmark result dictionary with write throughput/latency evidence.
        Returned only after every requested byte was accepted.

    Raises:
        ValueError: If stream or chunk byte sizes are non-positive.
        StorageBenchmarkWriteError: A raw write stalled, a sibling was stopped
            after another stream failed, or the accepted total differs from the
            requested size. No result dictionary is returned.
        OSError: A stream write or fsync failed. No result dictionary is returned.
            Sibling streams are joined before their files are removed.
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
    sample_task = asyncio.create_task(sample_loop())
    started_at = utc_iso()
    started = time.perf_counter()
    cancel_writes = threading.Event()
    token = _WRITE_CANCEL.set(cancel_writes)

    async def run_stream(path: Path):
        try:
            return await asyncio.to_thread(
                _write_stream,
                path,
                bytes_per_stream,
                chunk_bytes,
                args.fsync,
            )
        except Exception:
            # Ask siblings to stop, then let gather join every worker before
            # cleanup. Cancelling the asyncio task would abandon the thread.
            cancel_writes.set()
            raise

    try:
        # return_exceptions waits for every stream, including one that opens
        # after the first worker has already failed.
        outcomes = await asyncio.gather(
            *(run_stream(path) for path in paths),
            return_exceptions=True,
        )
    finally:
        _WRITE_CANCEL.reset(token)

    failure = _primary_stream_error(outcomes)
    if failure is not None:
        stop.set()
        sample_error = None
        try:
            await sample_task
        except Exception as exc:
            sample_error = exc
        _release_stream_files(paths, args.keep_files)
        if sample_error is not None:
            raise failure from sample_error
        raise failure

    results = outcomes
    duration = time.perf_counter() - started
    stop.set()
    await sample_task

    total_bytes = sum(item[0] for item in results)
    latencies = [latency for _, items in results for latency in items]
    _release_stream_files(paths, args.keep_files)
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


def main():
    """Parse storage benchmark arguments, execute baseline and write evidence."""
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
    result = asyncio.run(run(args))
    write_result(result, json_path=args.output_json, csv_path=args.output_csv)
    print(
        f"bytes={result['result']['bytes_written']} "
        f"write_MBps={result['result']['aggregate_write_MBps']:.2f} "
        f"p95_chunk_ms={result['result']['latency']['p95_ms']}"
    )


if __name__ == "__main__":
    main()
