from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, require_roles, require_scope
from app.core.config import settings
from app.db.session import get_session
from app.models.entities import AlarmInstanceEntity, AlarmRuleEntity, CameraEntity
from app.models.schemas import (
    AlarmInstanceRead,
    AlarmRuleCreate,
    AlarmRuleRead,
    AlarmRuleUpdate,
)

def _require_alarm_processing() -> None:
    """Reject alarm APIs when the active deployment profile has no alarm processor."""
    if not settings.alarm_processing_enabled:
        raise HTTPException(503, "Alarm processing unavailable in deployment profile")


router = APIRouter(
    prefix="/api/v1/alarms",
    tags=["alarms"],
    dependencies=[Depends(_require_alarm_processing)],
)

_MAX_RULE_CAMERAS = 1000


def rule_read(row: AlarmRuleEntity) -> AlarmRuleRead:
    """Convert an alarm-rule ORM row into its public response model.

    Args:
        row: Alarm rule row to serialize.

    Returns:
        AlarmRuleRead with normalized list fields.
    """
    return AlarmRuleRead(
        id=row.id,
        tenant_id=row.tenant_id,
        site_id=row.site_id,
        name=row.name,
        enabled=row.enabled,
        event_types=list(row.event_types_json or []),
        severities=list(row.severities_json or []),
        camera_ids=list(row.camera_ids_json or []),
        alarm_severity=row.alarm_severity,
        cooldown_seconds=row.cooldown_seconds,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def instance_read(row: AlarmInstanceEntity) -> AlarmInstanceRead:
    """Convert an alarm-instance ORM row into its public response model.

    Args:
        row: Alarm instance row to serialize.

    Returns:
        AlarmInstanceRead containing lifecycle and acknowledgement state.
    """
    return AlarmInstanceRead(
        id=row.id,
        rule_id=row.rule_id,
        event_id=row.event_id,
        tenant_id=row.tenant_id,
        site_id=row.site_id,
        camera_id=row.camera_id,
        event_type=row.event_type,
        severity=row.severity,
        state=row.state,
        message=row.message,
        opened_at=row.opened_at,
        last_event_at=row.last_event_at,
        acknowledged_at=row.acknowledged_at,
        acknowledged_by=row.acknowledged_by,
    )


async def _validate_cameras(
    session: AsyncSession,
    camera_ids: list[str],
    tenant_id: str,
    site_id: str | None,
    principal: Principal,
) -> None:
    # Persisted JSON can predate current API bounds; fail closed before querying.
    if (
        not isinstance(camera_ids, list)
        or len(camera_ids) > _MAX_RULE_CAMERAS
        or any(not isinstance(camera_id, str) or not camera_id for camera_id in camera_ids)
    ):
        raise HTTPException(422, "Invalid alarm-rule camera filter")
    if not camera_ids:
        return
    unique = sorted(set(camera_ids))
    rows = (
        await session.execute(select(CameraEntity).where(CameraEntity.id.in_(unique)))
    ).scalars().all()
    if len(rows) != len(unique):
        raise HTTPException(422, "One or more alarm-rule camera IDs do not exist")
    for camera in rows:
        require_scope(principal, camera.tenant_id, camera.site_id)
        if camera.tenant_id != tenant_id:
            raise HTTPException(422, "Alarm-rule cameras must belong to the rule tenant")
        if site_id is not None and camera.site_id != site_id:
            raise HTTPException(422, "Alarm-rule cameras must belong to the rule site")


async def _require_rule_write_scope(
    session: AsyncSession,
    tenant_id: str,
    site_id: str | None,
    camera_ids: list[str],
    principal: Principal,
) -> None:
    """Authorize complete matching coverage separately from shared read visibility.

    Empty camera filters are wildcard coverage. A null-site wildcard therefore
    needs all-site entitlement, while explicit cameras must each be authorized.
    Camera validation is batched and performed even for all-site identities so
    missing/moved references cannot silently establish mutation authority.
    """
    require_scope(principal, tenant_id, site_id)
    if site_id is None and not camera_ids and "*" not in principal.site_ids:
        raise HTTPException(404, "Resource not found")
    await _validate_cameras(session, camera_ids, tenant_id, site_id, principal)


@router.post("/rules", response_model=AlarmRuleRead, status_code=status.HTTP_201_CREATED)
async def create_rule(
    payload: AlarmRuleCreate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Create a scoped alarm-matching rule.

    Args:
        payload: Validated rule filters and alarm settings.
        session: Database session used for camera validation and persistence.
        principal: Authorized administrator or operator.

    Returns:
        Persisted AlarmRuleRead.

    Raises:
        HTTPException: If scope or referenced camera validation fails.
    """
    await _require_rule_write_scope(
        session, payload.tenant_id, payload.site_id, payload.camera_ids, principal
    )
    row = AlarmRuleEntity(
        tenant_id=payload.tenant_id,
        site_id=payload.site_id,
        name=payload.name,
        enabled=payload.enabled,
        event_types_json=sorted(set(payload.event_types)),
        severities_json=sorted(set(payload.severities)),
        camera_ids_json=sorted(set(payload.camera_ids)),
        alarm_severity=payload.alarm_severity,
        cooldown_seconds=payload.cooldown_seconds,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return rule_read(row)


@router.get("/rules", response_model=list[AlarmRuleRead])
async def list_rules(
    site_id: str | None = None,
    enabled: bool | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """List alarm rules visible to the caller scope.

    Args:
        site_id: Optional site filter.
        enabled: Optional rule-enabled filter.
        limit: Bounded result limit.
        session: Database session used to query rules.
        principal: Authenticated administrator, operator or viewer.

    Returns:
        AlarmRuleRead list ordered newest first.

    Raises:
        HTTPException: If a requested site is outside caller scope.
    """
    q = select(AlarmRuleEntity).order_by(AlarmRuleEntity.created_at.desc()).limit(limit)
    if principal.tenant_id != "*":
        q = q.where(AlarmRuleEntity.tenant_id == principal.tenant_id)
    if site_id:
        require_scope(principal, principal.tenant_id if principal.tenant_id != "*" else "*", site_id)
        q = q.where(AlarmRuleEntity.site_id == site_id)
    elif "*" not in principal.site_ids:
        q = q.where(
            (AlarmRuleEntity.site_id.is_(None))
            | (AlarmRuleEntity.site_id.in_(principal.site_ids))
        )
    if enabled is not None:
        q = q.where(AlarmRuleEntity.enabled.is_(enabled))
    return [rule_read(row) for row in (await session.execute(q)).scalars().all()]


async def _authorized_rule(
    session: AsyncSession, rule_id: str, principal: Principal
) -> AlarmRuleEntity:
    row = await session.get(AlarmRuleEntity, rule_id)
    if not row:
        raise HTTPException(404, "Alarm rule not found")
    await _require_rule_write_scope(
        session, row.tenant_id, row.site_id,
        row.camera_ids_json if row.camera_ids_json is not None else [], principal,
    )
    return row


@router.patch("/rules/{rule_id}", response_model=AlarmRuleRead)
async def update_rule(
    rule_id: str,
    payload: AlarmRuleUpdate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Apply a partial update to an authorized alarm rule.

    Args:
        rule_id: Alarm rule identifier to update.
        payload: Validated partial rule changes.
        session: Database session used for authorization/camera validation/persistence.
        principal: Authorized administrator or operator.

    Returns:
        Updated AlarmRuleRead.

    Raises:
        HTTPException: If the rule is absent/out of scope or camera filters are invalid.
    """
    row = await _authorized_rule(session, rule_id, principal)
    values = payload.model_dump(exclude_unset=True)
    camera_ids = values.get("camera_ids", list(row.camera_ids_json or []))
    # Validate the full proposed scope before assigning any ORM field. Existing
    # scope was independently authorized above; narrowing cannot take it over.
    await _require_rule_write_scope(session, row.tenant_id, row.site_id, camera_ids, principal)

    if "name" in values:
        row.name = values["name"]
    if "enabled" in values:
        row.enabled = values["enabled"]
    if "event_types" in values:
        row.event_types_json = sorted(set(values["event_types"]))
    if "severities" in values:
        row.severities_json = sorted(set(values["severities"]))
    if "camera_ids" in values:
        row.camera_ids_json = sorted(set(values["camera_ids"]))
    if "alarm_severity" in values:
        row.alarm_severity = values["alarm_severity"]
    if "cooldown_seconds" in values:
        row.cooldown_seconds = values["cooldown_seconds"]

    await session.commit()
    await session.refresh(row)
    return rule_read(row)


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(
    rule_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin")),
):
    """Soft-disable an authorized alarm rule while retaining audit references.

    Args:
        rule_id: Alarm rule identifier to disable.
        session: Database session used for authorization and persistence.
        principal: Authorized administrator.

    Returns:
        No response body on successful HTTP 204 completion.

    Raises:
        HTTPException: If the rule is absent or outside caller scope.
    """
    row = await _authorized_rule(session, rule_id, principal)
    # Rules are soft-disabled so historical alarm instances retain their
    # referential/audit context.
    row.enabled = False
    await session.commit()


@router.get("", response_model=list[AlarmInstanceRead])
async def list_alarms(
    state: str | None = Query(default=None, pattern="^(open|acknowledged|closed)$"),
    site_id: str | None = None,
    camera_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """List alarm instances within tenant/site/camera scope.

    Args:
        state: Optional alarm lifecycle-state filter.
        site_id: Optional site filter.
        camera_id: Optional authorized camera filter.
        limit: Bounded maximum result count.
        session: Database session used for scope checks and query.
        principal: Authenticated administrator, operator or viewer.

    Returns:
        AlarmInstanceRead list ordered newest first.

    Raises:
        HTTPException: If a requested site/camera is absent or outside scope.
    """
    q = select(AlarmInstanceEntity).order_by(AlarmInstanceEntity.opened_at.desc()).limit(limit)
    if principal.tenant_id != "*":
        q = q.where(AlarmInstanceEntity.tenant_id == principal.tenant_id)
    if site_id:
        require_scope(principal, principal.tenant_id if principal.tenant_id != "*" else "*", site_id)
        q = q.where(AlarmInstanceEntity.site_id == site_id)
    elif "*" not in principal.site_ids:
        q = q.where(AlarmInstanceEntity.site_id.in_(principal.site_ids))
    if camera_id:
        camera = await session.get(CameraEntity, camera_id)
        if not camera:
            raise HTTPException(404, "Camera not found")
        require_scope(principal, camera.tenant_id, camera.site_id)
        q = q.where(AlarmInstanceEntity.camera_id == camera_id)
    if state:
        q = q.where(AlarmInstanceEntity.state == state)
    return [instance_read(row) for row in (await session.execute(q)).scalars().all()]


@router.post("/{alarm_id}/acknowledge", response_model=AlarmInstanceRead)
async def acknowledge_alarm(
    alarm_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Acknowledge an open alarm for the authorized scope.

    Args:
        alarm_id: Alarm instance identifier.
        session: Database session used for lookup, scope check and persistence.
        principal: Authorized administrator or operator.

    Returns:
        Updated AlarmInstanceRead.

    Raises:
        HTTPException: If alarm is absent/out of scope or already closed.
    """
    row = await session.get(AlarmInstanceEntity, alarm_id)
    if not row:
        raise HTTPException(404, "Alarm not found")
    require_scope(principal, row.tenant_id, row.site_id)
    if row.state == "closed":
        raise HTTPException(409, "Closed alarm cannot be acknowledged")
    if row.state != "acknowledged":
        row.state = "acknowledged"
        row.acknowledged_at = datetime.now(timezone.utc)
        row.acknowledged_by = principal.subject
        await session.commit()
        await session.refresh(row)
    return instance_read(row)


@router.post("/{alarm_id}/close", response_model=AlarmInstanceRead)
async def close_alarm(
    alarm_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Close an alarm instance within the authorized scope.

    Args:
        alarm_id: Alarm instance identifier.
        session: Database session used for lookup, scope check and persistence.
        principal: Authorized administrator or operator.

    Returns:
        Updated AlarmInstanceRead in closed state.

    Raises:
        HTTPException: If the alarm is absent or outside caller scope.
    """
    row = await session.get(AlarmInstanceEntity, alarm_id)
    if not row:
        raise HTTPException(404, "Alarm not found")
    require_scope(principal, row.tenant_id, row.site_id)
    row.state = "closed"
    await session.commit()
    await session.refresh(row)
    return instance_read(row)
