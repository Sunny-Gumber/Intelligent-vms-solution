"""System health, capacity planning, licensing, layouts and federation."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select

from vms_contracts.dto import CapacityEstimate, HealthReport, LicenseStatus
from vms_contracts.enums import AlarmState, CameraState, FeatureFlag, Permission

from app.capacity.model import CameraClass, Workload, estimate, reference_workloads
from app.core import audit
from app.core.deps import AdminPrincipal, Ctr, CurrentPrincipal, DbSession, client_ip
from app.core.errors import NotFoundError
from app.db.models import Alarm, Camera, FederationNode, Layout
from app.health import metrics
from app.version import version_info

router = APIRouter(prefix="/system", tags=["system"])


class LicenseUpload(BaseModel):
    license_document: str


class CameraClassIn(BaseModel):
    count: int
    width: int = 1920
    height: int = 1080
    fps: float = 15.0
    codec: str = "h265"
    complexity: str = "medium"
    duty_cycle: float = 1.0
    analytics: bool = False


class CapacityRequest(BaseModel):
    classes: list[CameraClassIn]
    retention_days: int = 14
    live_viewers: int = 4
    streams_per_viewer: int = 16
    concurrent_exports: int = 2


class LayoutIn(BaseModel):
    name: str
    rows: int = 2
    cols: int = 2
    tiles: list[dict] = []
    shared: bool = False
    monitor_index: int = 0


class NodeIn(BaseModel):
    name: str
    base_url: str
    token: str | None = None


@router.get("/version", summary="Build and version information")
def version() -> dict:
    return version_info()


@router.get("/health", response_model=HealthReport,
            summary="Detailed component health")
async def health(container: Ctr, principal: CurrentPrincipal) -> HealthReport:
    principal.require(Permission.LIVE_VIEW)
    return await container.health.check()


@router.get("/metrics", summary="Prometheus metrics")
def prometheus(principal: CurrentPrincipal) -> Response:
    principal.require(Permission.DIAGNOSTICS_READ)
    body, content_type = metrics.prometheus_exposition()
    return Response(content=body, media_type=content_type)


@router.get("/summary", summary="Compact status summary (used by federation peers)")
def summary(session: DbSession, principal: CurrentPrincipal) -> dict:
    principal.require(Permission.LIVE_VIEW)
    return {
        "version": version_info()["version"],
        "cameras_total": int(session.execute(
            select(func.count()).select_from(Camera)).scalar_one()),
        "cameras_online": int(session.execute(
            select(func.count()).select_from(Camera)
            .where(Camera.state == CameraState.ONLINE.value)).scalar_one()),
        "alarms_open": int(session.execute(
            select(func.count()).select_from(Alarm)
            .where(Alarm.state.in_((AlarmState.NEW.value,
                                    AlarmState.ACTIVE.value)))).scalar_one()),
    }


@router.get("/license", response_model=LicenseStatus, summary="Licence status")
def license_status(session: DbSession, container: Ctr,
                   principal: CurrentPrincipal) -> LicenseStatus:
    principal.require(Permission.LIVE_VIEW)
    cameras = int(session.execute(select(func.count()).select_from(Camera)).scalar_one())
    return container.entitlements.to_dto(cameras)


@router.post("/license", response_model=LicenseStatus, summary="Install a licence file")
def install_license(request: Request, payload: LicenseUpload, session: DbSession,
                    container: Ctr, principal: AdminPrincipal) -> LicenseStatus:
    path = container.settings.data_root / "license.json"
    path.write_text(payload.license_document, encoding="utf-8")
    container.settings.license_file = path
    entitlements = container.refresh_entitlements()
    cameras = int(session.execute(select(func.count()).select_from(Camera)).scalar_one())
    audit.record(session, action=audit.Action.LICENSE_UPDATE,
                 actor_id=principal.user_id, actor_name=principal.username,
                 client_ip=client_ip(request),
                 detail={"edition": entitlements.edition.value,
                         "valid": entitlements.valid})
    session.commit()
    return entitlements.to_dto(cameras)


@router.post("/capacity", response_model=CapacityEstimate,
             summary="Capacity planning estimate (model, not a benchmark)")
def capacity(payload: CapacityRequest, principal: CurrentPrincipal) -> CapacityEstimate:
    principal.require(Permission.LIVE_VIEW)
    return estimate(Workload(
        classes=[CameraClass(**c.model_dump()) for c in payload.classes],
        retention_days=payload.retention_days, live_viewers=payload.live_viewers,
        streams_per_viewer=payload.streams_per_viewer,
        concurrent_exports=payload.concurrent_exports))


@router.get("/capacity/reference", summary="Reference deployment sizings")
def capacity_reference(principal: CurrentPrincipal) -> dict:
    principal.require(Permission.LIVE_VIEW)
    return {name: estimate(workload)
            for name, workload in reference_workloads().items()}


@router.get("/layouts", summary="Your saved layouts")
def list_layouts(session: DbSession, principal: CurrentPrincipal) -> dict:
    principal.require(Permission.LIVE_VIEW)
    rows = session.execute(
        select(Layout).where((Layout.owner_id == principal.user_id)
                             | (Layout.shared.is_(True)))
        .order_by(Layout.name)).scalars().all()
    return {"items": [{"id": layout.id, "name": layout.name, "rows": layout.rows,
                       "cols": layout.cols, "tiles": layout.tiles,
                       "shared": layout.shared, "owner_id": layout.owner_id,
                       "monitor_index": layout.monitor_index} for layout in rows]}


@router.post("/layouts", status_code=201, summary="Save a layout")
def save_layout(payload: LayoutIn, session: DbSession,
                principal: CurrentPrincipal) -> dict:
    principal.require(Permission.LIVE_VIEW)
    existing = session.execute(
        select(Layout).where(Layout.owner_id == principal.user_id,
                             Layout.name == payload.name)).scalar_one_or_none()
    layout = existing or Layout(owner_id=principal.user_id, name=payload.name)
    layout.rows, layout.cols = payload.rows, payload.cols
    layout.tiles = payload.tiles
    layout.shared = payload.shared
    layout.monitor_index = payload.monitor_index
    if existing is None:
        session.add(layout)
    session.flush()
    result = {"id": layout.id, "name": layout.name}
    session.commit()
    return result


@router.delete("/layouts/{layout_id}", status_code=204, summary="Delete a layout")
def delete_layout(layout_id: str, session: DbSession,
                  principal: CurrentPrincipal) -> Response:
    layout = session.get(Layout, layout_id)
    if layout is None or (layout.owner_id != principal.user_id
                          and not principal.is_admin):
        raise NotFoundError("Layout not found", code="layout_not_found")
    session.delete(layout)
    session.commit()
    return Response(status_code=204)


@router.get("/federation/nodes", summary="Registered remote servers")
async def list_nodes(session: DbSession, container: Ctr,
                     principal: CurrentPrincipal) -> dict:
    principal.require(Permission.FEDERATION_MANAGE)
    summaries = await container.federation.poll_all(session)
    session.commit()
    return {
        "enabled": container.settings.federation_enabled,
        "nodes": [s.to_dict() for s in summaries],
        "implemented": ["registration", "health aggregation",
                        "camera and alarm counts"],
        "not_implemented": ["distributed recording", "cross-node failover",
                            "live video proxying"],
    }


@router.post("/federation/nodes", status_code=201,
             summary="Register a remote server")
def register_node(request: Request, payload: NodeIn, session: DbSession, container: Ctr,
                  principal: CurrentPrincipal) -> dict:
    principal.require(Permission.FEDERATION_MANAGE)
    container.entitlements.require(FeatureFlag.FEDERATION)
    node = FederationNode(name=payload.name, base_url=payload.base_url)
    session.add(node)
    session.flush()
    if payload.token:
        node.token_blob = container.vault.encrypt(payload.token, aad=f"node:{node.id}")
    audit.record(session, action="federation.node.register",
                 actor_id=principal.user_id, actor_name=principal.username,
                 object_type="federation_node", object_id=node.id,
                 client_ip=client_ip(request),
                 detail={"name": payload.name, "base_url": payload.base_url})
    result = {"id": node.id, "name": node.name}
    session.commit()
    return result
