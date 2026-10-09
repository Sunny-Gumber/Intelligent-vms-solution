"""VMS-FIX-039: unexpected ONVIF unsubscribe failures must not drop a camera.

``camera_loop`` logs ``TargetNotAllowed``, ``OSError``, and ``RuntimeError``
raised by the real ``unsubscribe`` function and keeps pulling. The supervisor
drops a task that has already finished and starts a replacement for that
camera. No database is required.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from pathlib import Path

import pytest

from app.services.network_policy import TargetNotAllowed
from app.services.onvif_events import PullPointSubscription

ROOT = Path(__file__).parents[1]
LOGGER_NAME = "onvif-event-worker"
CAMERA_ID = "cam-fix039"
REPLACEMENT_CAMERA_ID = "cam-fix039-replace"


def _load_worker():
    """Load a fresh ONVIF event-worker module for one regression scenario."""
    path = ROOT / "services" / "onvif-event-worker" / "main.py"
    spec = importlib.util.spec_from_file_location("onvif_event_worker_fix039", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _target(worker, camera_id: str):
    """Build one synthetic ONVIF target. No live device and no credentials."""
    return worker.Target(
        camera_id=camera_id,
        tenant_id="tenant-a",
        site_id="site-a",
        media_node_id="media-local-01",
        username=None,
        password=None,
        event_xaddr="http://192.0.2.40/onvif/event",
    )


async def _unsubscribe_fault(worker, error: Exception) -> dict:
    """Drive ``camera_loop`` through one pull failure and one real unsubscribe."""
    from app.services import onvif_events as onvif_events_mod

    calls = {"pull": 0, "unsub": 0, "create": 0}
    second_pull = asyncio.Event()
    release = asyncio.Event()
    original_soap = onvif_events_mod._soap
    original_wait = asyncio.wait_for

    async def create_pullpoint(*_args, **_kwargs):
        calls["create"] += 1
        return PullPointSubscription(
            address="http://192.0.2.40/onvif/events",
            tenant_id="tenant-a",
            site_id="site-a",
        )

    async def set_synchronization_point(*_args, **_kwargs):
        return None

    async def pull_messages(*_args, **_kwargs):
        calls["pull"] += 1
        if calls["pull"] == 1:
            raise ConnectionError("synthetic pull failure")
        second_pull.set()
        await release.wait()
        return None

    async def failing_soap(*_args, **_kwargs):
        raise error

    async def unsubscribe(subscription, username, password):
        calls["unsub"] += 1
        onvif_events_mod._soap = failing_soap
        try:
            await onvif_events_mod.unsubscribe(subscription, username, password)
        finally:
            onvif_events_mod._soap = original_soap

    async def fast_wait(awaitable, timeout=None):
        if timeout is not None and timeout >= 1:
            timeout = 0.01
        return await original_wait(awaitable, timeout)

    worker.create_pullpoint = create_pullpoint
    worker.set_synchronization_point = set_synchronization_point
    worker.pull_messages = pull_messages
    worker.unsubscribe = unsubscribe
    asyncio.wait_for = fast_wait
    stop = asyncio.Event()
    task = asyncio.create_task(worker.camera_loop(_target(worker, CAMERA_ID), stop))
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3
        while loop.time() < deadline and not second_pull.is_set() and not task.done():
            await asyncio.sleep(0.01)
        task_error = None
        if task.done() and not task.cancelled():
            exc = task.exception()
            task_error = f"{type(exc).__name__}: {exc}" if exc else None
        return {
            "pull": calls["pull"],
            "unsub": calls["unsub"],
            "create": calls["create"],
            "second_pull": second_pull.is_set(),
            "task_done": task.done(),
            "task_error": task_error,
        }
    finally:
        stop.set()
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        asyncio.wait_for = original_wait
        onvif_events_mod._soap = original_soap


async def _replace_finished(worker) -> dict:
    """Finish one camera task and count how many times the supervisor starts it."""
    calls = {"n": 0}
    loads = {"n": 0}

    async def camera_loop(_camera, stop):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("synthetic camera task exit")
        await stop.wait()

    async def load_targets():
        loads["n"] += 1
        return [_target(worker, REPLACEMENT_CAMERA_ID)]

    worker.camera_loop = camera_loop
    worker.load_targets = load_targets
    worker.REFRESH_SECONDS = 0.01
    supervisor = asyncio.create_task(worker.supervisor())
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3
        while loop.time() < deadline:
            if calls["n"] >= 2:
                break
            if loads["n"] >= 3 and calls["n"] < 2:
                break
            await asyncio.sleep(0.01)
        return {"starts": calls["n"], "loads": loads["n"]}
    finally:
        supervisor.cancel()
        await asyncio.gather(supervisor, return_exceptions=True)


@pytest.mark.parametrize(
    ("error_type", "message"),
    [
        (TargetNotAllowed, "synthetic unsubscribe target is outside site policy"),
        (OSError, "synthetic unsubscribe name resolution failed"),
        (RuntimeError, "synthetic unsubscribe programming failure"),
    ],
)
def test_unexpected_unsubscribe_keeps_camera_loop_alive(error_type, message, caplog):
    """Log an unexpected unsubscribe failure and pull again for the same camera."""
    worker = _load_worker()
    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        result = asyncio.run(_unsubscribe_fault(worker, error_type(message)))
    assert result["unsub"] >= 1
    assert result["second_pull"] is True
    assert result["pull"] >= 2
    assert result["create"] >= 2
    assert result["task_done"] is False
    assert result["task_error"] is None
    faults = [
        record
        for record in caplog.records
        if record.name == LOGGER_NAME and record.getMessage().startswith("unsubscribe_failed ")
    ]
    assert faults, caplog.records
    logged = faults[-1]
    assert CAMERA_ID in logged.getMessage()
    assert logged.exc_info is not None
    assert isinstance(logged.exc_info[1], error_type)
    assert message in str(logged.exc_info[1])


def test_supervisor_replaces_finished_camera_task(caplog):
    """Drop a camera task that has already finished and start a replacement."""
    worker = _load_worker()
    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        result = asyncio.run(_replace_finished(worker))
    assert result["starts"] >= 2, result
    exits = [
        record
        for record in caplog.records
        if record.name == LOGGER_NAME and record.getMessage().startswith("camera_task_exited ")
    ]
    assert exits, caplog.records
    logged = exits[-1]
    assert REPLACEMENT_CAMERA_ID in logged.getMessage()
    assert logged.exc_info is not None
    assert isinstance(logged.exc_info[1], RuntimeError)
    assert "synthetic camera task exit" in str(logged.exc_info[1])
