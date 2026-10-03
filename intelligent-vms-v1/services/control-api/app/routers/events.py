from datetime import datetime, timedelta, timezone

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, require_roles, require_scope
from app.db.session import get_session
from app.models.schemas import EventIn, EventRead
from app.routers.cameras import authorized_camera
from app.services.event_search import EventSearchError, event_search
from app.services.outbox import OutboxPayloadTooLarge, enqueue_event_once
from app.core.config import settings

router = APIRouter(prefix="/api/v1/events", tags=["events"])
internal_router = APIRouter(prefix="/internal/v1/events", tags=["internal-events"])


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def ingest_event(
    event: EventIn,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "service")),
):
    """Persist an authorized event through the transactional outbox.

    Args:
        event: Validated normalized event.
        session: Database session used for outbox persistence.
        principal: Authorized administrator, operator or service identity.

    Returns:
        Acceptance object with event identifier and dedupe result.

    Raises:
        HTTPException: If scope authorization or payload-size bounds fail.
    """
    require_scope(principal, event.tenant_id, event.site_id)
    payload = event.model_dump(mode="json")
    try:
        inserted = await enqueue_event_once(session, payload)
        await session.commit()
    except OutboxPayloadTooLarge as exc:
        await session.rollback()
        raise HTTPException(413, str(exc)) from exc
    return {"accepted": True, "event_id": event.event_id, "new": inserted}


@router.get("", response_model=list[EventRead])
async def search_events(
    start: datetime | None = None,
    end: datetime | None = None,
    site_id: str | None = None,
    camera_id: str | None = None,
    event_type: str | None = None,
    severity: str | None = Query(default=None, pattern="^(info|low|medium|high|critical)$"),
    limit: int = Query(default=100, ge=1),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """Search events inside bounded time and authorization scope.

    Args:
        start: Optional timezone-aware search start.
        end: Optional timezone-aware search end.
        site_id: Optional authorized site filter.
        camera_id: Optional authorized camera filter.
        event_type: Optional event-type filter.
        severity: Optional bounded severity filter.
        limit: Requested result count, capped by service configuration.
        session: Database session used for camera authorization.
        principal: Authenticated administrator, operator or viewer.

    Returns:
        EventRead-compatible rows from the event search backend.

    Raises:
        HTTPException: If time/scope validation fails or event search is unavailable.
    """
    now = datetime.now(timezone.utc)
    end = end or now
    start = start or (end - timedelta(hours=24))
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(422, "start and end must include a timezone")
    if end <= start:
        raise HTTPException(422, "end must be after start")
    if end - start > timedelta(hours=max(1, settings.event_query_max_window_hours)):
        raise HTTPException(422, "event query window exceeds configured maximum")
    limit = min(limit, max(1, settings.event_query_max_limit))

    if site_id:
        require_scope(principal, principal.tenant_id if principal.tenant_id != "*" else "*", site_id)
    if camera_id:
        camera = await authorized_camera(session, camera_id, principal)
        if site_id and camera.site_id != site_id:
            return []

    allowed_sites = None if "*" in principal.site_ids or site_id else sorted(principal.site_ids)
    tenant = None if principal.tenant_id == "*" else principal.tenant_id
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
        )
    except EventSearchError as exc:
        raise HTTPException(503, str(exc)) from exc
    return rows


@internal_router.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
async def ingest_spooled_event(
    event: EventIn,
    x_regional_spool_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    """Accept a regional-spool event into the transactional outbox.

    Args:
        event: Validated normalized event.
        x_regional_spool_token: Shared regional-spool authentication token.
        session: Database session used for outbox persistence.

    Returns:
        Acceptance object with event identifier and dedupe result.

    Raises:
        HTTPException: If spool authentication/configuration or payload bounds fail.
    """
    if not settings.regional_spool_token:
        raise HTTPException(503, "Regional spool token is not configured")
    if not x_regional_spool_token or not hmac.compare_digest(
        x_regional_spool_token,
        settings.regional_spool_token,
    ):
        raise HTTPException(401, "Invalid regional spool token")
    payload = event.model_dump(mode="json")
    try:
        inserted = await enqueue_event_once(session, payload)
        await session.commit()
    except OutboxPayloadTooLarge as exc:
        await session.rollback()
        raise HTTPException(413, str(exc)) from exc
    return {"accepted": True, "event_id": event.event_id, "new": inserted}
