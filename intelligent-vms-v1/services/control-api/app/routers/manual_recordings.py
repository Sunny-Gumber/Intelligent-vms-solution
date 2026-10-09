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
from app.routers.cameras import _lock_camera_row, authorized_camera
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


def _session_lock_order(rows: list[ManualRecordingSessionEntity]) -> list[ManualRecordingSessionEntity]:
    """Return sessions in primary-key order before taking row locks.

    The active list displays oldest-first and the recent list displays
    newest-first. Finalizing in those display orders would lock the same
    sessions from opposite ends.
    """
    return sorted(rows, key=lambda row: row.id)


async def _finalize_if_expired(session: AsyncSession, row: ManualRecordingSessionEntity, now: datetime) -> bool:
    """Stop an expired session only when a locked re-read is still ACTIVE.

    List selects return before an explicit stop commits. ``FOR UPDATE``
    re-reads that commit, and ``stopped_at`` is assigned only while the
    locked row is still ``ACTIVE``. The stale list object is not assigned,
    so flush cannot replace the earlier stop with ``max_stop_at``.

    The lock is this session row only. Manual start already holds the camera
    row, and camera delete already holds the placement fence and then the
    camera row. This function does not acquire either of those locks, so it
    cannot invert that order. SQLite has no ``FOR UPDATE`` clause; the same
    re-read runs without it.
    """
    if row.state != "ACTIVE" or _max_stop(row) > now:
        return False
    locked = await session.get(
        ManualRecordingSessionEntity,
        row.id,
        populate_existing=True,
        with_for_update=session.get_bind().dialect.name == "postgresql",
    )
    if locked is None or locked.state != "ACTIVE" or _max_stop(locked) > now:
        return False
    row = locked
    row.state = "STOPPED"
    row.stopped_at = _max_stop(row)
    await session.flush()
    return True


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
    """Persist an idempotent server-timed manual-recording intent.

    The camera row is locked before the policy read and the session insert so
    that insert cannot commit between a concurrent delete's active-session
    stop and the camera delete.
    """
    camera = await authorized_camera(session, camera_id, principal)
    camera = await _lock_camera_row(session, camera.id)
    if camera is None:
        raise HTTPException(404, "Camera not found")
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
    """Return currently active manual intents visible to this principal.

    Expiry goes through ``_finalize_if_expired``, which locks each session row
    and writes ``stopped_at`` only while that locked row is still ``ACTIVE``.
    An earlier explicit stop keeps its timestamp. Session locks are taken in
    id order and do not include the camera row or the placement fence.

    Args:
        session: Database session for this request.
        principal: Admin or operator whose tenant, site, and ownership filter the list.

    Returns:
        Sessions that are still ``ACTIVE`` after expiry, oldest start first.
    """
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
    for row in _session_lock_order(rows):
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
    """Return a bounded recent manual-session history for recovery and retry.

    The same expiry guard as the active list re-reads each overdue session
    under its row lock and keeps an earlier committed ``stopped_at``.

    Args:
        limit: Maximum number of sessions to return, newest start first.
        session: Database session for this request.
        principal: Admin or operator whose tenant, site, and ownership filter the list.

    Returns:
        Recent sessions after expiry, newest start first.
    """
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
    for row in _session_lock_order(rows):
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
