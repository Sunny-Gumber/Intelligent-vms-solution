#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import stat
import tempfile
import time
from collections.abc import Awaitable
from pathlib import Path
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


def _as_failed_run(error: BaseException, *, source: str) -> BaseException:
    """Return the failure that must fail the process.

    ``SystemExit(0)``, ``SystemExit()``, and ``SystemExit(False)`` share a
    success status. Letting one escape ends the CLI with status 0, and a JSON
    file left from an earlier run then looks like this run succeeded.

    Args:
        error: Failure from setup, a worker, the sampler, teardown, or
            publishing. This run's own shutdown cancellation is not passed
            here.
        source: Short origin used in the rewritten message. ``worker`` keeps
            the historical wording.

    Returns:
        A ``RuntimeError`` when ``error`` is a successful ``SystemExit``.
        Otherwise the original failure, including ``CancelledError``,
        ``KeyboardInterrupt``, and a non-zero ``SystemExit``.
    """
    if isinstance(error, SystemExit) and error.code in (None, 0):
        return RuntimeError(f"reconnect benchmark {source} raised SystemExit(0)")
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
        BaseException: The original failure for every other type, including
            ``KeyboardInterrupt``, ``CancelledError``, and a non-zero
            ``SystemExit``.
    """
    published = _as_failed_run(error, source=source)
    if published is not error:
        raise published from error
    raise error


def _completed_task_error(task: asyncio.Task[object]) -> BaseException | None:
    """Return a failure stored on a finished task, including a returned one.

    A nested task that returns ``SystemExit`` has contained it. ``gather``
    then yields that instance as a normal result, so callers must not treat
    only ``task.exception()`` as a failure.

    Args:
        task: Finished worker, producer, or sampler task.

    Returns:
        The task's failure, or None when it finished without one.

    Raises:
        InvalidStateError: If ``task`` is not done.
    """
    if task.cancelled():
        return asyncio.CancelledError("reconnect benchmark worker cancelled")
    error = task.exception()
    if error is not None:
        return error
    outcome = task.result()
    if isinstance(outcome, BaseException):
        return outcome
    return None


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
        Exception: Unexpected worker, sampler, setup, teardown, or result
            failures propagate so benchmark evidence is not produced.
            ``OSError`` and ``asyncio.TimeoutError`` inside one attempt stay
            measurements.
        asyncio.CancelledError: Propagates when a worker or the sampler raises
            it on its own, and when the whole run is cancelled externally.
            Cancellation this run requested while shutting down does not.
        KeyboardInterrupt: Propagates when a worker or the sampler raises it.
            This is not a measured attempt and not a successful result.
        RuntimeError: A successful ``SystemExit`` from a worker, the sampler,
            setup, teardown, or result building is rewritten to this so the
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

    async def enqueue(count: int, worker_count: int) -> BaseException | None:
        try:
            for index in range(count):
                await queue.put(index)
            for _ in range(worker_count):
                await queue.put(None)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            # SystemExit and KeyboardInterrupt abort the loop if they escape a
            # nested task. Hold them so drive() can fail the run.
            return exc
        return None

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
        failure_source = "worker"
        teardown_error: BaseException | None = None
        try:
            while work:
                done, _pending = await asyncio.wait(work | {watch_task}, return_when=asyncio.FIRST_COMPLETED)
                if worker_error is not None:
                    break
                for task in done:
                    if task is watch_task or task not in work:
                        continue
                    work.discard(task)
                    failure = _completed_task_error(task)
                    if failure is None or worker_error is not None:
                        continue
                    worker_error = failure
                    if task is enqueue_task and not isinstance(failure, asyncio.CancelledError):
                        failure_source = "setup"
                    break
                if worker_error is not None:
                    break
        finally:
            shutdown_requested = True
            try:
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
                        if task is enqueue_task and not isinstance(outcome, asyncio.CancelledError):
                            failure_source = "setup"
                        continue
                    if outcome is not worker_error and not isinstance(outcome, asyncio.CancelledError):
                        LOG.error(
                            "reconnect_benchmark_worker_cleanup_error error=%s",
                            outcome.__class__.__name__,
                        )
            except BaseException as exc:
                # A teardown SystemExit(0) must not replace a worker failure or
                # exit the process with status 0.
                teardown_error = exc
        if isinstance(teardown_error, KeyboardInterrupt):
            _raise_failed(teardown_error, source="teardown")
        if worker_error is not None:
            if teardown_error is not None and not isinstance(teardown_error, asyncio.CancelledError):
                LOG.error(
                    "reconnect_benchmark_teardown_failed error=%s",
                    teardown_error.__class__.__name__,
                )
            if failure_source == "worker":
                LOG.error(
                    "reconnect_benchmark_worker_failed error=%s",
                    worker_error.__class__.__name__,
                )
            else:
                LOG.error(
                    "reconnect_benchmark_setup_failed error=%s",
                    worker_error.__class__.__name__,
                )
            _raise_failed(worker_error, source=failure_source)
        if teardown_error is not None:
            LOG.error(
                "reconnect_benchmark_teardown_failed error=%s",
                teardown_error.__class__.__name__,
            )
            _raise_failed(teardown_error, source="teardown")
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

    try:
        sampler = SystemSampler()
    except BaseException as exc:
        _raise_failed(exc, source="setup")
    samples = []
    stop = asyncio.Event()

    async def sample_loop() -> BaseException | None:
        try:
            while not stop.is_set():
                samples.append(sampler.sample())
                try:
                    await asyncio.wait_for(stop.wait(), timeout=max(0.1, args.sample_interval))
                except asyncio.TimeoutError:
                    # Expected sample cadence timeout while the benchmark is active.
                    continue
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            # Contain SystemExit and KeyboardInterrupt inside this task. A
            # nested task that lets either escape aborts the loop or exits 0
            # before the parent can fail the run.
            return exc
        return None

    async def drive_measured() -> BaseException | None:
        # A nested task that lets KeyboardInterrupt or SystemExit escape will
        # abort the loop or exit 0 before the parent can fail the run cleanly.
        try:
            await drive(args.attempts, True)
        except (KeyboardInterrupt, SystemExit) as exc:
            return exc
        return None

    try:
        # Taken before tasks exist so a clock failure cannot leak them.
        started_at = utc_iso()
    except BaseException as exc:
        _raise_failed(exc, source="setup")
    sample_task = asyncio.create_task(sample_loop())
    drive_task = asyncio.create_task(drive_measured())
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
    sampler_cancel_requested = False
    if not sample_task.done() and (external_error is not None or drive_error is not None):
        sampler_cancel_requested = True
        sample_task.cancel()
    sample_error: BaseException | None = None
    try:
        sample_outcome = await _await_releasing_cancellation(sample_task)
    except BaseException as exc:
        sample_error = exc
    else:
        if isinstance(sample_outcome, BaseException):
            sample_error = sample_outcome
    sampler_failed = sample_error is not None and not (
        sampler_cancel_requested and isinstance(sample_error, asyncio.CancelledError)
    )
    duration = time.perf_counter() - started
    if external_error is not None:
        if sampler_failed:
            LOG.error(
                "reconnect_benchmark_sampler_failed error=%s",
                sample_error.__class__.__name__,
            )
        _raise_failed(external_error, source="run")
    if sampler_failed:
        if drive_error is not None and not isinstance(drive_error, asyncio.CancelledError):
            LOG.error(
                "reconnect_benchmark_worker_failed error=%s",
                drive_error.__class__.__name__,
            )
        LOG.error(
            "reconnect_benchmark_sampler_failed error=%s",
            sample_error.__class__.__name__,
        )
        _raise_failed(sample_error, source="sampler")
    if drive_error is not None:
        _raise_failed(drive_error, source="worker")
    _require_complete_attempts(
        args.attempts,
        ok_count=ok,
        failed_count=failed,
        dequeued=dequeued,
    )

    try:
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
    except BaseException as exc:
        _raise_failed(exc, source="result")


def _legacy_partial_path(json_path: Path) -> Path:
    """Return the old shared staging name, which this run does not own.

    Args:
        json_path: Final ``--output-json`` path.

    Returns:
        The sibling path ``<name>.partial``.
    """
    return json_path.with_name(f"{json_path.name}.partial")


def _is_real_directory(path: Path) -> bool:
    """Report whether ``path`` is a directory and not a symlink.

    Args:
        path: Evidence path to inspect. A missing path is not a directory.

    Returns:
        True when ``lstat`` shows a directory. Symlinks are False even when
        their target is a directory, because unlink and ``os.replace`` replace
        the link itself.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(info.st_mode)


def _refuse_unsafe_evidence_paths(json_path: Path) -> None:
    """Fail closed before any evidence file is removed.

    Args:
        json_path: Configured ``--output-json`` path.

    Returns:
        None when neither the destination nor the legacy staging name is a
        real directory.

    Raises:
        RuntimeError: When either path is a directory. Nothing is deleted.
    """
    if _is_real_directory(json_path):
        raise RuntimeError(f"reconnect benchmark output JSON is a directory: {json_path}")
    legacy = _legacy_partial_path(json_path)
    if _is_real_directory(legacy):
        raise RuntimeError(f"reconnect benchmark staging path is a directory: {legacy}")


def _discard_stale_destination(json_path: Path) -> None:
    """Remove the destination once, before this run starts.

    Call this only after ``_refuse_unsafe_evidence_paths``. It removes a
    regular file or a symlink at ``--output-json`` and does not follow a
    symlink. It does not remove the legacy ``<name>.partial`` path, because
    that name is not owned by this run.

    A failed publish must not call this again. Another process may have
    replaced the destination after this process started, and deleting it
    would drop a run that already exited 0. A later invocation may clear the
    path at its own startup; that is a new run, not cleanup of the run that
    already published.

    Args:
        json_path: Configured ``--output-json`` path.

    Returns:
        None after the destination is absent or was already missing.

    Raises:
        OSError: If a present file cannot be removed.
    """
    json_path.unlink(missing_ok=True)


def _create_staging_file(json_path: Path) -> Path:
    """Create an exclusive temporary JSON file beside the destination.

    ``tempfile.mkstemp`` uses ``O_EXCL``, so two runs of the same
    ``--output-json`` do not share one staging file. The name is
    ``<output>.<random>.partial`` in the destination directory.

    Args:
        json_path: Final ``--output-json`` path.

    Returns:
        The new file path. The caller owns it until ``os.replace`` renames it
        or the caller unlinks it.

    Raises:
        OSError: If the directory cannot be created or the file cannot be
            created exclusively.
    """
    json_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        dir=json_path.parent,
        prefix=f"{json_path.name}.",
        suffix=".partial",
    )
    os.close(descriptor)
    return Path(name)


def _unlink_owned_staging(staging: Path) -> None:
    """Remove the temporary file this run created.

    Args:
        staging: Path returned by ``_create_staging_file`` for this run.

    Returns:
        None after that path is absent. The destination is not removed, even
        when ``os.replace`` has already moved this file onto it: after a
        successful replace the temporary name no longer exists.

    Raises:
        OSError: If the temporary file exists and cannot be removed.
    """
    staging.unlink(missing_ok=True)


def _publish_benchmark_result(result: dict, json_path: Path, csv_path: str | None) -> None:
    """Publish JSON only after a successful run, by atomic replace.

    The JSON is written to a private temporary file, then ``os.replace`` moves
    that file onto ``--output-json``. The CSV row is appended only after that
    replace returns. A failure before the replace unlinks the temporary file
    and leaves the destination untouched, so a sibling run's published JSON
    stays in place. A CSV failure after the replace also leaves the destination
    in place.

    Args:
        result: Completed benchmark result dictionary.
        json_path: Destination JSON evidence path.
        csv_path: Optional append-only CSV summary path.

    Returns:
        None after the destination JSON is this run's evidence. When
        ``csv_path`` is set, the CSV row is appended after the replace.

    Raises:
        Exception: Validation or filesystem failures from ``write_result`` or
            ``os.replace``. The temporary file is removed when the replace has
            not committed it.
    """
    staging = _create_staging_file(json_path)
    try:
        write_result(result, json_path=str(staging), csv_path=None)
        os.replace(staging, json_path)
    except BaseException:
        _unlink_owned_staging(staging)
        raise
    if csv_path:
        write_result(result, json_path=str(json_path), csv_path=csv_path)


def main():
    """Parse CLI arguments, execute the reconnect benchmark, and write results.

    Evidence policy: directory checks run first and delete nothing. A real
    directory at ``--output-json`` or at the legacy ``<name>.partial`` path
    raises ``RuntimeError`` and leaves every existing file in place. A symlink
    at ``<name>.partial`` is not removed and its target is not modified.

    After those checks, a regular file or symlink at ``--output-json`` is
    removed once, before the benchmark, so this process's own failure cannot
    be read as a previous run. That removal is not repeated. Each run then
    writes JSON to its own ``<output>.<random>.partial`` file in the same
    directory and ``os.replace`` moves it onto the destination. Cleanup of a
    failed replace unlinks only that temporary file. It does not unlink the
    destination, so a sibling process that published after this process
    started keeps the file from the run that exited 0. A later invocation of
    the same path may clear it at that invocation's startup; that is a new
    run. The CSV row is appended only after ``os.replace`` returns, and the
    CSV is not deleted when a later run fails. ``--help`` and argument errors
    still exit from ``argparse`` before any file is removed.

    Args:
        None. Arguments are read from the process command line.

    Returns:
        None after result files and the summary line are written.

    Raises:
        RuntimeError: A successful ``SystemExit`` from the run or from
            publishing is rewritten to this so the process cannot exit 0.
            Also raised when the destination or the legacy staging path is a
            directory.
        Exception: Benchmark or output failures propagate. A failed publish
            does not remove JSON that another run has already replaced into
            place.
        KeyboardInterrupt: Propagates when the run is interrupted.
        asyncio.CancelledError: Propagates when the run task is cancelled.
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
    json_path = Path(args.output_json)
    _refuse_unsafe_evidence_paths(json_path)
    _discard_stale_destination(json_path)
    try:
        result = asyncio.run(run(args))
        _publish_benchmark_result(result, json_path, args.output_csv)
    except BaseException as exc:
        _raise_failed(exc, source="publish")
    print(
        f"ok={result['result']['operations_ok']} "
        f"failed={result['result']['operations_failed']} "
        f"rate={result['result']['throughput_ops_s']:.2f}/s"
    )


if __name__ == "__main__":
    main()
