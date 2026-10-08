#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from collections.abc import Awaitable
from typing import TypeVar

from phase8_benchmark_common import SystemSampler, build_result, utc_iso, write_result


LOG = logging.getLogger(__name__)
_T = TypeVar("_T")


async def attempt(host: str, port: int, timeout: float) -> tuple[bool, float]:
    """Attempt one bounded TCP connection and measure completion latency.

    Args:
        host: TCP destination host.
        port: TCP destination port.
        timeout: Maximum connection wait in seconds.

    Returns:
        A tuple containing connection success and elapsed seconds.

    Raises:
        Exception: Unexpected programming/runtime failures propagate instead of
            being counted as ordinary network failures.
    """
    started = time.perf_counter()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
    except (OSError, asyncio.TimeoutError):
        return False, time.perf_counter() - started

    writer.close()
    try:
        await writer.wait_closed()
    except OSError as exc:
        # The connection itself succeeded; a peer reset during local close is
        # a cleanup condition, not a failed connection attempt.
        LOG.debug("tcp_reconnect_close_failed error=%s", exc.__class__.__name__)

    return True, time.perf_counter() - started


def _drain_queue(queue: asyncio.Queue[int | None]) -> None:
    """Drop queued work left behind by a cancelled worker.

    Args:
        queue: Attempt queue whose unfinished items should be discarded.

    Returns:
        None after every item still sitting in the queue is marked done.

    Raises:
        ValueError: If the queue's done count is already balanced.
    """
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        queue.task_done()


def _task_error(task: asyncio.Task[object]) -> BaseException | None:
    """Return a finished task's error, ignoring cancellation.

    Args:
        task: Worker or enqueue task to inspect.

    Returns:
        The task exception, or None when the task is unfinished, cancelled,
        or completed normally.
    """
    if not task.done() or task.cancelled():
        return None
    return task.exception()


async def _await_releasing_cancellation(awaitable: Awaitable[_T]) -> _T:
    """Await cleanup even if this task was already cancelled.

    Task cancellation is sticky: a later ``await`` raises ``CancelledError``
    again. Drop that count only while waiting for shutdown, then restore it so
    an external cancel is still reported after cleanup finishes.

    Args:
        awaitable: Shutdown awaitable, usually a gather of worker tasks.

    Returns:
        The awaitable's result after shutdown is allowed to finish.

    Raises:
        Exception: Propagates a non-cancellation failure from the awaitable.
    """
    current = asyncio.current_task()
    held = current.cancelling() if current is not None else 0
    if current is not None:
        while current.cancelling():
            current.uncancel()
    try:
        return await awaitable
    finally:
        if current is not None:
            for _ in range(held):
                current.cancel()


async def run(args) -> dict:
    """Run the configured reconnect workload and build benchmark evidence.

    Args:
        args: Parsed benchmark arguments containing workload and output options.

    Returns:
        A benchmark result dictionary compatible with the Phase 8 schema.
        Returned only when every worker finishes. Expected per-attempt TCP
        failures are counted inside that dictionary; they are not run failures.

    Raises:
        Exception: Unexpected worker or sampling failures propagate so benchmark
            evidence is not silently produced from a broken run. ``OSError`` and
            ``asyncio.TimeoutError`` inside one attempt stay measurements.
    """
    queue: asyncio.Queue[int | None] = asyncio.Queue(maxsize=max(1, args.concurrency * 4))
    latencies: list[float] = []
    ok = failed = 0
    lock = asyncio.Lock()

    async def worker(measure: bool):
        nonlocal ok, failed
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                # attempt() records OSError and asyncio.TimeoutError as
                # measurements. Any other exception is a broken run and must
                # escape so the remaining workers can be cancelled.
                success, elapsed = await attempt(args.host, args.port, args.timeout)
                async with lock:
                    if success:
                        ok += 1
                    else:
                        failed += 1
                    if measure:
                        latencies.append(elapsed)
            finally:
                queue.task_done()

    async def enqueue(count: int, worker_count: int) -> None:
        for index in range(count):
            await queue.put(index)
        for _ in range(worker_count):
            await queue.put(None)

    async def drive(count: int, measure: bool):
        # Do not wait on queue.join() after a worker dies. That worker has
        # already marked its current item done, but it never takes its
        # sentinel, and a full queue can also block the producer forever.
        worker_count = max(1, args.concurrency)
        workers = [asyncio.create_task(worker(measure)) for _ in range(worker_count)]
        enqueue_task = asyncio.create_task(enqueue(count, worker_count))
        watched = [enqueue_task, *workers]
        error: BaseException | None = None
        try:
            await asyncio.wait(watched, return_when=asyncio.FIRST_EXCEPTION)
            for task in watched:
                error = _task_error(task)
                if error is not None:
                    break
        finally:
            for task in watched:
                if not task.done():
                    task.cancel()
            outcomes = await _await_releasing_cancellation(
                asyncio.gather(*watched, return_exceptions=True)
            )
            _drain_queue(queue)
            for outcome in outcomes:
                if not isinstance(outcome, BaseException) or isinstance(outcome, asyncio.CancelledError):
                    continue
                if error is None:
                    error = outcome
                    continue
                if outcome is not error:
                    LOG.error(
                        "reconnect_benchmark_worker_cleanup_error error=%s",
                        outcome.__class__.__name__,
                    )
        if error is not None:
            LOG.error(
                "reconnect_benchmark_worker_failed error=%s",
                error.__class__.__name__,
            )
            raise error

    warmup_seconds = 0.0
    if args.warmup_attempts:
        warmup_started = time.perf_counter()
        await drive(args.warmup_attempts, False)
        warmup_seconds = time.perf_counter() - warmup_started
        ok = failed = 0

    sampler = SystemSampler()
    samples = []
    stop = asyncio.Event()

    async def sample_loop():
        while not stop.is_set():
            samples.append(sampler.sample())
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(0.1, args.sample_interval))
            except asyncio.TimeoutError:
                # Expected sample cadence timeout while the benchmark is active.
                continue

    sample_task = asyncio.create_task(sample_loop())
    started_at = utc_iso()
    started = time.perf_counter()
    drive_error: BaseException | None = None
    duration = 0.0
    try:
        await drive(args.attempts, True)
        duration = time.perf_counter() - started
    except BaseException as exc:
        drive_error = exc
    stop.set()
    sample_error: BaseException | None = None
    try:
        await _await_releasing_cancellation(sample_task)
    except BaseException as exc:
        sample_error = exc
    if drive_error is not None:
        if sample_error is not None and not isinstance(sample_error, asyncio.CancelledError):
            LOG.error(
                "reconnect_benchmark_sampler_failed error=%s",
                sample_error.__class__.__name__,
            )
        raise drive_error
    if sample_error is not None:
        raise sample_error

    return build_result(
        workload_type="tcp-reconnect-storm",
        workload_config={
            "host": args.host,
            "port": args.port,
            "attempts": args.attempts,
            "concurrency": args.concurrency,
            "timeout_seconds": args.timeout,
        },
        started_at=started_at,
        duration_seconds=duration,
        warmup_seconds=warmup_seconds,
        operations_ok=ok,
        operations_failed=failed,
        latencies_seconds=latencies,
        samples=samples,
    )


def main():
    """Parse CLI arguments, execute the reconnect benchmark, and write results.

    Args:
        None. Arguments are read from the process command line.

    Returns:
        None after result files and the summary line are written.

    Raises:
        Exception: Propagates benchmark or output failures to produce a non-zero
            process exit instead of publishing invalid evidence.
    """
    parser = argparse.ArgumentParser(description="Phase 8 TCP reconnect benchmark")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8554)
    parser.add_argument("--attempts", type=int, default=10000)
    parser.add_argument("--warmup-attempts", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--sample-interval", type=float, default=1.0)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-csv")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    write_result(result, json_path=args.output_json, csv_path=args.output_csv)
    print(
        f"ok={result['result']['operations_ok']} "
        f"failed={result['result']['operations_failed']} "
        f"rate={result['result']['throughput_ops_s']:.2f}/s"
    )


if __name__ == "__main__":
    main()
