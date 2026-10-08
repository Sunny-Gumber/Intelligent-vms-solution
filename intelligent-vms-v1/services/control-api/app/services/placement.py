from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import func, select

from app.core.config import settings
from app.core.effective_authority import (
    effective_authority_active,
    effective_authority_deadline,
)
from app.core.placement_renewal import assert_settings_renewal_budget
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

log = logging.getLogger(__name__)

CURSOR_KEY = "placement_controller_cursor"
RENEWAL_CURSOR_KEY = "placement_renewal_cursor"


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


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _extend_owner_lease(row: PlacementAssignmentEntity, now: datetime) -> bool:
    """Grant a full lease from now without shortening a later one.

    Args:
        row: Assignment whose current owner is already eligible.
        now: Controller evaluation time.

    Returns:
        True when the stored lease moved later. False when the row already
        holds this full term or a later one, so a second pass cannot shrink it.
    """
    new_lease = now + timedelta(seconds=settings.placement_lease_seconds)
    current = row.lease_expires_at
    if current is not None and _aware_utc(current) >= new_lease:
        return False
    row.lease_expires_at = new_lease
    row.autonomy_expires_at = autonomy_deadline(now)
    return True


def _role_required(role: str, recording_policy, ai_policy) -> bool:
    """Return whether the scan still places this role for the camera.

    Media is always required. Recording and AI are required only while their
    current policy is enabled. A missing or disabled policy is not renewed.
    """
    if role == "media":
        return True
    if role == "recording":
        return recording_policy is not None and bool(recording_policy.enabled)
    if role == "ai":
        return ai_policy is not None and bool(ai_policy.enabled)
    return False


def _site_region_id(site_regions: dict, camera: CameraEntity) -> str:
    """Return the camera's current site region, the same value the scan uses."""
    return site_regions.get(
        (camera.tenant_id, camera.site_id),
        settings.placement_default_region,
    )


def _owner_to_keep(
    nodes: Iterable[NodeSnapshot],
    existing: PlacementAssignmentEntity | None,
    role: str,
    region_id: str,
    now: datetime,
) -> NodeSnapshot | None:
    """Return the current owner when the scan would keep that owner.

    Eligibility uses the camera's current site region, not the region stored
    on the assignment. A stale recorded region must not block renewal of an
    owner the scan would keep, and must not extend an owner the scan would move.
    """
    if existing is None:
        return None
    current = next((node for node in nodes if node.id == existing.node_id), None)
    if current is not None and node_eligible(current, role, region_id, now):
        return current
    return None


def _keep_owner(row: PlacementAssignmentEntity, node: NodeSnapshot, recording: dict, now: datetime) -> bool:
    """Extend the lease of an owner the scan would keep.

    The scan keep path does not rewrite assignment.region_id, so this path
    does not either. Recording node metadata is updated the same way the scan
    updates it.
    """
    extended = _extend_owner_lease(row, now)
    if row.role == "recording":
        policy = recording.get(row.camera_id)
        if policy is not None:
            policy.recording_node_id = node.id
    return extended


def _renewal_assignment_query(after: str | None):
    # Enabled cameras only. Disabled cameras are outside the scan and must not
    # keep a lease alive through the independent pass.
    query = (
        select(PlacementAssignmentEntity)
        .join(CameraEntity, CameraEntity.id == PlacementAssignmentEntity.camera_id)
        .where(
            PlacementAssignmentEntity.active.is_(True),
            CameraEntity.enabled.is_(True),
        )
    )
    if after:
        query = query.where(PlacementAssignmentEntity.id > after)
    return query.order_by(PlacementAssignmentEntity.id).limit(settings.placement_renewal_batch_size)


async def _active_assignment_count(session) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(PlacementAssignmentEntity)
                .join(CameraEntity, CameraEntity.id == PlacementAssignmentEntity.camera_id)
                .where(
                    PlacementAssignmentEntity.active.is_(True),
                    CameraEntity.enabled.is_(True),
                )
            )
        ).scalar_one()
    )


async def _renew_eligible_owners(session, nodes: list[NodeSnapshot], now: datetime) -> int:
    """Renew one bounded page of owners the scan would keep.

    The page advances even when a row is not kept, so a stale owner cannot sit
    at the front and starve healthy ones. A role the scan no longer requires
    is not extended. Eligibility uses the camera's current site region.

    Args:
        session: Open placement transaction that already holds the execution lock.
        nodes: Enabled infrastructure nodes loaded for this run.
        now: Controller evaluation time shared with the camera scan.

    Returns:
        How many leases this page moved later. At most placement_renewal_batch_size.
    """
    state = await session.get(ServiceStateEntity, RENEWAL_CURSOR_KEY, with_for_update=True)
    if state is None:
        state = ServiceStateEntity(key=RENEWAL_CURSOR_KEY, value_json={})
        session.add(state)
        await session.flush()
    current = (state.value_json or {}).get("assignment_id")

    async def _page(after: str | None) -> list[PlacementAssignmentEntity]:
        return list((await session.execute(_renewal_assignment_query(after))).scalars().all())

    rows = await _page(current)
    if not rows and current:
        rows = await _page(None)
    if not rows:
        state.value_json = {"assignment_id": None}
        return 0

    camera_ids = [row.camera_id for row in rows]
    cameras = {
        camera.id: camera
        for camera in (
            await session.execute(select(CameraEntity).where(CameraEntity.id.in_(camera_ids)))
        ).scalars().all()
    }
    recording = {
        policy.camera_id: policy
        for policy in (
            await session.execute(
                select(RecordingPolicyEntity).where(RecordingPolicyEntity.camera_id.in_(camera_ids))
            )
        ).scalars().all()
    }
    ai = {
        policy.camera_id: policy
        for policy in (
            await session.execute(
                select(CameraAIPolicyEntity).where(CameraAIPolicyEntity.camera_id.in_(camera_ids))
            )
        ).scalars().all()
    }
    site_ids = sorted({camera.site_id for camera in cameras.values()})
    tenant_ids = sorted({camera.tenant_id for camera in cameras.values()})
    site_regions = {
        (region.tenant_id, region.site_id): region.region_id
        for region in (
            await session.execute(
                select(SiteRegionEntity).where(
                    SiteRegionEntity.site_id.in_(site_ids),
                    SiteRegionEntity.tenant_id.in_(tenant_ids),
                )
            )
        ).scalars().all()
    } if site_ids else {}
    renewed = 0
    for row in rows:
        camera = cameras.get(row.camera_id)
        if camera is None or not camera.enabled:
            continue
        if not _role_required(row.role, recording.get(row.camera_id), ai.get(row.camera_id)):
            continue
        kept = _owner_to_keep(nodes, row, row.role, _site_region_id(site_regions, camera), now)
        if kept is None:
            continue
        if _keep_owner(row, kept, recording, now):
            renewed += 1

    if len(rows) >= settings.placement_renewal_batch_size:
        cursor = rows[-1].id
    else:
        cursor = None
    state.value_json = {"assignment_id": cursor}
    return renewed


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
                # Record the same boundary the node was allowed to execute.
                valid_until = effective_authority_deadline(
                    row.lease_expires_at,
                    row.autonomy_expires_at,
                    settings.placement_fence_expiry_grace_seconds,
                )

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


def _placement_result(
    *,
    scanned: int = 0,
    moved: int = 0,
    unplaced: int = 0,
    deferred_autonomy: int = 0,
    renewed: int = 0,
    renewal_budget_exceeded: bool = False,
    cursor: str | None = None,
) -> dict:
    return {
        "scanned": scanned,
        "moved": moved,
        "unplaced": unplaced,
        "deferred_autonomy": deferred_autonomy,
        "renewed": renewed,
        "renewal_budget_exceeded": renewal_budget_exceeded,
        "cursor": cursor,
    }


async def run_placement_once() -> dict:
    """Run one bounded placement-controller iteration under the execution fence.

    Eligible owners are renewed from a cursor that does not follow the camera
    scan page, and only after the placement execution lock is held. Renewal
    keeps an owner only when the scan would keep that same owner. A missed
    lock is retried with a short backoff before this cycle gives up. A live
    population above the renewal ceiling skips renewal and still scans.

    Returns:
        Dictionary containing scanned, moved, unplaced, deferred, and renewed
        counts, whether the live population exceeded the renewal ceiling, and
        the persisted camera-scan cursor. Deferred counts are assignments whose
        previous effective authority is still live. Renewed counts are owners
        the scan would keep whose lease moved later on this run's renewal page.

    Raises:
        PlacementRenewalBudgetError: The configured budget is not strictly
            inside the lease. The lease is not shortened. A live population
            above the ceiling does not raise.
        Exception: Database, fencing or placement mutation failures propagate.
    """
    assert_settings_renewal_budget(settings)
    attempts = int(settings.placement_renewal_lock_retry_limit)
    for attempt in range(attempts):
        result = await _run_placement_once_locked()
        if result is not None:
            return result
        if attempt + 1 < attempts:
            await asyncio.sleep(float(settings.placement_renewal_lock_retry_seconds))
    return _placement_result()


async def _run_placement_once_locked() -> dict | None:
    """Run one locked placement iteration.

    Returns:
        The run summary, or None when the execution lock was not acquired.

    Raises:
        PlacementRenewalBudgetError: Propagated from callers that validate
            settings before this attempt. This function does not raise it for
            a population above the ceiling.
        Exception: Database or placement mutation failures propagate.
    """
    now = datetime.now(timezone.utc)
    scanned = moved = unplaced = deferred_autonomy = renewed = 0
    renewal_budget_exceeded = False
    cursor: str | None = None

    async with SessionLocal() as session:
        async with session.begin():
            if not await _leader_lock(session):
                return None

            active_assignments = await _active_assignment_count(session)
            ceiling = int(settings.placement_renewal_max_assignments)
            if active_assignments > ceiling:
                renewal_budget_exceeded = True
                log.critical(
                    "placement_renewal_budget_exceeded active_assignments=%s ceiling=%s",
                    active_assignments,
                    ceiling,
                )

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
            if renewal_budget_exceeded:
                renewed = 0
            else:
                renewed = await _renew_eligible_owners(session, nodes, now)

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
                return _placement_result(
                    renewed=renewed,
                    renewal_budget_exceeded=renewal_budget_exceeded,
                )

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

                region_id = _site_region_id(site_regions, camera)

                for role in ("media", "recording", "ai"):
                    if not _role_required(role, recording.get(camera.id), ai.get(camera.id)):
                        continue

                    existing = assignments.get((camera.id, role))
                    kept = _owner_to_keep(nodes, existing, role, region_id, now)
                    if kept is not None:
                        _keep_owner(existing, kept, recording, now)
                        continue

                    # A partitioned owner keeps executing until lease plus fence
                    # grace, or until a later pre-granted autonomy deadline.
                    # There is no revocation acknowledgement for a generation
                    # that is still the active owner, so control must wait for
                    # that shared boundary. Authorizing a successor earlier
                    # overlaps the old lease.
                    if existing is not None and effective_authority_active(
                        existing.lease_expires_at,
                        existing.autonomy_expires_at,
                        now,
                        settings.placement_fence_expiry_grace_seconds,
                    ):
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

    return _placement_result(
        scanned=scanned,
        moved=moved,
        unplaced=unplaced,
        deferred_autonomy=deferred_autonomy,
        renewed=renewed,
        renewal_budget_exceeded=renewal_budget_exceeded,
        cursor=cursor,
    )
