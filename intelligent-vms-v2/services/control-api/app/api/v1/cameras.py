"""Camera onboarding, inventory, discovery, import/export and lifecycle."""

from __future__ import annotations

import csv
import io
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from vms_contracts.dto import CameraCapabilities, CameraCreate, CameraOut, CameraUpdate, StreamOut
from vms_contracts.enums import (
    CameraState,
    DeviceProtocol,
    Permission,
    RecordingMode,
    RecordingState,
    StreamRole,
    TransportProtocol,
)

from app.core import audit
from app.core.deps import Ctr, CurrentPrincipal, DbSession, client_ip, load_camera, rate_limit
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.netguard import NetworkGuardError
from app.core.rbac import accessible_camera_ids
from app.db.models import Camera, CameraStream, Site
from app.devices.base import Capabilities
from app.devices.dedupe import find_duplicate
from app.devices.discovery import WsDiscovery, probe_unicast_range
from app.devices.probe import apply_probe
from app.logging_setup import get_logger
from app.services import camera_lifecycle

router = APIRouter(prefix="/cameras", tags=["cameras"])
log = get_logger("api.cameras")


def to_dto(camera: Camera) -> CameraOut:
    capability_fields = set(CameraCapabilities.model_fields)
    capabilities = {k: v for k, v in Capabilities.from_dict(camera.capabilities)
                    .to_dict().items() if k in capability_fields}
    return CameraOut(
        id=camera.id, name=camera.name, site_id=camera.site_id, host=camera.host,
        port=camera.port, protocol=DeviceProtocol(camera.protocol),
        vendor=camera.vendor, model=camera.model, firmware=camera.firmware,
        serial=camera.serial, state=CameraState(camera.state), enabled=camera.enabled,
        recording_mode=RecordingMode(camera.recording_mode),
        recording_state=RecordingState(camera.recording_state),
        capabilities=CameraCapabilities(**capabilities),
        streams=[StreamOut(id=s.id, role=StreamRole(s.role), enabled=s.enabled,
                           transport=TransportProtocol(s.transport), codec=s.codec,
                           width=s.width, height=s.height, fps=s.fps,
                           bitrate_kbps=s.bitrate_kbps)
                 for s in sorted(camera.streams, key=lambda s: s.role)],
        tags=list(camera.tags or []), last_seen_at=camera.last_seen_at,
        created_at=camera.created_at)


@router.get("", summary="List cameras visible to you")
def list_cameras(session: DbSession, principal: CurrentPrincipal,
                 site_id: str | None = None, state: str | None = None,
                 search: str | None = None, limit: int = Query(100, ge=1, le=500),
                 offset: int = Query(0, ge=0)) -> dict:
    principal.require(Permission.LIVE_VIEW)
    allowed = accessible_camera_ids(session, principal)

    def apply(statement):  # noqa: ANN001
        if allowed is not None:
            statement = statement.where(Camera.id.in_(allowed))
        if site_id:
            statement = statement.where(Camera.site_id == site_id)
        if state:
            statement = statement.where(Camera.state == state)
        if search:
            pattern = f"%{search}%"
            statement = statement.where(Camera.name.ilike(pattern)
                                        | Camera.host.ilike(pattern))
        return statement

    total = int(session.execute(
        apply(select(func.count()).select_from(Camera))).scalar_one())
    rows = session.execute(
        apply(select(Camera)).order_by(Camera.name).limit(limit).offset(offset)
    ).scalars().all()
    return {"total": total, "limit": limit, "offset": offset,
            "items": [to_dto(c) for c in rows]}


@router.get("/export/csv", summary="Export the camera inventory (credentials excluded)")
def export_cameras(request: Request, session: DbSession,
                   principal: CurrentPrincipal) -> Response:
    principal.require(Permission.CAMERA_MANAGE)
    allowed = accessible_camera_ids(session, principal)
    stmt = select(Camera).order_by(Camera.name)
    if allowed is not None:
        stmt = stmt.where(Camera.id.in_(allowed))

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "name", "host", "port", "rtsp_port", "protocol", "vendor",
                     "model", "serial", "site_id", "state", "recording_mode",
                     "username", "password"])
    for camera in session.execute(stmt).scalars().all():
        writer.writerow([
            camera.id, camera.name, camera.host, camera.port, camera.rtsp_port,
            camera.protocol, camera.vendor or "", camera.model or "",
            camera.serial or "", camera.site_id or "", camera.state,
            camera.recording_mode,
            camera.credential.username if camera.credential else "",
            # Passwords are NEVER exported; import prompts for them instead.
            "***not-exported***" if (camera.credential
                                     and camera.credential.secret_blob) else "",
        ])

    audit.record(session, action=audit.Action.CAMERA_EXPORT, actor_id=principal.user_id,
                 actor_name=principal.username, client_ip=client_ip(request))
    session.commit()
    return Response(content=buffer.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="cameras.csv"'})


@router.post("/discover", summary="Discover ONVIF cameras on the network",
             dependencies=[Depends(rate_limit("discovery"))])
async def discover(request: Request, session: DbSession, container: Ctr,
                   principal: CurrentPrincipal,
                   cidr: Annotated[str | None, Body(embed=True)] = None,
                   timeout_s: Annotated[float, Body(embed=True)] = 5.0) -> dict:
    principal.require(Permission.CAMERA_MANAGE)

    if cidr:
        try:
            hits = await probe_unicast_range(
                container.guard, cidr, list(container.settings.camera_allowed_ports))
        except NetworkGuardError as exc:
            raise ValidationError(str(exc), code=exc.code) from exc
        method = f"unicast:{cidr}"
    else:
        discovery = WsDiscovery(container.guard, timeout_s=min(timeout_s, 15.0))
        hits = await discovery.probe()
        method = "ws-discovery"

    known = {c.host: c for c in session.execute(select(Camera)).scalars().all()}
    results = [{
        "address": hit.address, "port": hit.port, "xaddrs": hit.xaddrs,
        "vendor": hit.vendor, "model": hit.model,
        "already_registered": hit.address in known,
        "camera_id": known[hit.address].id if hit.address in known else None,
    } for hit in hits]

    audit.record(session, action=audit.Action.DISCOVERY_RUN, actor_id=principal.user_id,
                 actor_name=principal.username, client_ip=client_ip(request),
                 detail={"method": method, "found": len(results)})
    session.commit()
    return {"method": method, "found": len(results), "devices": results}


class BulkCamera(BaseModel):
    name: str
    host: str
    port: int = 80
    rtsp_port: int = 554
    protocol: DeviceProtocol = DeviceProtocol.ONVIF
    username: str | None = None
    password: str | None = None
    site_id: str | None = None
    rtsp_main_path: str | None = None


class BulkImport(BaseModel):
    cameras: list[BulkCamera] = Field(max_length=500)
    validate_devices: bool = False
    skip_duplicates: bool = True


@router.post("/import", summary="Bulk onboard cameras")
async def bulk_import(request: Request, payload: BulkImport, session: DbSession,
                      container: Ctr, principal: CurrentPrincipal) -> dict:
    principal.require(Permission.CAMERA_MANAGE)
    container.entitlements.check_camera_quota(session, adding=len(payload.cameras))

    created, skipped, failed = [], [], []
    for entry in payload.cameras:
        try:
            container.guard.resolve_pinned(entry.host, entry.port)
        except NetworkGuardError as exc:
            failed.append({"host": entry.host, "error": str(exc)})
            continue

        duplicate = find_duplicate(session, host=entry.host, port=entry.port)
        if duplicate and payload.skip_duplicates:
            skipped.append({"host": entry.host, "reason": duplicate.reason,
                            "existing_camera_id": duplicate.camera_id})
            continue

        camera = Camera(name=entry.name, host=entry.host, port=entry.port,
                        rtsp_port=entry.rtsp_port, protocol=entry.protocol.value,
                        site_id=entry.site_id, state=CameraState.PROVISIONING.value)
        session.add(camera)
        session.flush()
        if entry.username or entry.password:
            container.credentials.set(session, camera, entry.username, entry.password)
        if entry.rtsp_main_path:
            from app.media.url_builder import build_rtsp_url

            session.add(CameraStream(
                camera_id=camera.id, role=StreamRole.MAIN.value, record=True,
                source_url=build_rtsp_url(host=entry.host, port=entry.rtsp_port,
                                          path=entry.rtsp_main_path)))
        if payload.validate_devices:
            outcome = await container.prober.probe(camera)
            apply_probe(session, camera, outcome, container.credentials)
        camera_lifecycle.ensure_default_schedule(session, camera)
        created.append({"camera_id": camera.id, "name": camera.name,
                        "state": camera.state})

    audit.record(session, action=audit.Action.CAMERA_IMPORT, actor_id=principal.user_id,
                 actor_name=principal.username, client_ip=client_ip(request),
                 detail={"created": len(created), "skipped": len(skipped),
                         "failed": len(failed)})
    session.commit()
    return {"created": created, "skipped": skipped, "failed": failed}


@router.post("", response_model=CameraOut, status_code=201, summary="Onboard a camera")
async def create_camera(request: Request, payload: CameraCreate, session: DbSession,
                        container: Ctr, principal: CurrentPrincipal,
                        validate: bool = Query(True, description="probe the device now"),
                        allow_duplicate: bool = False) -> CameraOut:
    principal.require(Permission.CAMERA_MANAGE)
    container.entitlements.check_camera_quota(session)

    try:
        container.guard.resolve_pinned(payload.host, payload.port)
    except NetworkGuardError as exc:
        raise ValidationError(str(exc), code=exc.code) from exc

    if not allow_duplicate:
        duplicate = find_duplicate(session, host=payload.host, port=payload.port)
        if duplicate and duplicate.confidence == "certain":
            raise ConflictError(
                f"This device is already configured as '{duplicate.camera_name}' "
                f"({duplicate.reason}).", code="duplicate_camera",
                extra={"existing_camera_id": duplicate.camera_id})

    if payload.site_id and session.get(Site, payload.site_id) is None:
        raise NotFoundError("Site not found", code="site_not_found")

    camera = Camera(
        name=payload.name, host=payload.host, port=payload.port,
        rtsp_port=payload.rtsp_port, protocol=payload.protocol.value,
        transport=payload.transport.value, site_id=payload.site_id,
        recording_mode=payload.recording_mode.value,
        storage_volume_id=payload.storage_volume_id, tags=list(payload.tags),
        state=CameraState.PROVISIONING.value)
    session.add(camera)
    session.flush()

    if payload.username or payload.password:
        container.credentials.set(session, camera, payload.username, payload.password)

    if payload.rtsp_main_path or payload.rtsp_sub_path:
        from app.media.url_builder import build_rtsp_url

        for role, path in ((StreamRole.MAIN, payload.rtsp_main_path),
                           (StreamRole.SUB, payload.rtsp_sub_path)):
            if not path:
                continue
            url = build_rtsp_url(host=payload.host, port=payload.rtsp_port, path=path,
                                 use_tls=payload.transport.value.startswith("rtsps"))
            session.add(CameraStream(camera_id=camera.id, role=role.value,
                                     source_url=url,
                                     transport=payload.transport.value,
                                     record=(role is StreamRole.MAIN)))
        session.flush()
        session.refresh(camera)

    if validate:
        outcome = await container.prober.probe(camera)
        apply_probe(session, camera, outcome, container.credentials)
        if not outcome.ok:
            log.warning("camera.validation_failed", camera_id=camera.id,
                        error=outcome.error, code=outcome.error_code)

    camera_lifecycle.ensure_default_schedule(session, camera)
    audit.record(session, action=audit.Action.CAMERA_CREATE, actor_id=principal.user_id,
                 actor_name=principal.username, object_type="camera",
                 object_id=camera.id, client_ip=client_ip(request),
                 detail={"host": payload.host, "protocol": payload.protocol.value,
                         "validated": validate, "state": camera.state})
    session.commit()
    session.refresh(camera)
    return to_dto(camera)


@router.get("/{camera_id}", response_model=CameraOut, summary="Camera detail")
def get_camera(camera_id: str, session: DbSession,
               principal: CurrentPrincipal) -> CameraOut:
    return to_dto(load_camera(session, principal, camera_id, Permission.LIVE_VIEW))


@router.patch("/{camera_id}", response_model=CameraOut, summary="Update a camera")
async def update_camera(request: Request, camera_id: str, payload: CameraUpdate,
                        session: DbSession, container: Ctr,
                        principal: CurrentPrincipal) -> CameraOut:
    camera = load_camera(session, principal, camera_id, Permission.CAMERA_MANAGE)
    changes: dict[str, Any] = {}

    if payload.name is not None:
        camera.name = changes["name"] = payload.name
    if payload.site_id is not None:
        if session.get(Site, payload.site_id) is None:
            raise NotFoundError("Site not found", code="site_not_found")
        camera.site_id = changes["site_id"] = payload.site_id
    if payload.enabled is not None:
        camera_lifecycle.set_enabled(session, camera, payload.enabled)
        changes["enabled"] = payload.enabled
    if payload.recording_mode is not None:
        camera.recording_mode = changes["recording_mode"] = payload.recording_mode.value
    if payload.storage_volume_id is not None:
        camera.storage_volume_id = changes["storage_volume_id"] = payload.storage_volume_id
    if payload.tags is not None:
        camera.tags = changes["tags"] = list(payload.tags)

    if payload.username is not None or payload.password is not None:
        container.credentials.set(session, camera, payload.username, payload.password)
        changes["credentials"] = "updated"
        # A stream authenticated with the old credentials must not linger.
        await container.sessions.close_for_camera(session, camera.id,
                                                  reason="credentials_changed")
        audit.record(session, action=audit.Action.CAMERA_CREDENTIAL_CHANGE,
                     actor_id=principal.user_id, actor_name=principal.username,
                     object_type="camera", object_id=camera.id,
                     client_ip=client_ip(request))

    camera.version += 1
    audit.record(session, action=audit.Action.CAMERA_UPDATE, actor_id=principal.user_id,
                 actor_name=principal.username, object_type="camera",
                 object_id=camera.id, client_ip=client_ip(request), detail=changes)
    session.commit()
    session.refresh(camera)
    return to_dto(camera)


@router.delete("/{camera_id}", summary="Delete a camera safely")
async def delete_camera(request: Request, camera_id: str, session: DbSession,
                        container: Ctr, principal: CurrentPrincipal) -> dict:
    load_camera(session, principal, camera_id, Permission.CAMERA_MANAGE)
    report = await camera_lifecycle.delete_camera(
        session, camera_id, actor_id=principal.user_id, actor_name=principal.username,
        session_broker=container.sessions, ptz_service=container.ptz,
        client_ip=client_ip(request))
    session.commit()
    return {
        "deleted": camera_id,
        "recording_tasks_stopped": report.recording_tasks_stopped,
        "live_sessions_closed": report.live_sessions_closed,
        "recordings_preserved": report.segments_preserved,
        "note": "Recorded footage is retained and remains playable and exportable.",
    }


class ReplaceRequest(BaseModel):
    host: str
    port: int | None = None
    username: str | None = None
    password: str | None = None


@router.post("/{camera_id}/replace", response_model=CameraOut,
             summary="Swap in replacement hardware, keeping history")
async def replace_camera(request: Request, camera_id: str, payload: ReplaceRequest,
                         session: DbSession, container: Ctr,
                         principal: CurrentPrincipal) -> CameraOut:
    load_camera(session, principal, camera_id, Permission.CAMERA_MANAGE)
    camera = await camera_lifecycle.replace_camera(
        session, camera_id, host=payload.host, port=payload.port,
        username=payload.username, password=payload.password,
        actor_id=principal.user_id, actor_name=principal.username,
        prober=container.prober, credential_store=container.credentials,
        session_broker=container.sessions, client_ip=client_ip(request))
    session.commit()
    session.refresh(camera)
    return to_dto(camera)


@router.post("/{camera_id}/validate", summary="Re-probe a camera now")
async def validate_camera(camera_id: str, session: DbSession, container: Ctr,
                          principal: CurrentPrincipal) -> dict:
    camera = load_camera(session, principal, camera_id, Permission.CAMERA_MANAGE)
    outcome = await container.prober.probe(camera)
    apply_probe(session, camera, outcome, container.credentials)
    session.commit()
    return {"ok": outcome.ok, "state": camera.state, "error": outcome.error,
            "error_code": outcome.error_code, "vendor": camera.vendor,
            "model": camera.model, "profiles_found": len(outcome.profiles),
            "capabilities": camera.capabilities}
