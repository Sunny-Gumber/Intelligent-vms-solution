from datetime import datetime, timedelta, timezone

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, require_roles, require_scope
from app.core.config import settings
from app.db.session import get_session
from app.models.entities import EventHistoryEntity
from app.models.schemas import EventCenterRead, EventHistoryPage, EventIn, EventRead
from app.routers.cameras import authorized_camera
from app.services.event_search import EventSearchError, event_search
from app.services.outbox import OutboxPayloadTooLarge, enqueue_event_once

router = APIRouter(prefix="/api/v1/events", tags=["events"])
internal_router = APIRouter(prefix="/internal/v1/events", tags=["internal-events"])


def _validate_window(
    start: datetime | None,
    end: datetime | None,
    before: datetime | None = None,
) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    end = end or now
    start = start or (end - timedelta(hours=1))
    for name, value in (("start", start), ("end", end), ("before", before)):
        if value is not None and value.tzinfo is None:
            raise HTTPException(422, f"{name} must include a timezone")
    if end <= start:
        raise HTTPException(422, "end must be after start")
    if end - start > timedelta(hours=max(1, settings.event_query_max_window_hours)):
        raise HTTPException(422, "event query window exceeds configured maximum")
    if before is not None and (before <= start or before > end):
        raise HTTPException(422, "before must fall inside the requested event window")
    return start, end


async def _authorize_filters(
    session: AsyncSession,
    principal: Principal,
    site_id: str | None,
    camera_id: str | None,
) -> tuple[str | None, list[str] | None]:
    if site_id:
        require_scope(
            principal,
            principal.tenant_id if principal.tenant_id != "*" else "*",
            site_id,
        )
    if camera_id:
        camera = await authorized_camera(session, camera_id, principal)
        if site_id and camera.site_id != site_id:
            return "__none__", []
    allowed_sites = None if "*" in principal.site_ids or site_id else sorted(principal.site_ids)
    tenant = None if principal.tenant_id == "*" else principal.tenant_id
    return tenant, allowed_sites


def _read_local(row: EventHistoryEntity) -> EventRead:
    return EventRead(
        event_id=row.event_id,
        tenant_id=row.tenant_id,
        site_id=row.site_id,
        camera_id=row.camera_id,
        timestamp=row.timestamp,
        event_type=row.event_type,
        object_type=row.object_type,
        source=row.source,
        confidence=row.confidence,
        zone_id=row.zone_id,
        severity=row.severity,
        snapshot_uri=row.snapshot_uri,
        recording_start=row.recording_start,
        recording_end=row.recording_end,
        attributes=dict(row.attributes_json or {}),
        ingested_at=row.ingested_at,
    )


async def _persist_local_event(session: AsyncSession, event: EventIn) -> bool:
    existing = await session.get(EventHistoryEntity, event.event_id)
    if existing is not None:
        return False
    values = event.model_dump()
    attributes = values.pop("attributes", {})
    row = EventHistoryEntity(**values, attributes_json=attributes)
    session.add(row)
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=max(1, settings.event_local_retention_days)
    )
    await session.execute(delete(EventHistoryEntity).where(EventHistoryEntity.timestamp < cutoff))
    await session.commit()
    return True


async def _store_event(session: AsyncSession, event: EventIn) -> bool:
    if settings.event_local_store_enabled:
        return await _persist_local_event(session, event)
    if not settings.event_pipeline_enabled:
        raise HTTPException(503, "Event pipeline unavailable in deployment profile")
    payload = event.model_dump(mode="json")
    try:
        inserted = await enqueue_event_once(session, payload)
        await session.commit()
        return inserted
    except OutboxPayloadTooLarge as exc:
        await session.rollback()
        raise HTTPException(413, str(exc)) from exc


async def _search_local(
    session: AsyncSession,
    *,
    tenant_id: str | None,
    allowed_sites: list[str] | None,
    site_id: str | None,
    camera_id: str | None,
    event_type: str | None,
    severity: str | None,
    start: datetime,
    end: datetime,
    limit: int,
    before: datetime | None = None,
    before_id: str | None = None,
) -> list[EventRead]:
    q = select(EventHistoryEntity).where(
        EventHistoryEntity.timestamp >= start,
        EventHistoryEntity.timestamp < end,
    )
    if tenant_id is not None:
        q = q.where(EventHistoryEntity.tenant_id == tenant_id)
    if site_id is not None:
        q = q.where(EventHistoryEntity.site_id == site_id)
    elif allowed_sites is not None:
        if not allowed_sites:
            return []
        q = q.where(EventHistoryEntity.site_id.in_(allowed_sites))
    if camera_id is not None:
        q = q.where(EventHistoryEntity.camera_id == camera_id)
    if event_type is not None:
        q = q.where(EventHistoryEntity.event_type == event_type)
    if severity is not None:
        q = q.where(EventHistoryEntity.severity == severity)
    if before is not None:
        if before_id:
            q = q.where(
                or_(
                    EventHistoryEntity.timestamp < before,
                    and_(
                        EventHistoryEntity.timestamp == before,
                        EventHistoryEntity.event_id < before_id,
                    ),
                )
            )
        else:
            q = q.where(EventHistoryEntity.timestamp < before)
    q = q.order_by(
        EventHistoryEntity.timestamp.desc(), EventHistoryEntity.event_id.desc()
    ).limit(limit)
    rows = (await session.execute(q)).scalars().all()
    return [_read_local(row) for row in rows]


async def _search(
    session: AsyncSession,
    principal: Principal,
    *,
    start: datetime,
    end: datetime,
    site_id: str | None,
    camera_id: str | None,
    event_type: str | None,
    severity: str | None,
    limit: int,
    before: datetime | None = None,
    before_id: str | None = None,
) -> list[EventRead]:
    tenant, allowed_sites = await _authorize_filters(
        session, principal, site_id, camera_id
    )
    if tenant == "__none__":
        return []
    if settings.event_local_store_enabled:
        return await _search_local(
            session,
            tenant_id=tenant,
            allowed_sites=allowed_sites,
            site_id=site_id,
            camera_id=camera_id,
            event_type=event_type,
            severity=severity,
            start=start,
            end=end,
            limit=limit,
            before=before,
            before_id=before_id,
        )
    try:
        rows = await event_search.search(
            tenant_id=tenant,
            allowed_sites=allowed_sites,
            site_id=site_id,
            camera_id=camera_id,
            event_type=event_type,
            severity=severity,
            start=start,
            end=end,
            limit=limit,
            before_timestamp=before,
            before_event_id=before_id,
        )
    except EventSearchError as exc:
        raise HTTPException(503, str(exc)) from exc
    return [EventRead.model_validate(row) for row in rows]


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def ingest_event(
    event: EventIn,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "service")),
):
    """Persist one normalized event through the active authoritative store."""
    require_scope(principal, event.tenant_id, event.site_id)
    camera = await authorized_camera(session, event.camera_id, principal)
    if camera.tenant_id != event.tenant_id or camera.site_id != event.site_id:
        raise HTTPException(422, "Event camera does not match event tenant/site scope")
    inserted = await _store_event(session, event)
    return {"accepted": True, "event_id": event.event_id, "new": inserted}


@router.get("", response_model=list[EventRead])
async def search_events(
    start: datetime | None = None,
    end: datetime | None = None,
    site_id: str | None = None,
    camera_id: str | None = None,
    event_type: str | None = Query(default=None, min_length=1, max_length=128),
    severity: str | None = Query(
        default=None, pattern="^(info|low|medium|high|critical)$"
    ),
    limit: int = Query(default=100, ge=1),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """Search a bounded authorized event window, preserving the legacy list contract."""
    if not settings.event_history_enabled:
        raise HTTPException(503, "Event history unavailable in deployment profile")
    start, end = _validate_window(start, end)
    limit = min(limit, max(1, settings.event_query_max_limit))
    return await _search(
        session,
        principal,
        start=start,
        end=end,
        site_id=site_id,
        camera_id=camera_id,
        event_type=event_type,
        severity=severity,
        limit=limit,
    )


@router.get("/history", response_model=EventHistoryPage)
async def event_history_page(
    start: datetime | None = None,
    end: datetime | None = None,
    site_id: str | None = None,
    camera_id: str | None = None,
    event_type: str | None = Query(default=None, min_length=1, max_length=128),
    severity: str | None = Query(
        default=None, pattern="^(info|low|medium|high|critical)$"
    ),
    before: datetime | None = None,
    before_id: str | None = Query(default=None, min_length=1, max_length=128),
    limit: int = Query(default=100, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """Return one deterministic bounded page for Event Center clients."""
    if not settings.event_history_enabled:
        raise HTTPException(503, "Event history unavailable in deployment profile")
    start, end = _validate_window(start, end, before)
    if before_id and before is None:
        raise HTTPException(422, "before_id requires before")
    limit = min(limit, max(1, settings.event_query_max_limit), 200)
    rows = await _search(
        session,
        principal,
        start=start,
        end=end,
        site_id=site_id,
        camera_id=camera_id,
        event_type=event_type,
        severity=severity,
        limit=limit + 1,
        before=before,
        before_id=before_id,
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    safe_items = [
        EventCenterRead.model_validate(
            item.model_dump(exclude={"snapshot_uri"})
        )
        for item in items
    ]
    if has_more and items:
        last = items[-1]
        return EventHistoryPage(
            items=safe_items, next_before=last.timestamp, next_before_id=last.event_id
        )
    return EventHistoryPage(items=safe_items)


@internal_router.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
async def ingest_spooled_event(
    event: EventIn,
    x_regional_spool_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    """Accept an authenticated regional-spool event into the active store."""
    if not settings.regional_spool_token:
        raise HTTPException(503, "Regional spool token is not configured")
    if not x_regional_spool_token or not hmac.compare_digest(
        x_regional_spool_token, settings.regional_spool_token
    ):
        raise HTTPException(401, "Invalid regional spool token")
    return {
        "accepted": True,
        "event_id": event.event_id,
        "new": await _store_event(session, event),
    }
