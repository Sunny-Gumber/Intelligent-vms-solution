"""PTZ command execution — capability gated, permission gated, time bounded."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session
from vms_contracts.dto import PtzCommand
from vms_contracts.enums import PtzMoveKind

from app.config import Settings, get_settings
from app.core.errors import NotFoundError, NotImplementedFeatureError, ValidationError
from app.db.models import Camera, PtzPreset
from app.devices.base import Capabilities, CapabilityNotSupported, DeviceAdapter, DeviceError
from app.logging_setup import get_logger
from app.ptz.arbiter import PtzArbiter

log = get_logger("ptz.service")


@dataclass
class PtzOutcome:
    accepted: bool
    kind: PtzMoveKind
    duration_s: float | None = None
    detail: str | None = None


class PtzService:
    def __init__(self, adapter_factory, arbiter: PtzArbiter | None = None,  # noqa: ANN001
                 settings: Settings | None = None) -> None:
        self.adapter_factory = adapter_factory
        self.settings = settings or get_settings()
        self.arbiter = arbiter or PtzArbiter(self.settings)

    @staticmethod
    def _require_capability(camera: Camera, kind: PtzMoveKind) -> Capabilities:
        caps = Capabilities.from_dict(camera.capabilities)
        required = {
            PtzMoveKind.CONTINUOUS: caps.ptz_continuous,
            PtzMoveKind.RELATIVE: caps.ptz_relative,
            PtzMoveKind.ABSOLUTE: caps.ptz_absolute,
            PtzMoveKind.PRESET: caps.ptz_presets,
            PtzMoveKind.STOP: caps.any_ptz,
        }[kind]
        if not required:
            raise NotImplementedFeatureError(
                f"This camera has not demonstrated {kind.value} PTZ support. "
                "Re-run camera capability detection if the device was upgraded.",
                code="ptz_capability_unavailable", extra={"capability": kind.value})
        return caps

    async def execute(self, session: Session, camera: Camera, command: PtzCommand, *,
                      owner_id: str, owner_name: str | None = None,
                      priority: int = 0) -> PtzOutcome:
        self._require_capability(camera, command.kind)

        if command.kind is PtzMoveKind.STOP:
            adapter = await self._adapter(session, camera)
            try:
                await adapter.ptz_stop()
            finally:
                await adapter.close()
            self.arbiter.clear_moving(session, camera.id)
            self.arbiter.release(session, camera.id, owner_id)
            return PtzOutcome(True, PtzMoveKind.STOP,
                              detail="stopped and control released")

        self.arbiter.acquire(session, camera.id, owner_id, owner_name=owner_name,
                             priority=priority)

        duration = min(command.duration_s or self.settings.ptz_max_move_seconds,
                       self.settings.ptz_max_move_seconds)
        adapter = await self._adapter(session, camera)
        try:
            if command.kind is PtzMoveKind.CONTINUOUS:
                await adapter.ptz_continuous(command.pan, command.tilt, command.zoom,
                                             command.speed, duration)
                self.arbiter.mark_moving(session, camera.id, owner_id, duration)
            elif command.kind is PtzMoveKind.RELATIVE:
                await adapter.ptz_relative(command.pan, command.tilt, command.zoom,
                                           command.speed)
            elif command.kind is PtzMoveKind.ABSOLUTE:
                await adapter.ptz_absolute(command.pan, command.tilt, command.zoom,
                                           command.speed)
            elif command.kind is PtzMoveKind.PRESET:
                if not command.preset_id:
                    raise ValidationError("preset_id is required", code="preset_required")
                preset = session.get(PtzPreset, command.preset_id)
                if preset is None or preset.camera_id != camera.id:
                    raise NotFoundError("Preset not found", code="preset_not_found")
                await adapter.ptz_goto_preset(preset.token or preset.name, command.speed)
        except CapabilityNotSupported as exc:
            raise NotImplementedFeatureError(str(exc),
                                             code="ptz_capability_unavailable") from exc
        finally:
            await adapter.close()

        log.info("ptz.command", camera_id=camera.id, kind=command.kind.value,
                 owner=owner_id, duration_s=duration)
        return PtzOutcome(True, command.kind, duration_s=duration)

    async def save_preset(self, session: Session, camera: Camera, name: str,
                          created_by: str) -> PtzPreset:
        self._require_capability(camera, PtzMoveKind.PRESET)
        adapter = await self._adapter(session, camera)
        try:
            token = await adapter.ptz_save_preset(name)
        except CapabilityNotSupported as exc:
            raise NotImplementedFeatureError(str(exc),
                                             code="ptz_capability_unavailable") from exc
        finally:
            await adapter.close()

        existing = session.execute(
            select(PtzPreset).where(PtzPreset.camera_id == camera.id,
                                    PtzPreset.name == name)).scalar_one_or_none()
        if existing:
            existing.token = token
            return existing
        preset = PtzPreset(camera_id=camera.id, name=name, token=token,
                           created_by=created_by)
        session.add(preset)
        session.flush()
        return preset

    async def delete_preset(self, session: Session, camera: Camera,
                            preset_id: str) -> None:
        preset = session.get(PtzPreset, preset_id)
        if preset is None or preset.camera_id != camera.id:
            raise NotFoundError("Preset not found", code="preset_not_found")
        adapter = await self._adapter(session, camera)
        try:
            if preset.token:
                await adapter.ptz_remove_preset(preset.token)
        except (CapabilityNotSupported, DeviceError) as exc:
            log.warning("ptz.preset_remove_device_failed", camera_id=camera.id,
                        error=str(exc))
        finally:
            await adapter.close()
        session.delete(preset)

    async def stop_cameras(self, session: Session, camera_ids: list[str]) -> int:
        """Watchdog path: force-stop a list of cameras."""
        stopped = 0
        for camera_id in camera_ids:
            camera = session.get(Camera, camera_id)
            if camera is None:
                self.arbiter.release(session, camera_id)
                continue
            try:
                adapter = await self._adapter(session, camera)
            except Exception as exc:  # noqa: BLE001
                log.warning("ptz.watchdog_adapter_failed", camera_id=camera_id,
                            error=str(exc))
                self.arbiter.clear_moving(session, camera_id)
                self.arbiter.release(session, camera_id)
                continue
            try:
                await adapter.ptz_stop()
                stopped += 1
                log.warning("ptz.watchdog_stop", camera_id=camera_id)
            except (CapabilityNotSupported, DeviceError) as exc:
                log.warning("ptz.watchdog_stop_failed", camera_id=camera_id,
                            error=str(exc))
            finally:
                await adapter.close()
            self.arbiter.clear_moving(session, camera_id)
            self.arbiter.release(session, camera_id)
        return stopped

    async def _adapter(self, session: Session, camera: Camera) -> DeviceAdapter:
        return await self.adapter_factory(session, camera)
