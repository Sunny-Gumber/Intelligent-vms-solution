#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import logging
import os  # CLI tests patch os.replace here; atomic publish uses the same module.
import stat
import time
from collections.abc import Awaitable
from pathlib import Path
from typing import TypeVar

import phase8_atomic_publish as atomic_publish
from phase8_benchmark_common import SystemSampler, build_result, utc_iso, write_result


LOG = logging.getLogger(__name__)
_T = TypeVar("_T")
# How long shutdown waits for a task that was cancelled. A workload that
# ignores CancelledError fails the run when this elapses. Cooperative
# attempt() calls return as soon as they are cancelled.
_SHUTDOWN_JOIN_SECONDS = 5.0


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


def _contained_task_outcome(task: asyncio.Task[object]) -> object:
    """Return a finished task's result, including a stored exception.

    ``asyncio.gather(..., return_exceptions=True)`` yields exception instances
    instead of raising them. Shutdown uses ``asyncio.wait`` and then reads each
    task the same way. A cancelled task becomes ``CancelledError`` so callers
    can ignore the cancellation they requested.

    Args:
        task: Finished worker, producer, or sampler task.

    Returns:
        The task's return value, or the exception it stored.

    Raises:
        InvalidStateError: If ``task`` is not done.
        asyncio.CancelledError: Not raised. A cancelled task returns an instance.
    """
    if task.cancelled():
        try:
            task.exception()
        except asyncio.CancelledError as exc:
            # task.exception() raises the CancelledError the coroutine stored,
            # including its message. A fresh CancelledError would drop that text.
            return exc
        return asyncio.CancelledError("reconnect benchmark worker cancelled")
    error = task.exception()
    if error is not None:
        return error
    return task.result()


async def _await_finished_task(task: asyncio.Task[object]) -> object:
    """Wait until ``task`` finishes, failing when it ignores cancellation.

    Args:
        task: Drive or sampler task. A task that is already done is not waited
            on again.

    Returns:
        The task's return value. A returned ``BaseException`` is a value, not
        a raised error. ``drive_measured`` uses that for ``KeyboardInterrupt``
        and ``SystemExit``.

    Raises:
        RuntimeError: ``task`` was still pending after ``_SHUTDOWN_JOIN_SECONDS``.
        BaseException: The exception stored on ``task``, including
            ``CancelledError`` when the task itself was cancelled.
    """
    if not task.done():
        _done, pending = await _await_releasing_cancellation(
            asyncio.wait({task}, timeout=_SHUTDOWN_JOIN_SECONDS)
        )
        if pending:
            LOG.error("reconnect benchmark shutdown timed out; a workload ignored cancellation")
            raise RuntimeError(
                "reconnect benchmark shutdown timed out; a workload ignored cancellation"
            )
    if task.cancelled():
        # Re-raise the stored CancelledError so its message is preserved.
        task.exception()
    return task.result()


def _run_benchmark(awaitable: Awaitable[_T]) -> _T:
    """Run ``awaitable`` and abandon tasks that ignore cancellation.

    ``asyncio.run`` waits until every task finishes during loop shutdown. A
    workload that swallows ``CancelledError`` would keep that wait open. This
    runner closes the loop after ``_SHUTDOWN_JOIN_SECONDS`` and lets the
    original benchmark error propagate.

    Args:
        awaitable: Benchmark coroutine, normally ``run(args)``.

    Returns:
        The awaitable's result when it finishes.

    Raises:
        BaseException: The awaitable's error, after leftover tasks have been
            cancelled and either finished or abandoned.
    """
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        task = loop.create_task(awaitable)
        try:
            return loop.run_until_complete(task)
        finally:
            _abandon_tasks_that_ignore_cancellation(loop)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _abandon_tasks_that_ignore_cancellation(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel leftover tasks and stop waiting after the shutdown bound.

    Args:
        loop: Loop that just finished the benchmark task.

    Returns:
        None. Tasks still pending after the bound are left for ``loop.close``.
        Closing the loop does not wait for them.

    Raises:
        None. Failures while cancelling are logged. The benchmark error that
        is already in flight stays the error the caller sees.
    """
    pending = [item for item in asyncio.all_tasks(loop) if not item.done()]
    for item in pending:
        item.cancel()
    if not pending:
        return
    try:
        loop.run_until_complete(asyncio.wait(set(pending), timeout=_SHUTDOWN_JOIN_SECONDS))
    except Exception:
        LOG.exception("reconnect benchmark failed while abandoning tasks")
    leftover = [item for item in pending if not item.done()]
    if leftover:
        LOG.error(
            "reconnect benchmark abandoned %s task(s) that ignored cancellation",
            len(leftover),
        )


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
            match the attempts that were dequeued or configured, and when a
            cancelled worker or sampler is still running after
            ``_SHUTDOWN_JOIN_SECONDS`` because it ignored cancellation.
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
            tasks = (*workers, enqueue_task, watch_task)
            try:
                for task in tasks:
                    if not task.done():
                        requested_cancel.add(task)
                        task.cancel()
                _done, pending = await _await_releasing_cancellation(
                    asyncio.wait(set(tasks), timeout=_SHUTDOWN_JOIN_SECONDS)
                )
                if pending:
                    LOG.error(
                        "reconnect benchmark shutdown timed out; a workload ignored cancellation"
                    )
                    raise RuntimeError(
                        "reconnect benchmark shutdown timed out; a workload ignored cancellation"
                    )
                _drain_queue(queue)
                for task in tasks:
                    outcome = _contained_task_outcome(task)
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
                # exit the process with status 0. A shutdown timeout is a
                # teardown failure as well: the run must not publish.
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
        drive_outcome = await _await_finished_task(drive_task)
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
        sample_outcome = await _await_finished_task(sample_task)
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


def _publish_benchmark_result(result: dict, json_path: Path, csv_path: str | None) -> None:
    """Publish JSON and the optional CSV through the shared atomic helper.

    Each file is written to a private ``tempfile.mkstemp`` file, fsynced,
    replaced, and then the directory is fsynced. The JSON file is not opened
    again after its replace, so the CSV publish cannot truncate it. A failure
    unlinks only the temporary file that replace has not yet committed. An
    unlink error does not replace the original publish error.

    A ``SIGKILL`` during the temporary write skips that unlink. The leftover
    ``<output>.<random>.partial`` file stays in the destination directory.
    This function does not delete unrelated files to clean that up.

    Args:
        result: Completed benchmark result dictionary.
        json_path: Destination JSON evidence path.
        csv_path: Optional CSV summary path.

    Returns:
        None after the destination JSON is this run's evidence. When
        ``csv_path`` is set, that file is replaced after the JSON replace.

    Raises:
        Exception: Validation or filesystem failures from ``write_result`` or
            the atomic replace. The temporary file is removed when the replace
            has not committed it. The destination JSON is left in place when
            the failure happens after that replace.
    """
    atomic_publish.publish_benchmark_evidence(
        result,
        json_path,
        csv_path,
        write_result=write_result,
    )


def _require_positive_attempts(value: str) -> int:
    """Reject a reconnect workload that would publish zero attempts.

    Args:
        value: Text from ``--attempts``.

    Returns:
        The attempt count when it is an integer greater than or equal to 1.

    Raises:
        argparse.ArgumentTypeError: The value is not an integer or is below 1.
            ``argparse`` exits before any evidence file is removed.
    """
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("attempts must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("attempts must be >= 1")
    return parsed


def main():
    """Parse CLI arguments, execute the reconnect benchmark, and write results.

    Evidence policy: directory checks run first and delete nothing. A real
    directory at ``--output-json`` or at the legacy ``<name>.partial`` path
    raises ``RuntimeError`` and leaves every existing file in place. A symlink
    at ``<name>.partial`` is not removed and its target is not modified.

    After those checks, a regular file or symlink at ``--output-json`` is
    removed once, before the benchmark, so this process's own failure cannot
    be read as a previous run. That removal is the latest-run rule: a later
    invocation clears the path at its own startup, and a failure does not put
    the previous file back. Two overlapping invocations of the same path are
    not a way to keep every process that exits 0. The removal is not repeated
    after the run starts.

    Each output is then published by ``phase8_atomic_publish``: a private
    ``<output>.<random>.partial`` file created with ``tempfile.mkstemp`` in
    mode ``0600``, an ``fsync`` of that file, ``os.replace``, and an ``fsync``
    of the directory. Nothing is written to a file after its replace. Cleanup
    of a failed replace unlinks only that temporary file and keeps the original
    exception if the unlink fails. It does not unlink the destination, so a
    sibling process that published after this process started keeps the file
    from the run that exited 0. The CSV is a separate replace, not a rewrite
    of the JSON, and it is not deleted when a later run fails.

    ``SIGKILL`` while a temporary file is still open leaves that file behind.
    The next run does not delete arbitrary files to remove it. ``--help`` and
    argument errors, including ``--attempts`` below 1, still exit from
    ``argparse`` before any file is removed. Shutdown of a workload that
    ignores cancellation is bounded by ``_SHUTDOWN_JOIN_SECONDS`` and fails
    the run.

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
    parser.add_argument("--attempts", type=_require_positive_attempts, default=10000)
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
        result = _run_benchmark(run(args))
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
