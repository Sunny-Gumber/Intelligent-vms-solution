from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, require_roles
from app.db.session import get_session
from app.models.entities import CameraEntity, CameraHealthStateEntity
from app.models.schemas import CameraHealthStateRead, HealthSummary
from app.services.health_monitor import stats

router = APIRouter(prefix="/api/v1/health", tags=["health"])


def _scope(query, principal: Principal):
    if principal.tenant_id != "*":
        query = query.where(CameraEntity.tenant_id == principal.tenant_id)
    if "*" not in principal.site_ids:
        query = query.where(CameraEntity.site_id.in_(principal.site_ids))
    return query


@router.get("/cameras", response_model=list[CameraHealthStateRead])
async def camera_health_states(
    state: str | None = Query(default=None, pattern="^(unknown|degraded|online|offline)$"),
    site_id: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    after_camera_id: str | None = None,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """List persisted camera health states within caller scope.

    Args:
        state: Optional health-state filter.
        site_id: Optional site filter.
        limit: Bounded page size.
        after_camera_id: Optional keyset-pagination cursor.
        session: Database session used for scoped health query.
        principal: Authenticated administrator, operator or viewer.

    Returns:
        CameraHealthStateRead list ordered by camera identifier.
    """
    q = (
        select(CameraEntity, CameraHealthStateEntity)
        .join(CameraHealthStateEntity, CameraHealthStateEntity.camera_id == CameraEntity.id)
        .order_by(CameraEntity.id)
        .limit(limit)
    )
    q = _scope(q, principal)
    if state:
        q = q.where(CameraHealthStateEntity.state == state)
    if site_id:
        q = q.where(CameraEntity.site_id == site_id)
    if after_camera_id:
        q = q.where(CameraEntity.id > after_camera_id)
    rows = (await session.execute(q)).all()
    return [
        CameraHealthStateRead(
            camera_id=camera.id,
            tenant_id=camera.tenant_id,
            site_id=camera.site_id,
            name=camera.name,
            state=health.state,
            path_present=health.path_present,
            ready=health.ready,
            observed_at=health.observed_at,
            changed_at=health.changed_at,
            detail=health.detail_json or {},
        )
        for camera, health in rows
    ]


@router.get("/summary", response_model=HealthSummary)
async def health_summary(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """Return scoped camera-health counts and latest monitor statistics.

    Args:
        session: Database session used for scoped aggregate query.
        principal: Authenticated administrator, operator or viewer.

    Returns:
        HealthSummary containing state totals and monitor counters.
    """
    q = (
        select(CameraHealthStateEntity.state, func.count())
        .join(CameraEntity, CameraEntity.id == CameraHealthStateEntity.camera_id)
        .group_by(CameraHealthStateEntity.state)
    )
    q = _scope(q, principal)
    counts = {state: count for state, count in (await session.execute(q)).all()}
    return HealthSummary(
        total=sum(counts.values()),
        unknown=counts.get("unknown", 0),
        degraded=counts.get("degraded", 0),
        online=counts.get("online", 0),
        offline=counts.get("offline", 0),
        monitor_last_duration_seconds=stats.last_duration_seconds,
        monitor_last_scanned=stats.last_scanned,
        monitor_last_changed=stats.last_changed,
        monitor_media_errors=stats.media_errors,
    )
