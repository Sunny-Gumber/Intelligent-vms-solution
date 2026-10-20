"""Supervised background tasks.

Every long-running loop in the control plane (retention, storage monitor,
device monitor, session reaper, PTZ watchdog, escalation, notifications) runs
under this supervisor so that a crash restarts the task with bounded backoff
instead of killing the app, repeated crashes mark the component unhealthy
instead of hiding, and shutdown is cooperative and bounded.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.core.backoff import BackoffPolicy
from app.core.timeutil import utcnow
from app.logging_setup import get_logger

log = get_logger("supervisor")

TaskFactory = Callable[[], Awaitable[None]]


@dataclass
class TaskStatus:
    name: str
    running: bool = False
    restarts: int = 0
    consecutive_failures: int = 0
    last_started_at: dt.datetime | None = None
    last_error: str | None = None
    last_error_at: dt.datetime | None = None
    last_heartbeat_at: dt.datetime | None = None
    critical: bool = False

    @property
    def healthy(self) -> bool:
        return self.running and self.consecutive_failures == 0


@dataclass
class SupervisedTask:
    name: str
    factory: TaskFactory
    policy: BackoffPolicy = field(default_factory=lambda: BackoffPolicy(initial_s=1.0, max_s=60.0))
    critical: bool = False
    status: TaskStatus = field(init=False)
    _task: asyncio.Task | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.status = TaskStatus(name=self.name, critical=self.critical)


class Supervisor:
    """Owns the lifecycle of all background tasks for one process."""

    def __init__(self) -> None:
        self._tasks: dict[str, SupervisedTask] = {}
        self._stopping = asyncio.Event()

    def register(self, name: str, factory: TaskFactory, *, critical: bool = False,
                 policy: BackoffPolicy | None = None) -> SupervisedTask:
        if name in self._tasks:
            raise ValueError(f"task {name!r} is already registered")
        task = SupervisedTask(name=name, factory=factory, critical=critical,
                              policy=policy or BackoffPolicy(initial_s=1.0, max_s=60.0))
        self._tasks[name] = task
        return task

    async def start_all(self) -> None:
        self._stopping.clear()
        for task in self._tasks.values():
            if task._task is None or task._task.done():
                task._task = asyncio.create_task(self._run(task), name=f"vms:{task.name}")
        log.info("supervisor.started", tasks=sorted(self._tasks))

    async def _run(self, task: SupervisedTask) -> None:
        attempt = 0
        while not self._stopping.is_set():
            task.status.running = True
            task.status.last_started_at = utcnow()
            try:
                await task.factory()
                task.status.consecutive_failures = 0
                if self._stopping.is_set():
                    break
                attempt += 1
            except asyncio.CancelledError:
                task.status.running = False
                log.info("supervisor.task_cancelled", task=task.name)
                raise
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                task.status.consecutive_failures += 1
                task.status.restarts += 1
                task.status.last_error = f"{type(exc).__name__}: {exc}"[:500]
                task.status.last_error_at = utcnow()
                log.error("supervisor.task_failed", task=task.name, attempt=attempt,
                          critical=task.critical, error=task.status.last_error, exc_info=True)
            finally:
                task.status.running = False

            if self._stopping.is_set():
                break
            if task.policy.max_attempts and attempt >= task.policy.max_attempts:
                log.error("supervisor.task_gave_up", task=task.name, attempts=attempt)
                return
            try:
                await asyncio.wait_for(self._stopping.wait(),
                                       timeout=max(task.policy.delay_for(attempt), 0.01))
                break
            except asyncio.TimeoutError:
                continue

    async def stop_all(self, timeout_s: float = 15.0) -> None:
        self._stopping.set()
        pending = [t._task for t in self._tasks.values() if t._task and not t._task.done()]
        if not pending:
            log.info("supervisor.stopped", tasks=0)
            return
        done, still_running = await asyncio.wait(pending, timeout=timeout_s)
        for task in still_running:
            task.cancel()
        if still_running:
            await asyncio.gather(*still_running, return_exceptions=True)
        log.info("supervisor.stopped", graceful=len(done), cancelled=len(still_running))

    def heartbeat(self, name: str) -> None:
        task = self._tasks.get(name)
        if task:
            task.status.last_heartbeat_at = utcnow()
            task.status.consecutive_failures = 0

    def statuses(self) -> list[TaskStatus]:
        return [t.status for t in self._tasks.values()]

    def is_healthy(self) -> bool:
        return all(t.status.healthy for t in self._tasks.values() if t.critical)


async def interruptible_sleep(stop: asyncio.Event, seconds: float) -> bool:
    """Sleep that wakes immediately on shutdown. Returns True if interrupted."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False
