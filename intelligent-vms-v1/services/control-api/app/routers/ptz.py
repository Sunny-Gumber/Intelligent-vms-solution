"""Authorized camera PTZ API.

This router is the only desktop-facing PTZ authority.  Camera credentials,
service addresses and raw SOAP responses never cross the API boundary.
"""

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, require_roles
from app.core.security import decrypt_secret
from app.db.session import get_session
from app.models.entities import CameraCapabilityEntity, CameraEntity
from app.models.schemas import (
    PtzCapabilitiesRead,
    PtzCommandRead,
    PtzMoveRequest,
    PtzStopRequest,
)
from app.routers.cameras import authorized_camera
from app.services.network_policy import TargetNotAllowed
from app.services.onvif_client import OnvifError
from app.services import ptz as ptz_service

router = APIRouter(prefix="/api/v1/ptz", tags=["ptz"])

_generation_lock = asyncio.Lock()
_generations: dict[tuple[str, str, str], int] = {}
_last_move_at: dict[tuple[str, str, str], float] = {}
_MIN_MOVE_INTERVAL_SECONDS = 0.075


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, TargetNotAllowed):
        return HTTPException(
            400,
            {"code": "TARGET_NOT_ALLOWED", "message": "ONVIF target is not allowed"},
        )
    if isinstance(exc, OnvifError):
        return HTTPException(exc.status_code, {"code": exc.code, "message": exc.message})
    return HTTPException(502, {"code": "PTZ_ERROR", "message": "PTZ operation failed"})


async def _context(
    camera_id: str,
    session: AsyncSession,
    principal: Principal,
) -> tuple[CameraEntity, CameraCapabilityEntity, str | None, str | None]:
    camera = await authorized_camera(session, camera_id, principal)
    capability = (
        await session.execute(
            select(CameraCapabilityEntity).where(CameraCapabilityEntity.camera_id == camera_id)
        )
    ).scalar_one_or_none()
    if capability is None:
        raise HTTPException(
            404,
            {"code": "ONVIF_CAPABILITY_NOT_FOUND", "message": "ONVIF capability snapshot not found"},
        )
    if not bool((capability.features_json or {}).get("ptz")):
        raise HTTPException(
            422,
            {"code": "PTZ_UNAVAILABLE", "message": "Camera does not advertise ONVIF PTZ"},
        )
    return (
        camera,
        capability,
        decrypt_secret(camera.username_enc),
        decrypt_secret(camera.password_enc),
    )


async def _claim_generation(
    principal: Principal,
    camera_id: str,
    generation: int,
    context_id: str,
    *,
    movement: bool,
) -> None:
    key = (principal.subject, camera_id, context_id)
    async with _generation_lock:
        current = _generations.get(key, 0)
        if generation <= current:
            raise HTTPException(
                409,
                {"code": "STALE_PTZ_COMMAND", "message": "PTZ command generation is stale"},
            )
        if movement:
            now = time.monotonic()
            last = _last_move_at.get(key)
            if last is not None and now - last < _MIN_MOVE_INTERVAL_SECONDS:
                raise HTTPException(
                    429,
                    {"code": "PTZ_RATE_LIMITED", "message": "PTZ movement commands are too frequent"},
                )
            _last_move_at[key] = now
        _generations[key] = generation


async def _is_current_generation(
    principal: Principal, camera_id: str, generation: int, context_id: str
) -> bool:
    key = (principal.subject, camera_id, context_id)
    async with _generation_lock:
        return _generations.get(key) == generation


@router.get("/cameras/{camera_id}/capabilities", response_model=PtzCapabilitiesRead)
async def camera_ptz_capabilities(
    camera_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Return live server-authoritative continuous PTZ capabilities."""
    camera, capability, username, password = await _context(camera_id, session, principal)
    try:
        discovered = await ptz_service.capabilities(
            capability.services_json or [],
            capability.main_profile_token,
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
        return PtzCapabilitiesRead(camera_id=camera.id, **discovered)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/cameras/{camera_id}/move", response_model=PtzCommandRead)
async def move_camera(
    camera_id: str,
    payload: PtzMoveRequest,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Issue one fenced, bounded continuous PTZ movement command."""
    camera, capability, username, password = await _context(camera_id, session, principal)
    await _claim_generation(
        principal, camera.id, payload.generation, str(payload.context_id), movement=True
    )
    try:
        await ptz_service.continuous_move(
            capability.services_json or [],
            capability.main_profile_token,
            payload.pan,
            payload.tilt,
            payload.zoom,
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
        if not await _is_current_generation(
            principal, camera.id, payload.generation, str(payload.context_id)
        ):
            # A newer STOP/context command won while the device call was in flight.
            # Compensate after late MOVE completion so motion cannot remain active.
            try:
                await ptz_service.stop(
                    capability.services_json or [],
                    capability.main_profile_token,
                    username,
                    password,
                    camera.tenant_id,
                    camera.site_id,
                )
            except Exception:
                pass
            raise HTTPException(
                409,
                {"code": "STALE_PTZ_COMMAND", "message": "PTZ movement was superseded"},
            )
        return PtzCommandRead(camera_id=camera.id, state="moving", generation=payload.generation)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/cameras/{camera_id}/stop", response_model=PtzCommandRead)
async def stop_camera(
    camera_id: str,
    payload: PtzStopRequest,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Issue a stop-priority fenced PTZ stop command."""
    camera, capability, username, password = await _context(camera_id, session, principal)
    await _claim_generation(
        principal, camera.id, payload.generation, str(payload.context_id), movement=False
    )
    try:
        await ptz_service.stop(
            capability.services_json or [],
            capability.main_profile_token,
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
        return PtzCommandRead(camera_id=camera.id, state="stopped", generation=payload.generation)
    except Exception as exc:
        raise _http_error(exc) from exc
