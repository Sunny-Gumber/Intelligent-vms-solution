import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import bindparam, cast, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from sqlalchemy.sql.dml import Update

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
_RULE_LOCK_TIMEOUT = "2000ms"
_RULE_STATEMENT_TIMEOUT = "5000ms"
_SQLITE_RULE_BUSY_TIMEOUT_MS = 2000
_SQLITE_POLICY_CLEANUP_SECONDS = 5
_RULE_CONFLICT_SQLSTATES = {"55P03", "40P01", "40001", "57014"}


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
    # Resolve only references visible to this caller. A missing or inaccessible
    # ID has the same outward denial, including mixed batches and legacy filters.
    query = select(CameraEntity).where(CameraEntity.id.in_(unique))
    if principal.tenant_id != "*":
        query = query.where(CameraEntity.tenant_id == principal.tenant_id)
    if "*" not in principal.site_ids:
        query = query.where(CameraEntity.site_id.is_(None) | CameraEntity.site_id.in_(principal.site_ids))
    rows = (await session.execute(query)).scalars().all()
    if len(rows) != len(unique):
        raise HTTPException(404, "Resource not found")
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


async def _restore_sqlite_policy(connection: AsyncConnection, previous_timeout: int) -> None:
    """Finish cleanup on the owned connection before it can return to the pool.

    Shield one bounded cleanup task, including repeated request cancellation.
    Restoration failure invalidates the physical connection; an already
    invalidated connection must never trigger a fresh checkout for restoration.
    """
    async def restore() -> None:
        try:
            async with asyncio.timeout(_SQLITE_POLICY_CLEANUP_SECONDS):
                await connection.rollback()
                if not connection.invalidated:
                    await connection.exec_driver_sql(f"PRAGMA busy_timeout = {previous_timeout}")
                    await connection.rollback()
        except BaseException:
            await connection.invalidate()
            raise

    cleanup = asyncio.create_task(restore())
    cancellation = None
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as error:
            cancellation = error
    cleanup.result()
    if cancellation is not None:
        raise cancellation


async def _commit_rule_change(session: AsyncSession, statement: Update) -> AlarmRuleRead:
    """Execute the conditional statement and construct the response before commit."""
    written = (await session.execute(statement)).scalar_one_or_none()
    if written is None:
        raise HTTPException(409, "Alarm rule changed; retry request")
    response = rule_read(written)
    await session.commit()
    return response


async def _sqlite_rule_change(session: AsyncSession, statement: Update) -> AlarmRuleRead:
    """Own the SQLite connection across transaction end and policy restoration."""
    engine = session.bind
    if not isinstance(engine, AsyncEngine):
        # Repository request sessions are engine-bound. Do not acquire an
        # arbitrary second connection for externally owned transaction sessions.
        raise HTTPException(503, "Alarm-rule transactional writes unavailable")
    # The conditional statement already captured every authorized value. Release
    # the read transaction before owning a write checkout, including pool_size=1.
    await session.rollback()
    async with engine.connect() as connection:
        previous_timeout = (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar_one()
        try:
            await connection.exec_driver_sql(f"PRAGMA busy_timeout = {_SQLITE_RULE_BUSY_TIMEOUT_MS}")
            # Connection ownership stays here even when the bound session commits
            # or rolls back. control_fully includes the PRAGMA's autobegun transaction.
            async with AsyncSession(bind=connection, expire_on_commit=False,
                                    join_transaction_mode="control_fully") as writer:
                return await _commit_rule_change(writer, statement)
        finally:
            await _restore_sqlite_policy(connection, previous_timeout)


async def _persist_rule_change(
    session: AsyncSession, row: AlarmRuleEntity, changes: dict[str, object]
) -> AlarmRuleRead:
    """Compare authorized scope and revision atomically with the persisted row.

    No ORM field is assigned before this conditional UPDATE. PostgreSQL
    rechecks its predicate after a competing writer commits; SQLite serializes
    writes and either rechecks or rejects a stale read transaction as busy.
    Returning zero rows is a conflict, never permission to retry blindly.
    """
    dialect = session.get_bind().dialect.name
    expected = bindparam("authorized_cameras", row.camera_ids_json,
                         type_=AlarmRuleEntity.camera_ids_json.type)
    if dialect == "postgresql":
        cameras_match = func.coalesce(
            cast(AlarmRuleEntity.camera_ids_json, JSONB), cast("null", JSONB)
        ) == cast(expected, JSONB)
    elif dialect == "sqlite":
        cameras_match = func.coalesce(
            func.json(AlarmRuleEntity.camera_ids_json), "null"
        ) == func.json(expected)
    else:
        # Supported deployments use PostgreSQL or SQLite. Do not silently
        # substitute an unprotected ORM write on another database.
        await session.rollback()
        raise HTTPException(503, "Alarm-rule transactional writes unavailable")

    # SQLite returns naive timestamps. Advance strictly even if the clock steps
    # backwards or two calls share a microsecond: every API write changes revision.
    observed_revision = row.updated_at.replace(tzinfo=timezone.utc) if row.updated_at.tzinfo is None else row.updated_at
    next_revision = max(datetime.now(timezone.utc), observed_revision + timedelta(microseconds=1))
    statement = (
        update(AlarmRuleEntity)
        .where(
            AlarmRuleEntity.id == row.id,
            AlarmRuleEntity.tenant_id == row.tenant_id,
            AlarmRuleEntity.site_id.is_not_distinct_from(row.site_id),
            cameras_match,
            AlarmRuleEntity.updated_at == row.updated_at,
        )
        .values(**changes, updated_at=next_revision)
        .returning(AlarmRuleEntity)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    try:
        if dialect == "postgresql":
            # Transaction-local settings also bound table-lock contention and
            # do not leak into a pooled connection's next request.
            await session.execute(select(func.set_config("lock_timeout", _RULE_LOCK_TIMEOUT, True)))
            await session.execute(select(func.set_config("statement_timeout", _RULE_STATEMENT_TIMEOUT, True)))
            return await _commit_rule_change(session, statement)
        return await _sqlite_rule_change(session, statement)
    except DBAPIError as error:
        await session.rollback()
        state = getattr(error.orig, "sqlstate", None)
        code = getattr(error.orig, "sqlite_errorcode", 0)
        if state in _RULE_CONFLICT_SQLSTATES or code & 255 in {5, 6}:
            raise HTTPException(409, "Alarm rule changed or busy; retry request") from None
        raise
    except BaseException:
        # Includes validation/conflict and request cancellation. The dependency
        # context additionally closes the session on every exit.
        await session.rollback()
        raise


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

    changes = {}
    for key, value in values.items():
        if key in {"event_types", "severities", "camera_ids"}:
            changes[f"{key}_json"] = sorted(set(value))
        else:
            changes[key] = value
    return await _persist_rule_change(session, row, changes)


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
    await _persist_rule_change(session, row, {"enabled": False})


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

    The load takes ``session.get(..., with_for_update=True)``. The transition
    is ``SET state='acknowledged' WHERE id=:id AND state='open'``, including
    the acknowledgement actor and timestamp. Zero rows are re-read; an already
    closed alarm returns HTTP 409. The loaded ORM object is not assigned, so a
    stale snapshot cannot overwrite close.

    Args:
        alarm_id: Alarm instance identifier.
        session: Database session used for lookup, scope check and persistence.
        principal: Authorized administrator or operator.

    Returns:
        Updated AlarmInstanceRead.

    Raises:
        HTTPException: If alarm is absent/out of scope or already closed.
    """
    # REV-033-101 / QA-033-101: the reproduction barrier hooks session.get.
    # session.execute(select(...).with_for_update()) does not take this lock.
    row = await session.get(AlarmInstanceEntity, alarm_id, with_for_update=True)
    if not row:
        raise HTTPException(404, "Alarm not found")
    require_scope(principal, row.tenant_id, row.site_id)
    if row.state == "closed":
        raise HTTPException(409, "Closed alarm cannot be acknowledged")
    if row.state != "acknowledged":
        acknowledged_at = datetime.now(timezone.utc)
        updated_id = (
            await session.execute(
                update(AlarmInstanceEntity)
                .where(
                    AlarmInstanceEntity.id == alarm_id,
                    AlarmInstanceEntity.state == "open",
                )
                .values(
                    state="acknowledged",
                    acknowledged_at=acknowledged_at,
                    acknowledged_by=principal.subject,
                )
                .returning(AlarmInstanceEntity.id)
                .execution_options(synchronize_session=False)
            )
        ).scalar_one_or_none()
        if updated_id is None:
            await session.refresh(row)
            if row.state == "closed":
                raise HTTPException(409, "Closed alarm cannot be acknowledged")
        else:
            await session.refresh(row)
            await session.commit()
    return instance_read(row)


@router.post("/{alarm_id}/close", response_model=AlarmInstanceRead)
async def close_alarm(
    alarm_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Close an alarm instance within the authorized scope.

    The load takes ``session.get(..., with_for_update=True)`` so this terminal
    write is serialized with acknowledge. Close still assigns ``closed`` after
    the lock and commits that state.

    Args:
        alarm_id: Alarm instance identifier.
        session: Database session used for lookup, scope check and persistence.
        principal: Authorized administrator or operator.

    Returns:
        Updated AlarmInstanceRead in closed state.

    Raises:
        HTTPException: If the alarm is absent or outside caller scope.
    """
    # REV-033-101 / QA-033-101: same session.get lock as acknowledge.
    row = await session.get(AlarmInstanceEntity, alarm_id, with_for_update=True)
    if not row:
        raise HTTPException(404, "Alarm not found")
    require_scope(principal, row.tenant_id, row.site_id)
    row.state = "closed"
    await session.commit()
    await session.refresh(row)
    return instance_read(row)
