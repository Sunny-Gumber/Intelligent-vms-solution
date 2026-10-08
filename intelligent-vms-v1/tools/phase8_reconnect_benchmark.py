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


def _publishable_worker_error(error: BaseException) -> BaseException:
    """Return the worker failure that must fail the process.

    ``SystemExit(0)`` and ``SystemExit()`` would otherwise end the CLI with
    status 0 and no evidence file, which looks like a successful run.

    Args:
        error: Failure recorded from a worker that this drive did not cancel.

    Returns:
        A ``RuntimeError`` when ``error`` is a successful ``SystemExit``.
        Otherwise the original failure, including ``CancelledError`` and
        ``KeyboardInterrupt``.
    """
    if isinstance(error, SystemExit) and error.code in (None, 0):
        return RuntimeError("reconnect benchmark worker raised SystemExit(0)")
    return error


def _require_complete_attempts(
    expected: int,
    *,
    ok_count: int,
    failed_count: int,
    dequeued: int,
) -> None:
    """Refuse to publish a result whose counts do not match the workload.

    Args:
        expected: Configured attempt count for this drive.
        ok_count: Successful attempts recorded for this drive.
        failed_count: Measured per-attempt failures recorded for this drive.
        dequeued: Work items workers actually took, excluding shutdown sentinels.

    Returns:
        None when both comparisons match.

    Raises:
        RuntimeError: When the recorded attempts differ from the queue or the
            configured attempt count. Callers must not build a result after this.
    """
    finished = ok_count + failed_count
    if finished != expected or finished != dequeued:
        raise RuntimeError(
            "reconnect benchmark refused partial result "
            f"ok={ok_count} failed={failed_count} dequeued={dequeued} attempts={expected}"
        )


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
            evidence is not produced. ``OSError`` and ``asyncio.TimeoutError``
            inside one attempt stay measurements.
        asyncio.CancelledError: Propagates when a worker raises it on its own,
            and when the whole run is cancelled externally.
        KeyboardInterrupt: Propagates when a worker raises it. This is not a
            measured attempt and not a successful result.
        RuntimeError: A worker ``SystemExit(0)`` is rewritten to this so the
            process cannot exit 0. Also raised when recorded attempts do not
            match the attempts that were dequeued or configured.
    """
    queue: asyncio.Queue[int | None] = asyncio.Queue(maxsize=max(1, args.concurrency * 4))
    latencies: list[float] = []
    ok = failed = 0
    dequeued = 0
    lock = asyncio.Lock()
    shutdown_requested = False
    worker_error: BaseException | None = None
    failure_event = asyncio.Event()

    async def worker(measure: bool) -> None:
        nonlocal ok, failed, dequeued, worker_error
        try:
            while True:
                item = await queue.get()
                try:
                    if item is None:
                        return
                    async with lock:
                        dequeued += 1
                    # attempt() records OSError and asyncio.TimeoutError as
                    # measurements. Any other failure, including CancelledError
                    # raised by the attempt itself, fails the run.
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
        except BaseException as exc:
            # drive() sets shutdown_requested before task.cancel(). Those
            # CancelledErrors are cleanup. A CancelledError that arrives
            # before that flag is the attempt dying on its own.
            if shutdown_requested and isinstance(exc, asyncio.CancelledError):
                raise
            if worker_error is None:
                worker_error = exc
            failure_event.set()
            # KeyboardInterrupt and SystemExit abort the loop or the process
            # if they escape a nested task. Hold them for the parent coroutine.
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                return
            raise

    async def enqueue(count: int, worker_count: int) -> None:
        for index in range(count):
            await queue.put(index)
        for _ in range(worker_count):
            await queue.put(None)

    async def drive(count: int, measure: bool) -> None:
        # Do not wait on queue.join() after a worker dies. That worker has
        # already marked its current item done, but it never takes its
        # sentinel, and a full queue can also block the producer forever.
        nonlocal shutdown_requested, worker_error, failure_event
        shutdown_requested = False
        worker_error = None
        failure_event = asyncio.Event()
        started_ok, started_failed, started_dequeued = ok, failed, dequeued
        worker_count = max(1, args.concurrency)
        workers = [asyncio.create_task(worker(measure)) for _ in range(worker_count)]
        enqueue_task = asyncio.create_task(enqueue(count, worker_count))
        watch_task = asyncio.create_task(failure_event.wait())
        work = {enqueue_task, *workers}
        requested_cancel: set[asyncio.Task[object]] = set()
        try:
            while work:
                done, _pending = await asyncio.wait(work | {watch_task}, return_when=asyncio.FIRST_COMPLETED)
                if worker_error is not None:
                    break
                for task in done:
                    if task is watch_task or task not in work:
                        continue
                    work.discard(task)
                    if task.cancelled():
                        if worker_error is None:
                            worker_error = asyncio.CancelledError("reconnect benchmark worker cancelled")
                        break
                    task_error = task.exception()
                    if task_error is not None and worker_error is None:
                        worker_error = task_error
                        break
                if worker_error is not None:
                    break
        finally:
            shutdown_requested = True
            for task in (*workers, enqueue_task, watch_task):
                if not task.done():
                    requested_cancel.add(task)
                    task.cancel()
            outcomes = await _await_releasing_cancellation(
                asyncio.gather(*workers, enqueue_task, watch_task, return_exceptions=True)
            )
            _drain_queue(queue)
            for task, outcome in zip((*workers, enqueue_task, watch_task), outcomes, strict=True):
                if not isinstance(outcome, BaseException):
                    continue
                if isinstance(outcome, asyncio.CancelledError) and task in requested_cancel:
                    continue
                if worker_error is None:
                    worker_error = outcome
                    continue
                if outcome is not worker_error and not isinstance(outcome, asyncio.CancelledError):
                    LOG.error(
                        "reconnect_benchmark_worker_cleanup_error error=%s",
                        outcome.__class__.__name__,
                    )
        if worker_error is not None:
            LOG.error(
                "reconnect_benchmark_worker_failed error=%s",
                worker_error.__class__.__name__,
            )
            published = _publishable_worker_error(worker_error)
            if published is worker_error:
                raise worker_error
            raise published from worker_error
        _require_complete_attempts(
            count,
            ok_count=ok - started_ok,
            failed_count=failed - started_failed,
            dequeued=dequeued - started_dequeued,
        )

    warmup_seconds = 0.0
    if args.warmup_attempts:
        warmup_started = time.perf_counter()
        await drive(args.warmup_attempts, False)
        warmup_seconds = time.perf_counter() - warmup_started
        ok = failed = dequeued = 0

    sampler = SystemSampler()
    samples = []
    stop = asyncio.Event()

    async def sample_loop() -> None:
        while not stop.is_set():
            samples.append(sampler.sample())
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(0.1, args.sample_interval))
            except asyncio.TimeoutError:
                # Expected sample cadence timeout while the benchmark is active.
                continue

    async def drive_measured() -> BaseException | None:
        # A nested task that lets KeyboardInterrupt or SystemExit escape will
        # abort the loop or exit 0 before the parent can fail the run cleanly.
        try:
            await drive(args.attempts, True)
        except (KeyboardInterrupt, SystemExit) as exc:
            return exc
        return None

    sample_task = asyncio.create_task(sample_loop())
    drive_task = asyncio.create_task(drive_measured())
    started_at = utc_iso()
    started = time.perf_counter()
    external_error: BaseException | None = None
    try:
        await asyncio.wait({drive_task, sample_task}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException as exc:
        external_error = exc
    stop.set()
    if not drive_task.done():
        drive_task.cancel()
    drive_error: BaseException | None = None
    try:
        drive_outcome = await _await_releasing_cancellation(drive_task)
    except BaseException as exc:
        drive_error = exc
    else:
        if isinstance(drive_outcome, BaseException):
            drive_error = drive_outcome
    if not sample_task.done() and (external_error is not None or drive_error is not None):
        sample_task.cancel()
    sample_error: BaseException | None = None
    try:
        await _await_releasing_cancellation(sample_task)
    except BaseException as exc:
        sample_error = exc
    duration = time.perf_counter() - started
    if external_error is not None:
        if sample_error is not None and not isinstance(sample_error, asyncio.CancelledError):
            LOG.error(
                "reconnect_benchmark_sampler_failed error=%s",
                sample_error.__class__.__name__,
            )
        raise external_error
    if sample_error is not None and not isinstance(sample_error, asyncio.CancelledError):
        if drive_error is not None and not isinstance(drive_error, asyncio.CancelledError):
            LOG.error(
                "reconnect_benchmark_worker_failed error=%s",
                drive_error.__class__.__name__,
            )
        LOG.error(
            "reconnect_benchmark_sampler_failed error=%s",
            sample_error.__class__.__name__,
        )
        raise sample_error
    if drive_error is not None:
        raise drive_error
    _require_complete_attempts(
        args.attempts,
        ok_count=ok,
        failed_count=failed,
        dequeued=dequeued,
    )

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
