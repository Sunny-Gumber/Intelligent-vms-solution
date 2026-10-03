from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, require_roles
from app.core.config import settings
from app.db.session import get_session
from app.models.entities import CameraEntity, ManualRecordingSessionEntity
from app.models.schemas import ManualRecordingRead
from app.routers.cameras import authorized_camera
from app.routers.recordings import _policy_for_camera, stream_recording_clip

router = APIRouter(prefix="/api/v1/manual-recordings", tags=["manual-recordings"])


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _max_stop(row: ManualRecordingSessionEntity) -> datetime:
    return _utc(row.max_stop_at)


def _read(row: ManualRecordingSessionEntity) -> ManualRecordingRead:
    start = _utc(row.started_at)
    stop = _utc(row.stopped_at) if row.stopped_at else None
    return ManualRecordingRead(
        id=row.id, tenant_id=row.tenant_id, site_id=row.site_id, camera_id=row.camera_id,
        operator_subject=row.operator_subject, state=row.state, started_at=start, stopped_at=stop,
        effective_max_stop_at=_utc(row.max_stop_at),
        duration=(stop-start).total_seconds() if stop else None,
        download_ready=bool(row.state == "STOPPED" and row.camera_id),
    )


def _can_manage(row: ManualRecordingSessionEntity, principal: Principal) -> bool:
    return principal.can_access(row.tenant_id, row.site_id) and (
        principal.subject == row.operator_subject or "admin" in principal.roles
    )


async def _finalize_if_expired(session: AsyncSession, row: ManualRecordingSessionEntity, now: datetime) -> bool:
    if row.state == "ACTIVE" and _max_stop(row) <= now:
        row.state = "STOPPED"
        row.stopped_at = _max_stop(row)
        await session.flush()
        return True
    return False


async def _owned_session(session: AsyncSession, session_id: str, principal: Principal, *, lock: bool = False):
    stmt = select(ManualRecordingSessionEntity).where(ManualRecordingSessionEntity.id == session_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None or not _can_manage(row, principal):
        raise HTTPException(404, "Manual recording session not found")
    return row


@router.post("/cameras/{camera_id}/start", response_model=ManualRecordingRead)
async def start_manual_recording(
    camera_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Persist an idempotent server-timed manual-recording intent."""
    camera = await authorized_camera(session, camera_id, principal)
    policy = await _policy_for_camera(session, camera.id, lock=True)
    if policy is None or not policy.enabled or policy.mode != "continuous":
        raise HTTPException(409, "Continuous authoritative recording must be active before Start")
    now = datetime.now(timezone.utc)
    existing = (
        await session.execute(
            select(ManualRecordingSessionEntity).where(
                ManualRecordingSessionEntity.tenant_id == camera.tenant_id,
                ManualRecordingSessionEntity.site_id == camera.site_id,
                ManualRecordingSessionEntity.camera_id == camera.id,
                ManualRecordingSessionEntity.operator_subject == principal.subject,
                ManualRecordingSessionEntity.state == "ACTIVE",
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:
        if await _finalize_if_expired(session, existing, now):
            await session.commit()
        else:
            return _read(existing)
    row = ManualRecordingSessionEntity(
        tenant_id=camera.tenant_id, site_id=camera.site_id, camera_id=camera.id,
        operator_subject=principal.subject, state="ACTIVE", started_at=now,
        max_stop_at=now + timedelta(seconds=settings.recording_export_max_duration_seconds),
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = (
            await session.execute(
                select(ManualRecordingSessionEntity).where(
                    ManualRecordingSessionEntity.tenant_id == camera.tenant_id,
                    ManualRecordingSessionEntity.site_id == camera.site_id,
                    ManualRecordingSessionEntity.camera_id == camera.id,
                    ManualRecordingSessionEntity.operator_subject == principal.subject,
                    ManualRecordingSessionEntity.state == "ACTIVE",
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise HTTPException(409, "Concurrent manual recording Start conflict")
        return _read(existing)
    await session.refresh(row)
    return _read(row)


@router.get("/active", response_model=list[ManualRecordingRead])
async def active_manual_recordings(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Return currently active manual intents visible to this principal."""
    stmt = select(ManualRecordingSessionEntity).where(ManualRecordingSessionEntity.state == "ACTIVE")
    if principal.tenant_id != "*":
        stmt = stmt.where(ManualRecordingSessionEntity.tenant_id == principal.tenant_id)
    if "*" not in principal.site_ids:
        stmt = stmt.where(ManualRecordingSessionEntity.site_id.in_(principal.site_ids))
    if "admin" not in principal.roles:
        stmt = stmt.where(ManualRecordingSessionEntity.operator_subject == principal.subject)
    rows = list((await session.execute(stmt.order_by(ManualRecordingSessionEntity.started_at))).scalars())
    now = datetime.now(timezone.utc)
    changed = False
    for row in rows:
        changed = await _finalize_if_expired(session, row, now) or changed
    if changed:
        await session.commit()
    return [_read(row) for row in rows if row.state == "ACTIVE"]


@router.get("/recent", response_model=list[ManualRecordingRead])
async def recent_manual_recordings(
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Return a bounded recent manual-session history for recovery and retry."""
    stmt = select(ManualRecordingSessionEntity)
    if principal.tenant_id != "*":
        stmt = stmt.where(ManualRecordingSessionEntity.tenant_id == principal.tenant_id)
    if "*" not in principal.site_ids:
        stmt = stmt.where(ManualRecordingSessionEntity.site_id.in_(principal.site_ids))
    if "admin" not in principal.roles:
        stmt = stmt.where(ManualRecordingSessionEntity.operator_subject == principal.subject)
    rows = list(
        (await session.execute(
            stmt.order_by(ManualRecordingSessionEntity.started_at.desc()).limit(limit)
        )).scalars()
    )
    now = datetime.now(timezone.utc)
    changed = False
    for row in rows:
        changed = await _finalize_if_expired(session, row, now) or changed
    if changed:
        await session.commit()
    return [_read(row) for row in rows]


@router.get("/{session_id}", response_model=ManualRecordingRead)
async def get_manual_recording(
    session_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Return one authorized durable manual-recording session."""
    row = await _owned_session(session, session_id, principal, lock=True)
    if await _finalize_if_expired(session, row, datetime.now(timezone.utc)):
        await session.commit()
    return _read(row)


@router.post("/{session_id}/stop", response_model=ManualRecordingRead)
async def stop_manual_recording(
    session_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Atomically finalize a manual-recording interval using server UTC."""
    row = await _owned_session(session, session_id, principal, lock=True)
    if row.state == "STOPPED":
        return _read(row)
    now = datetime.now(timezone.utc)
    row.state = "STOPPED"
    row.stopped_at = min(now, _max_stop(row))
    await session.commit()
    return _read(row)


@router.get("/{session_id}/export")
async def export_manual_recording(
    request: Request,
    session_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Authorize and export the immutable finalized manual interval via #291."""
    row = await _owned_session(session, session_id, principal, lock=True)
    if await _finalize_if_expired(session, row, datetime.now(timezone.utc)):
        await session.commit()
    if row.state != "STOPPED" or row.stopped_at is None:
        raise HTTPException(409, "Manual recording must be stopped before download")
    if row.camera_id is None:
        raise HTTPException(410, "Camera was deleted; manual session media is no longer addressable")
    camera = await authorized_camera(session, row.camera_id, principal)
    start, stop = _utc(row.started_at), _utc(row.stopped_at)
    return await stream_recording_clip(request, camera, start, (stop-start).total_seconds(), session)
