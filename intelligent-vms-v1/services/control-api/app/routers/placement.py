from datetime import datetime, timezone
import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    Principal,
    require_global_admin,
    require_node_scope,
    require_roles,
    require_scope,
)
from app.db.session import get_session
from app.models.entities import CameraEntity
from app.models.placement import InfrastructureNodeEntity, PlacementAssignmentEntity, SiteRegionEntity
from app.models.placement_schemas import (
    NodeFenceSnapshotRead,
    NodeHeartbeat,
    NodeRead,
    NodeUpsert,
    PlacementRead,
    PlacementRunRead,
    RevocationAckRead,
    SiteRegionRead,
    SiteRegionUpdate,
)
from app.services.fencing import (
    FenceConflict,
    FenceSnapshotTooLarge,
    acknowledge_revocation,
    fence_snapshot,
)
from app.services.coordination import PlacementExecutionBusy, await_placement_execution_lock
from app.services.placement import persisted_role_readiness, run_placement_once

router = APIRouter(prefix="/api/v1/infrastructure", tags=["infrastructure"])
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _validate_node_id(node_id: str) -> None:
    if not _NODE_ID_RE.fullmatch(node_id):
        raise HTTPException(422, "Invalid infrastructure node id")


def node_read(row: InfrastructureNodeEntity) -> NodeRead:
    """Convert an infrastructure-node ORM row into the public response model.

    Args:
        row: Persisted infrastructure node to serialize.

    Returns:
        NodeRead containing roles, endpoints, capacity/load, role readiness
        and authority state.
    """
    return NodeRead(
        id=row.id, name=row.name, region_id=row.region_id, roles=list(row.roles_json or []),
        state=row.state, enabled=row.enabled, endpoints=row.endpoints_json or {},
        capacity=row.capacity_json or {}, load=row.load_json or {},
        role_readiness=row.role_readiness_json,
        heartbeat_at=row.heartbeat_at, authority_mode=row.authority_mode,
        generation=row.generation,
    )


@router.put("/nodes/{node_id}", response_model=NodeRead)
async def upsert_node(
    node_id: str,
    payload: NodeUpsert,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_global_admin()),
):
    """Create or update an infrastructure node and ownership generation metadata.

    Register, configure, and drain (state draining) share this route. There is
    no node-delete route. Only a global administrator (role admin and
    tenant_id "*") may call it.

    Args:
        node_id: Stable infrastructure-node identifier.
        payload: Validated node configuration/capacity.
        session: Database session used for persistence.
        principal: Global administrator (role admin and tenant_id "*").

    Returns:
        Persisted NodeRead.

    Raises:
        HTTPException: If the node identifier is invalid, or the placement fence
            is still held when the bounded wait expires.
    """
    _validate_node_id(node_id)
    try:
        await await_placement_execution_lock(session)
    except PlacementExecutionBusy as exc:
        raise HTTPException(
            409,
            "Placement ownership is changing; retry node update",
        ) from exc
    row = await session.get(InfrastructureNodeEntity, node_id)
    created = row is None
    if row is None:
        row = InfrastructureNodeEntity(id=node_id, name=payload.name, region_id=payload.region_id)
        session.add(row)
    elif row.region_id != payload.region_id or set(row.roles_json or []) != set(payload.roles):
        row.generation += 1
    row.name = payload.name
    row.region_id = payload.region_id
    row.roles_json = sorted(set(payload.roles))
    row.state = payload.state
    if created or principal.has_any_role("admin"):
        row.enabled = payload.enabled
    row.endpoints_json = dict(payload.endpoints)
    row.capacity_json = dict(payload.capacity)
    row.heartbeat_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(row)
    return node_read(row)


@router.post("/nodes/{node_id}/heartbeat", response_model=NodeRead)
async def heartbeat_node(
    node_id: str,
    payload: NodeHeartbeat,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_global_admin(allow_node_service=True)),
):
    """Update node load, authority mode and bounded heartbeat freshness.

    Args:
        node_id: Infrastructure node sending the heartbeat.
        payload: Validated load/authority/observation payload.
        session: Database session used to update node state.
        principal: Global administrator, or the service identity bound to this node.

    Returns:
        Updated NodeRead.

    Raises:
        HTTPException: If node identity/scope is invalid or the node is unregistered.
    """
    _validate_node_id(node_id)
    require_node_scope(principal, node_id)
    row = await session.get(InfrastructureNodeEntity, node_id)
    if not row:
        raise HTTPException(404, "Infrastructure node not registered")
    # FIX-014 lock order: this handler does not take the placement advisory
    # fence. Do not load this row and then wait on that fence. Readiness is
    # written on the same row as load and heartbeat_at.
    # FIX-042 overlap: draft PR #136 replaces these assignments with one
    # conditional UPDATE keyed by heartbeat_at. role_readiness_json belongs in
    # that UPDATE's values. Leaving it dirty on the ORM object lets a later
    # flush write readiness without the heartbeat_at predicate.
    now = datetime.now(timezone.utc)
    observed_at = payload.observed_at.astimezone(timezone.utc)
    # Delayed regional-spool delivery must not make stale telemetry look fresh.
    # Future node clocks are clamped so they cannot extend placement freshness.
    row.load_json = dict(payload.load)
    row.role_readiness_json = persisted_role_readiness(payload, row.roles_json)
    row.authority_mode = payload.authority_mode
    row.heartbeat_at = min(observed_at, now)
    await session.commit()
    await session.refresh(row)
    return node_read(row)




@router.get("/nodes/{node_id}/fences", response_model=NodeFenceSnapshotRead)
async def get_node_fences(
    node_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "service")),
):
    """Return active assignments and pending revocations for one node.

    Args:
        node_id: Infrastructure node requesting its fencing snapshot.
        session: Database session used to load node and fencing state.
        principal: Authorized administrator or node-scoped service identity.

    Returns:
        NodeFenceSnapshotRead for the requested node.

    Raises:
        HTTPException: If node identity/scope/registration is invalid or the
            bounded snapshot cannot be produced.
    """
    _validate_node_id(node_id)
    require_node_scope(principal, node_id)
    node = await session.get(InfrastructureNodeEntity, node_id)
    if not node:
        raise HTTPException(404, "Infrastructure node not registered")
    try:
        return NodeFenceSnapshotRead(**(await fence_snapshot(session, node_id)))
    except FenceSnapshotTooLarge as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post(
    "/nodes/{node_id}/fences/revocations/{revocation_id}/ack",
    response_model=RevocationAckRead,
)
async def ack_node_revocation(
    node_id: str,
    revocation_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_global_admin(allow_node_service=True)),
):
    """Acknowledge execution of a durable placement revocation.

    Args:
        node_id: Infrastructure node applying the revocation.
        revocation_id: Durable revocation identifier.
        session: Database session used to validate/persist acknowledgement.
        principal: Global administrator, or the service identity bound to this node.

    Returns:
        RevocationAckRead confirming acknowledgement.

    Raises:
        HTTPException: If node scope is invalid, the revocation conflicts or is absent.
    """
    _validate_node_id(node_id)
    require_node_scope(principal, node_id)
    try:
        acknowledged = await acknowledge_revocation(
            session,
            node_id,
            revocation_id,
        )
    except FenceConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if not acknowledged:
        raise HTTPException(404, "Revocation not found")
    await session.commit()
    return RevocationAckRead(
        revocation_id=revocation_id,
        acknowledged=True,
    )


@router.get("/nodes", response_model=list[NodeRead])
async def list_nodes(
    region_id: str | None = None,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin")),
):
    """List registered infrastructure nodes, optionally filtered by region.

    Args:
        region_id: Optional region identifier filter.
        session: Database session used to query nodes.
        principal: Authorized administrator.

    Returns:
        NodeRead list ordered by region and node identifier.
    """
    q = select(InfrastructureNodeEntity).order_by(InfrastructureNodeEntity.region_id, InfrastructureNodeEntity.id)
    if region_id:
        q = q.where(InfrastructureNodeEntity.region_id == region_id)
    return [node_read(x) for x in (await session.execute(q)).scalars().all()]


@router.put("/sites/region", response_model=SiteRegionRead)
async def set_site_region(
    payload: SiteRegionUpdate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Create or update the region assignment for an authorized site.

    Args:
        payload: Validated tenant/site/region assignment.
        session: Database session used for lookup and persistence.
        principal: Authorized administrator or operator.

    Returns:
        Persisted SiteRegionRead.

    Raises:
        HTTPException: If tenant/site scope authorization fails, or the placement
            fence is still held when the bounded wait expires.
    """
    require_scope(principal, payload.tenant_id, payload.site_id)
    try:
        await await_placement_execution_lock(session)
    except PlacementExecutionBusy as exc:
        raise HTTPException(
            409,
            "Placement ownership is changing; retry site region update",
        ) from exc
    row = (
        await session.execute(
            select(SiteRegionEntity).where(
                SiteRegionEntity.tenant_id == payload.tenant_id,
                SiteRegionEntity.site_id == payload.site_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = SiteRegionEntity(tenant_id=payload.tenant_id, site_id=payload.site_id, region_id=payload.region_id)
        session.add(row)
    else:
        row.region_id = payload.region_id
    await session.commit()
    await session.refresh(row)
    return SiteRegionRead(id=row.id, tenant_id=row.tenant_id, site_id=row.site_id, region_id=row.region_id)


@router.get("/placements", response_model=list[PlacementRead])
async def list_placements(
    camera_id: str | None = None,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """List placement assignments within caller tenant/site scope.

    Args:
        camera_id: Optional authorized camera filter.
        session: Database session used for scoped placement query.
        principal: Authorized administrator or operator.

    Returns:
        Bounded PlacementRead list.

    Raises:
        HTTPException: If a requested camera is absent or outside caller scope.
    """
    q = (
        select(PlacementAssignmentEntity)
        .join(CameraEntity, CameraEntity.id == PlacementAssignmentEntity.camera_id)
        .order_by(PlacementAssignmentEntity.camera_id, PlacementAssignmentEntity.role)
    )
    if principal.tenant_id != "*":
        q = q.where(CameraEntity.tenant_id == principal.tenant_id)
    if "*" not in principal.site_ids:
        q = q.where(CameraEntity.site_id.in_(principal.site_ids))
    if camera_id:
        camera = await session.get(CameraEntity, camera_id)
        if not camera:
            raise HTTPException(404, "Camera not found")
        require_scope(principal, camera.tenant_id, camera.site_id)
        q = q.where(PlacementAssignmentEntity.camera_id == camera_id)
    rows = (await session.execute(q.limit(2000))).scalars().all()
    return [
        PlacementRead(
            camera_id=row.camera_id, role=row.role, region_id=row.region_id, node_id=row.node_id,
            cleanup_node_ids=list(row.cleanup_node_ids_json or []),
            generation=row.generation, applied_generation=row.applied_generation,
            active=row.active, reason=row.reason,
            lease_expires_at=row.lease_expires_at,
            autonomy_expires_at=row.autonomy_expires_at,
            assigned_at=row.assigned_at,
        )
        for row in rows
    ]


@router.post("/placement/run", response_model=PlacementRunRead)
async def run_placement(
    principal: Principal = Depends(require_global_admin()),
):
    """Run one bounded global placement-controller iteration.

    Assignment and failover both happen inside this run. Only a global
    administrator (role admin and tenant_id "*") may start it.

    Args:
        principal: Global administrator (role admin and tenant_id "*").

    Returns:
        PlacementRunRead containing scan, move, unplaced, deferred, and renewed
        counts, whether the live population exceeded the renewal ceiling,
        whether every lock attempt failed, and the cursor.

    Raises:
        Exception: Placement service failures propagate to the API error handler.
    """
    return PlacementRunRead(**(await run_placement_once()))
