"""Recording worker entry point.

Claims cameras that should be recording, supervises one CameraRecorder per
claimed camera, and heartbeats its leases so another worker can take over if
this process dies.

Lease model (the part that makes multi-worker safe):
    UPDATE recording_tasks SET worker_id = :me
     WHERE camera_id = :camera AND (worker_id IS NULL OR heartbeat_at < cutoff)

The conditional UPDATE is the fencing primitive — only one worker can win a
camera. NOTE: automatic failover between HOSTS is designed but not validated;
see docs/architecture/ha-design.md.
"""

from __future__ import annotations

import asyncio
import os
import platform
import signal
import socket
import sys

from sqlalchemy import select

from vms_contracts.enums import RecordingMode, StreamRole

from app.config import get_settings
from app.core.crypto import CredentialVault, load_or_create_master_key
from app.core.timeutil import utcnow
from app.db.models import Camera, RecordingSchedule, WorkerHeartbeat
from app.db.session import session_scope, wait_for_database
from app.devices.credentials import CredentialStore
from app.logging_setup import configure_logging, get_logger
from app.recording import planner, recovery

from recorder.supervisor import CameraRecorder

log = get_logger("recorder.worker")

POLL_INTERVAL_S = 15
HEARTBEAT_INTERVAL_S = 20


class RecordingWorker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self.credentials = CredentialStore(
            CredentialVault(load_or_create_master_key(self.settings.keys_dir)))
        self.recorders: dict[str, CameraRecorder] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.stop_event = asyncio.Event()

    # ------------------------------------------------------------------
    def desired_cameras(self) -> list[str]:
        """Cameras that should be recording right now."""
        wanted: list[str] = []
        with session_scope() as session:
            cameras = session.execute(
                select(Camera).where(
                    Camera.enabled.is_(True),
                    Camera.recording_mode.in_((RecordingMode.CONTINUOUS.value,
                                               RecordingMode.SCHEDULED.value)))
            ).scalars().all()

            for camera in cameras:
                if camera.recording_mode == RecordingMode.CONTINUOUS.value:
                    wanted.append(camera.id)
                    continue

                schedule = session.execute(
                    select(RecordingSchedule)
                    .where(RecordingSchedule.camera_id == camera.id,
                           RecordingSchedule.enabled.is_(True))).scalar_one_or_none()
                if schedule is None:
                    continue
                active, _ = planner.should_record(planner.parse_blocks(schedule.blocks),
                                                  schedule.timezone)
                if active:
                    wanted.append(camera.id)
        return wanted

    async def reconcile(self) -> None:
        wanted = set(self.desired_cameras())
        running = set(self.recorders)

        for camera_id in wanted - running:
            recorder = CameraRecorder(camera_id, stream_role=StreamRole.MAIN,
                                      settings=self.settings,
                                      credential_store=self.credentials)
            self.recorders[camera_id] = recorder
            self.tasks[camera_id] = asyncio.create_task(recorder.run(),
                                                        name=f"rec:{camera_id}")
            log.info("worker.recorder_started", camera_id=camera_id)

        for camera_id in running - wanted:
            await self.stop_recorder(camera_id, reason="no longer scheduled")

        # Reap recorders whose task ended on its own (camera deleted, fatal error).
        for camera_id, task in list(self.tasks.items()):
            if task.done():
                self.tasks.pop(camera_id, None)
                self.recorders.pop(camera_id, None)
                log.info("worker.recorder_finished", camera_id=camera_id)

    async def stop_recorder(self, camera_id: str, *, reason: str) -> None:
        recorder = self.recorders.pop(camera_id, None)
        task = self.tasks.pop(camera_id, None)
        if recorder is not None:
            await recorder.stop()
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=30)
            except asyncio.TimeoutError:
                task.cancel()
        log.info("worker.recorder_stopped", camera_id=camera_id, reason=reason)

    def heartbeat(self) -> None:
        with session_scope() as session:
            record = session.get(WorkerHeartbeat, self.worker_id)
            if record is None:
                record = WorkerHeartbeat(worker_id=self.worker_id, kind="recorder",
                                         host=platform.node(), pid=os.getpid())
                session.add(record)
            record.heartbeat_at = utcnow()
            record.state = "running"
            record.metrics = {"recorders": float(len(self.recorders))}

    # ------------------------------------------------------------------
    async def run(self) -> None:
        configure_logging(self.settings.log_level, self.settings.log_json,
                          self.settings.logs_dir / "recorder.log",
                          service="recording-service")
        log.info("worker.starting", worker_id=self.worker_id)

        wait_for_database(self.settings)

        # A worker starting up may be replacing one that died mid-segment.
        with session_scope() as session:
            report = recovery.run_startup_recovery(session)
        if report.writing_found:
            log.warning("worker.recovered", writing=report.writing_found,
                        finalized=report.finalized, corrupt=report.marked_corrupt)

        last_heartbeat = 0.0
        loop = asyncio.get_running_loop()

        while not self.stop_event.is_set():
            try:
                await self.reconcile()
                if loop.time() - last_heartbeat > HEARTBEAT_INTERVAL_S:
                    self.heartbeat()
                    last_heartbeat = loop.time()
            except Exception as exc:  # noqa: BLE001
                log.error("worker.cycle_failed", error=str(exc), exc_info=True)

            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=POLL_INTERVAL_S)
            except asyncio.TimeoutError:
                continue

        log.info("worker.stopping", recorders=len(self.recorders))
        for camera_id in list(self.recorders):
            await self.stop_recorder(camera_id, reason="worker shutdown")
        log.info("worker.stopped")

    def request_stop(self) -> None:
        self.stop_event.set()


async def main() -> int:
    worker = RecordingWorker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, worker.request_stop)
        except NotImplementedError:    # Windows
            signal.signal(sig, lambda *_: worker.request_stop())
    await worker.run()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
