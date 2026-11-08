"""Camera online/offline monitoring.

A circuit breaker per camera means a dead device is probed less and less often
instead of consuming a worker slot every cycle. State changes emit events
exactly ONCE per transition — not on every failed poll, which would flood the
alarm list.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy import select
from vms_contracts.enums import CameraState, EventSeverity, EventType

from app.config import Settings, get_settings
from app.core.circuit import BreakerRegistry, CircuitOpenError
from app.core.supervisor import interruptible_sleep
from app.core.timeutil import utcnow
from app.db.models import Camera
from app.db.session import session_scope
from app.logging_setup import get_logger

log = get_logger("devices.monitor")

OFFLINE_THRESHOLD = 2


@dataclass
class MonitorResult:
    checked: int = 0
    online: int = 0
    offline: int = 0
    transitions: int = 0


class DeviceMonitor:
    def __init__(self, prober, event_sink, settings: Settings | None = None,  # noqa: ANN001
                 *, interval_s: int | None = None, concurrency: int = 8) -> None:
        self.prober = prober
        self.event_sink = event_sink
        self.settings = settings or get_settings()
        self.interval_s = interval_s or self.settings.device_monitor_interval_s
        self.breakers = BreakerRegistry(failure_threshold=3,
                                        reset_timeout_s=self.interval_s * 3)
        self.semaphore = asyncio.Semaphore(concurrency)
        self.stop_event = asyncio.Event()

    async def check_camera(self, camera_id: str) -> tuple[CameraState, list[dict]]:
        breaker = self.breakers.get(camera_id)
        events: list[dict] = []

        with session_scope() as session:
            camera = session.get(Camera, camera_id)
            if camera is None or not camera.enabled:
                return CameraState.DISABLED, events
            previous = CameraState(camera.state)
            name, site_id = camera.name, camera.site_id

            try:
                outcome = await breaker.call(lambda: self.prober.probe(camera))
            except CircuitOpenError:
                return previous, events
            except Exception as exc:  # noqa: BLE001
                log.warning("device.monitor_probe_error", camera_id=camera_id,
                            error=str(exc))
                outcome = None

            if outcome is None or not outcome.ok:
                camera.consecutive_failures += 1
                detail = outcome.error if outcome else "probe raised an exception"
                new_state = (CameraState.UNAUTHORIZED
                             if outcome and outcome.state is CameraState.UNAUTHORIZED
                             else CameraState.OFFLINE)
                if camera.consecutive_failures >= OFFLINE_THRESHOLD and previous != new_state:
                    camera.state = new_state.value
                    camera.state_detail = detail
                    events.append({
                        "type": (EventType.CAMERA_UNAUTHORIZED
                                 if new_state is CameraState.UNAUTHORIZED
                                 else EventType.CAMERA_OFFLINE),
                        "severity": EventSeverity.HIGH, "camera_id": camera_id,
                        "site_id": site_id,
                        "payload": {"camera": name, "detail": detail}})
                return CameraState(camera.state), events

            camera.consecutive_failures = 0
            camera.last_seen_at = utcnow()
            if previous is not CameraState.ONLINE:
                camera.state = CameraState.ONLINE.value
                camera.state_detail = None
                events.append({
                    "type": EventType.CAMERA_ONLINE, "severity": EventSeverity.INFO,
                    "camera_id": camera_id, "site_id": site_id,
                    "payload": {"camera": name, "previous_state": previous.value}})
            return CameraState.ONLINE, events

    async def run_once(self) -> MonitorResult:
        result = MonitorResult()
        with session_scope() as session:
            camera_ids = list(session.execute(
                select(Camera.id).where(Camera.enabled.is_(True))).scalars().all())

        async def check(camera_id: str) -> None:
            async with self.semaphore:
                state, events = await self.check_camera(camera_id)
                result.checked += 1
                if state is CameraState.ONLINE:
                    result.online += 1
                else:
                    result.offline += 1
                result.transitions += len(events)
                for event in events:
                    await self.event_sink(event)

        await asyncio.gather(*(check(cid) for cid in camera_ids))
        return result

    async def run(self) -> None:
        log.info("devices.monitor_started", interval_s=self.interval_s)
        while not self.stop_event.is_set():
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001
                log.error("devices.monitor_failed", error=str(exc), exc_info=True)
            if await interruptible_sleep(self.stop_event, self.interval_s):
                break

    def stop(self) -> None:
        self.stop_event.set()
