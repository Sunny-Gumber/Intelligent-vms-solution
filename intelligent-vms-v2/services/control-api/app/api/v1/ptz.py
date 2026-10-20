"""PTZ control endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from vms_contracts.dto import PtzCommand, PtzPresetOut
from vms_contracts.enums import Permission

from app.core import audit
from app.core.deps import Ctr, CurrentPrincipal, DbSession, client_ip, load_camera
from app.db.models import PtzPreset
from app.devices.base import Capabilities

router = APIRouter(prefix="/ptz", tags=["ptz"])


class PresetIn(BaseModel):
    name: str


@router.get("/{camera_id}/capabilities",
            summary="What PTZ this camera actually supports")
def capabilities(camera_id: str, session: DbSession,
                 principal: CurrentPrincipal) -> dict:
    camera = load_camera(session, principal, camera_id, Permission.LIVE_VIEW)
    caps = Capabilities.from_dict(camera.capabilities)
    return {
        "camera_id": camera_id, "ptz_available": caps.any_ptz,
        "continuous": caps.ptz_continuous, "relative": caps.ptz_relative,
        "absolute": caps.ptz_absolute, "presets": caps.ptz_presets,
        "probed_at": caps.probed_at,
        "note": ("Capabilities are reported only when the device demonstrated them "
                 "during probing. Re-validate the camera after a firmware upgrade."),
    }


@router.post("/{camera_id}/command", summary="Send a PTZ command")
async def command(request: Request, camera_id: str, payload: PtzCommand,
                  session: DbSession, container: Ctr,
                  principal: CurrentPrincipal) -> dict:
    camera = load_camera(session, principal, camera_id, Permission.PTZ_CONTROL)
    outcome = await container.ptz.execute(
        session, camera, payload, owner_id=principal.user_id,
        owner_name=principal.username, priority=10 if principal.is_admin else 0)
    audit.record(session, action=audit.Action.PTZ_COMMAND, actor_id=principal.user_id,
                 actor_name=principal.username, object_type="camera",
                 object_id=camera_id, client_ip=client_ip(request),
                 detail={"kind": payload.kind.value, "duration_s": outcome.duration_s})
    session.commit()
    return {"accepted": outcome.accepted, "kind": outcome.kind.value,
            "duration_s": outcome.duration_s, "detail": outcome.detail,
            "max_move_seconds": container.settings.ptz_max_move_seconds}


@router.post("/{camera_id}/release", status_code=204, summary="Release PTZ control")
def release(camera_id: str, session: DbSession, container: Ctr,
            principal: CurrentPrincipal) -> Response:
    load_camera(session, principal, camera_id, Permission.PTZ_CONTROL)
    container.ptz.arbiter.release(session, camera_id, principal.user_id)
    session.commit()
    return Response(status_code=204)


@router.get("/{camera_id}/lock", summary="Who currently controls this camera")
def lock_status(camera_id: str, session: DbSession, container: Ctr,
                principal: CurrentPrincipal) -> dict:
    load_camera(session, principal, camera_id, Permission.LIVE_VIEW)
    lock = container.ptz.arbiter.owner(session, camera_id)
    if lock is None:
        return {"camera_id": camera_id, "locked": False}
    return {"camera_id": camera_id, "locked": True,
            "owner": lock.owner_name or lock.owner_id,
            "is_you": lock.owner_id == principal.user_id,
            "expires_at": lock.expires_at, "moving_until": lock.moving_until}


@router.get("/{camera_id}/presets", summary="List presets")
def list_presets(camera_id: str, session: DbSession,
                 principal: CurrentPrincipal) -> dict:
    load_camera(session, principal, camera_id, Permission.LIVE_VIEW)
    rows = session.execute(
        select(PtzPreset).where(PtzPreset.camera_id == camera_id)
        .order_by(PtzPreset.name)).scalars().all()
    return {"items": [PtzPresetOut(id=p.id, camera_id=p.camera_id, name=p.name,
                                   token=p.token, created_at=p.created_at)
                      for p in rows]}


@router.post("/{camera_id}/presets", response_model=PtzPresetOut, status_code=201,
             summary="Save the current position as a preset")
async def save_preset(request: Request, camera_id: str, payload: PresetIn,
                      session: DbSession, container: Ctr,
                      principal: CurrentPrincipal) -> PtzPresetOut:
    camera = load_camera(session, principal, camera_id, Permission.PTZ_PRESET_MANAGE)
    preset = await container.ptz.save_preset(session, camera, payload.name,
                                             principal.user_id)
    audit.record(session, action=audit.Action.PTZ_PRESET_SAVE,
                 actor_id=principal.user_id, actor_name=principal.username,
                 object_type="camera", object_id=camera_id,
                 client_ip=client_ip(request), detail={"preset": payload.name})
    session.commit()
    session.refresh(preset)
    return PtzPresetOut(id=preset.id, camera_id=preset.camera_id, name=preset.name,
                        token=preset.token, created_at=preset.created_at)


@router.delete("/{camera_id}/presets/{preset_id}", status_code=204,
               summary="Delete a preset")
async def delete_preset(request: Request, camera_id: str, preset_id: str,
                        session: DbSession, container: Ctr,
                        principal: CurrentPrincipal) -> Response:
    camera = load_camera(session, principal, camera_id, Permission.PTZ_PRESET_MANAGE)
    await container.ptz.delete_preset(session, camera, preset_id)
    audit.record(session, action=audit.Action.PTZ_PRESET_DELETE,
                 actor_id=principal.user_id, actor_name=principal.username,
                 object_type="camera", object_id=camera_id,
                 client_ip=client_ip(request), detail={"preset_id": preset_id})
    session.commit()
    return Response(status_code=204)
