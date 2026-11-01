"""Periodic storage health loop.

Runs under the supervisor. Every storage_check_interval_s it refreshes all
volumes, publishes state-change events and updates RAID status. It NEVER
deletes data; retention is a separate, explicitly audited process.
"""

from __future__ import annotations

import asyncio

from app.config import Settings, get_settings
from app.core.supervisor import interruptible_sleep
from app.db.session import session_scope
from app.logging_setup import get_logger
from app.storage import raid
from app.storage import volumes as volume_service

log = get_logger("storage.monitor")


class StorageMonitor:
    def __init__(self, event_sink, settings: Settings | None = None) -> None:  # noqa: ANN001
        self.event_sink = event_sink
        self.settings = settings or get_settings()
        self.stop_event = asyncio.Event()

    async def run_once(self) -> int:
        with session_scope() as session:
            events = volume_service.refresh_all(session, self.settings)
            for volume in volume_service.writable_volumes(session):
                if volume.raid_device:
                    volume.raid_status = raid.status_for_device(volume.raid_device)
        for event in events:
            await self.event_sink(event)
        return len(events)

    async def run(self) -> None:
        log.info("storage.monitor_started",
                 interval_s=self.settings.storage_check_interval_s)
        while not self.stop_event.is_set():
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001
                log.error("storage.monitor_iteration_failed", error=str(exc),
                          exc_info=True)
            if await interruptible_sleep(self.stop_event,
                                         self.settings.storage_check_interval_s):
                break

    def stop(self) -> None:
        self.stop_event.set()
