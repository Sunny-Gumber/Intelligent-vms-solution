from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import select

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.stream_keys import make_role_stream_key
from app.services.coordination import try_placement_execution_lock
from app.models.entities import CameraAIPolicyEntity, CameraEntity, RecordingPolicyEntity, ServiceStateEntity
from app.models.placement import (
    InfrastructureNodeEntity,
    PlacementAssignmentEntity,
    PlacementRevocationEntity,
    SiteRegionEntity,
)

CURSOR_KEY = "placement_controller_cursor"


@dataclass(frozen=True)
class NodeSnapshot:
    """Immutable infrastructure-node state used by placement decisions.

    Attributes:
        id: Infrastructure node identifier.
        region_id: Placement region served by the node.
        roles: Roles the node can execute.
        state: Operational node state.
        enabled: Whether the node accepts placement.
        capacity: Configured capacity dimensions.
        load: Reported current load dimensions.
        heartbeat_at: Last trusted heartbeat timestamp.
        authority_mode: Current fencing/authority mode.
    """

    id: str
    region_id: str
    roles: frozenset[str]
    state: str
    enabled: bool
    capacity: dict
    load: dict
    heartbeat_at: datetime
    authority_mode: str = "central_online"


def _num(data: dict, key: str) -> float:
    try:
        return max(0.0, float(data.get(key, 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def role_utilization(node: NodeSnapshot, role: str) -> tuple[float, float] | None:
    """Calculate conservative utilization for one placement role.

    Args:
        node: Candidate infrastructure-node snapshot.
        role: Placement role: media, recording or ai.

    Returns:
        Tuple of worst capacity ratio and active-count load, or None when the
        role/capacity/load data is insufficient for safe placement.
    """
    c, l = node.capacity, node.load
    if role == "media":
        dimensions = [
            ("max_ingress_mbps", "ingress_mbps"),
            ("max_egress_mbps", "egress_mbps"),
            ("max_sources", "active_sources"),
        ]
        active_key = "active_sources"
    elif role == "recording":
        dimensions = [
            ("max_record_mbps", "record_mbps"),
            ("max_recordings", "active_recordings"),
        ]
        active_key = "active_recordings"
    elif role == "ai":
        dimensions = [
            ("max_ai_mpix_s", "ai_mpix_s"),
            ("max_ai_jobs", "active_ai_jobs"),
        ]
        active_key = "active_ai_jobs"
    else:
        return None

    ratios = []
    for maximum_key, used_key in dimensions:
        maximum = _num(c, maximum_key)
        if maximum <= 0:
            continue
        if used_key not in l:
            # A configured safety limit without a trustworthy measured load
            # must not be treated as zero utilization.
            return None
        ratios.append(_num(l, used_key) / maximum)
    if not ratios or active_key not in l:
        return None
    return max(ratios), _num(l, active_key)


def node_eligible(node: NodeSnapshot, role: str, region_id: str, now: datetime) -> bool:
    """Return whether a node is safe and eligible for one placement role.

    Args:
        node: Candidate infrastructure-node snapshot.
        role: Required placement role.
        region_id: Required placement region.
        now: Controller evaluation time.

    Returns:
        True only when role, region, freshness, authority mode and headroom pass.
    """
    if not node.enabled or node.state != "active" or role not in node.roles:
        return False
    # A node that has not re-synchronized its fence snapshot must not receive
    # renewed central lease/autonomy authority merely because delayed telemetry
    # reached the control plane.
    if node.authority_mode != "central_online":
        return False
    if node.region_id != region_id:
        return False
    heartbeat = node.heartbeat_at
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=timezone.utc)
    if (now - heartbeat).total_seconds() > settings.placement_node_stale_seconds:
        return False
    utilization = role_utilization(node, role)
    return utilization is not None and utilization[0] < settings.placement_headroom


def choose_node(nodes: Iterable[NodeSnapshot], role: str, region_id: str, now: datetime) -> NodeSnapshot | None:
    """Choose the least-utilized deterministic eligible node.

    Args:
        nodes: Candidate node snapshots.
        role: Required placement role.
        region_id: Required placement region.
        now: Controller evaluation time.

    Returns:
        Selected node, or None when no candidate is safely eligible.
    """
    eligible = [node for node in nodes if node_eligible(node, role, region_id, now)]
    if not eligible:
        return None
    return min(eligible, key=lambda n: (*role_utilization(n, role), n.id))


def autonomy_deadline(now: datetime) -> datetime | None:
    """Calculate the pre-granted offline autonomy deadline.

    Args:
        now: Controller time from which autonomy is granted.

    Returns:
        Deadline datetime, or None when offline autonomy is disabled.
    """
    seconds = max(0, int(settings.placement_offline_autonomy_seconds))
    if seconds <= 0:
        return None
    return now + timedelta(seconds=seconds)


def autonomy_active(row: PlacementAssignmentEntity | None, now: datetime) -> bool:
    """Return whether an assignment still owns valid pre-granted autonomy.

    Args:
        row: Existing placement assignment, if any.
        now: Controller evaluation time.

    Returns:
        True when the assignment autonomy deadline remains in the future.
    """
    if row is None or row.autonomy_expires_at is None:
        return False
    expiry = row.autonomy_expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry > now


def project_assignment(node: NodeSnapshot, role: str) -> None:
    """Project count-based load inside one controller run.

    Mbps/MPix demand is not guessed. Hardware qualification later supplies measured
    demand; count projection prevents a fresh cluster from assigning every camera
    to the same zero-load node in a single batch.

    Args:
        node: Selected node snapshot whose projected load is updated.
        role: Placement role being assigned.

    Returns:
        None after the in-memory projected count is updated when supported.
    """
    key = {
        "media": "active_sources",
        "recording": "active_recordings",
        "ai": "active_ai_jobs",
    }.get(role)
    if key:
        node.load[key] = _num(node.load, key) + 1.0


async def _leader_lock(session) -> bool:
    return await try_placement_execution_lock(session)


async def _assign(
    session,
    camera: CameraEntity,
    role: str,
    region_id: str,
    node: NodeSnapshot,
    row: PlacementAssignmentEntity | None,
    *,
    reason: str,
    now: datetime | None = None,
) -> tuple[PlacementAssignmentEntity, bool]:
    now = now or datetime.now(timezone.utc)
    lease = now + timedelta(seconds=settings.placement_lease_seconds)
    autonomy = autonomy_deadline(now)
    changed = row is None or row.node_id != node.id or row.region_id != region_id
    if row is None:
        row = PlacementAssignmentEntity(
            camera_id=camera.id,
            role=role,
            region_id=region_id,
            node_id=node.id,
            generation=1,
            active=True,
            reason=reason,
            lease_expires_at=lease,
            autonomy_expires_at=autonomy,
            assigned_at=now,
        )
        session.add(row)
    else:
        if changed:
            old_node_id = row.node_id
            old_generation = row.generation
            ownership_changed = old_node_id != node.id

            if session is not None and ownership_changed:
                valid_until = row.lease_expires_at
                if row.autonomy_expires_at is not None:
                    autonomy_until = row.autonomy_expires_at
                    if autonomy_until.tzinfo is None:
                        autonomy_until = autonomy_until.replace(tzinfo=timezone.utc)
                    lease_until = valid_until
                    if lease_until.tzinfo is None:
                        lease_until = lease_until.replace(tzinfo=timezone.utc)
                    if autonomy_until > lease_until:
                        valid_until = autonomy_until

                session.add(
                    PlacementRevocationEntity(
                        assignment_id=row.id,
                        camera_id=row.camera_id,
                        role=row.role,
                        node_id=old_node_id,
                        revoked_generation=old_generation,
                        execution_keys_json=(
                            [
                                camera.stream_key,
                                *(
                                    [make_role_stream_key(camera.stream_key, "main")]
                                    if camera.sub_path
                                    else []
                                ),
                                *(
                                    [camera.third_stream_key]
                                    if camera.third_stream_key
                                    else []
                                ),
                            ]
                            if row.role == "media"
                            else []
                        ),
                        reason=reason,
                        valid_until=valid_until,
                    )
                )

                pending_for_new_owner = (
                    await session.execute(
                        select(PlacementRevocationEntity).where(
                            PlacementRevocationEntity.assignment_id == row.id,
                            PlacementRevocationEntity.node_id == node.id,
                            PlacementRevocationEntity.acknowledged_at.is_(None),
                            PlacementRevocationEntity.cancelled_at.is_(None),
                        )
                    )
                ).scalars().all()
                for pending in pending_for_new_owner:
                    pending.cancelled_at = now

            if ownership_changed:
                row.generation += 1
                row.applied_generation = None
                cleanup = [
                    old_id
                    for old_id in (row.cleanup_node_ids_json or [])
                    if old_id != node.id
                ]
                if old_node_id not in cleanup:
                    cleanup.append(old_node_id)
                row.cleanup_node_ids_json = cleanup

            row.node_id = node.id
            row.region_id = region_id
            row.reason = reason
            row.assigned_at = now
        row.active = True
        row.lease_expires_at = lease
        row.autonomy_expires_at = autonomy

    if role == "media":
        camera.media_node_id = node.id
        if changed:
            camera.desired_state = "assigned"
    return row, changed


async def run_placement_once() -> dict:
    """Run one bounded placement-controller iteration under the execution fence.

    Returns:
        Dictionary containing scanned, moved, unplaced, deferred-autonomy counts
        and the persisted pagination cursor.

    Raises:
        Exception: Database, fencing or placement mutation failures propagate.
    """
    now = datetime.now(timezone.utc)
    scanned = moved = unplaced = deferred_autonomy = 0
    cursor: str | None = None

    async with SessionLocal() as session:
        async with session.begin():
            if not await _leader_lock(session):
                return {
                    "scanned": 0,
                    "moved": 0,
                    "unplaced": 0,
                    "deferred_autonomy": 0,
                    "cursor": None,
                }

            node_rows = (
                await session.execute(
                    select(InfrastructureNodeEntity).where(InfrastructureNodeEntity.enabled.is_(True))
                )
            ).scalars().all()
            nodes = [
                NodeSnapshot(
                    id=n.id,
                    region_id=n.region_id,
                    roles=frozenset(n.roles_json or []),
                    state=n.state,
                    enabled=n.enabled,
                    capacity=n.capacity_json or {},
                    load=dict(n.load_json or {}),
                    heartbeat_at=n.heartbeat_at,
                    authority_mode=n.authority_mode or "fenced_degraded",
                )
                for n in node_rows
            ]

            state = await session.get(ServiceStateEntity, CURSOR_KEY, with_for_update=True)
            if state is None:
                state = ServiceStateEntity(key=CURSOR_KEY, value_json={})
                session.add(state)
                await session.flush()
            current = (state.value_json or {}).get("camera_id")

            def camera_query(after: str | None):
                q = select(CameraEntity).where(CameraEntity.enabled.is_(True))
                if after:
                    q = q.where(CameraEntity.id > after)
                return q.order_by(CameraEntity.id).limit(settings.placement_batch_size)

            cameras = list((await session.execute(camera_query(current))).scalars().all())
            if not cameras and current:
                cameras = list((await session.execute(camera_query(None))).scalars().all())
            if not cameras:
                state.value_json = {"camera_id": None}
                return {
                    "scanned": 0,
                    "moved": 0,
                    "unplaced": 0,
                    "deferred_autonomy": 0,
                    "cursor": None,
                }

            camera_ids = [c.id for c in cameras]
            recording = {
                p.camera_id: p
                for p in (
                    await session.execute(
                        select(RecordingPolicyEntity).where(RecordingPolicyEntity.camera_id.in_(camera_ids))
                    )
                ).scalars().all()
            }
            ai = {
                p.camera_id: p
                for p in (
                    await session.execute(
                        select(CameraAIPolicyEntity).where(CameraAIPolicyEntity.camera_id.in_(camera_ids))
                    )
                ).scalars().all()
            }
            assignments = {
                (a.camera_id, a.role): a
                for a in (
                    await session.execute(
                        select(PlacementAssignmentEntity).where(
                            PlacementAssignmentEntity.camera_id.in_(camera_ids)
                        )
                    )
                ).scalars().all()
            }
            site_ids = sorted({c.site_id for c in cameras})
            tenant_ids = sorted({c.tenant_id for c in cameras})
            site_regions = {
                (s.tenant_id, s.site_id): s.region_id
                for s in (
                    await session.execute(
                        select(SiteRegionEntity).where(
                            SiteRegionEntity.site_id.in_(site_ids),
                            SiteRegionEntity.tenant_id.in_(tenant_ids),
                        )
                    )
                ).scalars().all()
            }

            last_processed: str | None = None
            exhausted_moves = False
            for camera in cameras:
                if moved >= settings.placement_max_moves_per_run:
                    exhausted_moves = True
                    break

                region_id = site_regions.get(
                    (camera.tenant_id, camera.site_id),
                    settings.placement_default_region,
                )

                for role in ("media", "recording", "ai"):
                    if role == "recording":
                        policy = recording.get(camera.id)
                        if not policy or not policy.enabled:
                            continue
                    if role == "ai":
                        policy = ai.get(camera.id)
                        if not policy or not policy.enabled:
                            continue

                    existing = assignments.get((camera.id, role))
                    current_node = next(
                        (n for n in nodes if existing is not None and n.id == existing.node_id),
                        None,
                    )
                    if current_node and node_eligible(current_node, role, region_id, now):
                        existing.lease_expires_at = now + timedelta(
                            seconds=settings.placement_lease_seconds
                        )
                        existing.autonomy_expires_at = autonomy_deadline(now)
                        if role == "recording":
                            recording[camera.id].recording_node_id = current_node.id
                        continue

                    # A disconnected owner may keep only the generation for which
                    # central authority explicitly pre-granted an offline window.
                    # Central failover is deferred until that window ends; otherwise
                    # old and new owners could both be valid during a WAN partition.
                    if autonomy_active(existing, now):
                        deferred_autonomy += 1
                        continue

                    target = choose_node(nodes, role, region_id, now)
                    if target is None:
                        unplaced += 1
                        continue

                    reason = "initial" if existing is None else "failover"
                    row, changed = await _assign(
                        session,
                        camera,
                        role,
                        region_id,
                        target,
                        existing,
                        reason=reason,
                        now=now,
                    )
                    assignments[(camera.id, role)] = row
                    if changed:
                        moved += 1
                        project_assignment(target, role)
                    if role == "recording":
                        recording[camera.id].recording_node_id = target.id

                scanned += 1
                last_processed = camera.id
                if moved >= settings.placement_max_moves_per_run:
                    exhausted_moves = True
                    break

            if exhausted_moves:
                cursor = last_processed
            elif len(cameras) >= settings.placement_batch_size:
                cursor = cameras[-1].id
            else:
                cursor = None
            state.value_json = {"camera_id": cursor}

    return {
        "scanned": scanned,
        "moved": moved,
        "unplaced": unplaced,
        "deferred_autonomy": deferred_autonomy,
        "cursor": cursor,
    }
