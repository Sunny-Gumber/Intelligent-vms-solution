"""Storage volume administration and diagnostics."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from vms_contracts.dto import VolumeOut
from vms_contracts.enums import Permission, StorageState, VolumeKind

from app.core import audit
from app.core.deps import Ctr, CurrentPrincipal, DbSession, client_ip
from app.core.errors import NotFoundError, ValidationError
from app.db.models import StorageVolume
from app.recording import retention
from app.storage import diagnostics, raid
from app.storage import volumes as volume_service

router = APIRouter(prefix="/storage", tags=["storage"])


class VolumeIn(BaseModel):
    name: str
    path: str
    kind: VolumeKind = VolumeKind.LOCAL
    min_free_percent: float = Field(default=10.0, ge=0, le=90)
    priority: int = 100


def dto(volume: StorageVolume, camera_count: int = 0) -> VolumeOut:
    return VolumeOut(id=volume.id, name=volume.name, path=volume.path,
                     kind=VolumeKind(volume.kind), state=StorageState(volume.state),
                     total_bytes=volume.total_bytes, free_bytes=volume.free_bytes,
                     used_bytes=volume.used_bytes,
                     min_free_percent=volume.min_free_percent, enabled=volume.enabled,
                     camera_count=camera_count, raid_status=volume.raid_status,
                     last_checked_at=volume.last_checked_at)


@router.get("/volumes", summary="Storage volumes")
def list_volumes(session: DbSession, principal: CurrentPrincipal) -> dict:
    principal.require(Permission.LIVE_VIEW)
    volume_service.refresh_all(session)
    counts = volume_service.camera_counts(session)
    rows = session.execute(
        select(StorageVolume).order_by(StorageVolume.priority)).scalars().all()
    payload = {"items": [dto(v, counts.get(v.id, 0)) for v in rows],
               "raid_arrays": [{"device": a.device, "level": a.level, "state": a.state,
                                "detail": a.detail} for a in raid.all_arrays()],
               "raid_note": ("Software RAID only. Hardware RAID controllers are not "
                             "queried; their status is reported as unknown.")}
    session.commit()
    return payload


@router.post("/volumes", response_model=VolumeOut, status_code=201,
             summary="Register a storage volume")
def create_volume(request: Request, payload: VolumeIn, session: DbSession,
                  principal: CurrentPrincipal) -> VolumeOut:
    principal.require(Permission.STORAGE_MANAGE)
    try:
        volume = volume_service.create_volume(
            session, name=payload.name, path=payload.path, kind=payload.kind,
            min_free_percent=payload.min_free_percent, priority=payload.priority)
    except ValueError as exc:
        raise ValidationError(str(exc), code="storage_path_unusable") from exc
    audit.record(session, action=audit.Action.STORAGE_UPDATE,
                 actor_id=principal.user_id, actor_name=principal.username,
                 object_type="storage_volume", object_id=volume.id,
                 client_ip=client_ip(request),
                 detail={"name": payload.name, "path": payload.path})
    session.commit()
    return dto(volume)


@router.patch("/volumes/{volume_id}", response_model=VolumeOut,
              summary="Update a volume")
def update_volume(request: Request, volume_id: str, payload: dict, session: DbSession,
                  principal: CurrentPrincipal) -> VolumeOut:
    principal.require(Permission.STORAGE_MANAGE)
    volume = session.get(StorageVolume, volume_id)
    if volume is None:
        raise NotFoundError("Storage volume not found", code="volume_not_found")
    for field in ("name", "enabled", "min_free_percent", "priority", "raid_device"):
        if field in payload:
            setattr(volume, field, payload[field])
    volume_service.refresh_volume(session, volume)
    audit.record(session, action=audit.Action.STORAGE_UPDATE,
                 actor_id=principal.user_id, actor_name=principal.username,
                 object_type="storage_volume", object_id=volume_id,
                 client_ip=client_ip(request), detail={"changed": sorted(payload)})
    session.commit()
    return dto(volume)


@router.get("/diagnostics", summary="Storage diagnostics")
def storage_diagnostics(session: DbSession, principal: CurrentPrincipal) -> dict:
    principal.require(Permission.DIAGNOSTICS_READ)
    reports = diagnostics.diagnose_all(session)
    payload = {"volumes": [asdict(r) for r in reports],
               "cameras_without_storage": diagnostics.cameras_without_storage(session)}
    session.commit()
    return payload


@router.post("/retention/run", summary="Run retention now")
def run_retention(request: Request, session: DbSession, container: Ctr,
                  principal: CurrentPrincipal, dry_run: bool = True) -> dict:
    principal.require(Permission.STORAGE_MANAGE)
    result = retention.run_retention(session, settings=container.settings,
                                     dry_run=dry_run)
    audit.record(session, action=audit.Action.RETENTION_DELETE,
                 actor_id=principal.user_id, actor_name=principal.username,
                 client_ip=client_ip(request),
                 detail={"dry_run": dry_run, "deleted": result.deleted,
                         "bytes": result.bytes_reclaimed})
    session.commit()
    return {"dry_run": dry_run, "examined": result.examined,
            "deleted": result.deleted, "bytes_reclaimed": result.bytes_reclaimed,
            "missing_files": result.missing_files, "errors": result.errors[:20],
            "emergency_rotation": result.emergency}
