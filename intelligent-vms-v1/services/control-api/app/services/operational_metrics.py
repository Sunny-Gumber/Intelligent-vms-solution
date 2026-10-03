from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, or_, select

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.entities import (
    CameraAIPolicyEntity,
    CameraHealthStateEntity,
    EventOutboxEntity,
    RecordingHealthStateEntity,
    RecordingPolicyEntity,
)
from app.models.placement import InfrastructureNodeEntity, PlacementAssignmentEntity
from app.services.placement import NodeSnapshot, role_utilization


CAMERA_STATES = ("online", "degraded", "offline", "unknown", "other")
NODE_STATES = ("active", "draining", "maintenance", "other")
AUTHORITY_MODES = (
    "central_online",
    "regional_autonomous",
    "fenced_degraded",
    "other",
)
PLACEMENT_ROLES = ("media", "recording", "ai")


@dataclass(frozen=True)
class OperationalSnapshot:
    """Hold low-cardinality operational measurements for Prometheus export.

    Attributes:
        camera_states: Camera health counts by bounded state.
        camera_transport_ready: Cameras with a successful latest RTSP transport probe.
        camera_health_stale: Camera health rows older than the configured heartbeat window.
        media_paths_present: Cameras whose latest health sample saw a MediaMTX path.
        recording_active: Enabled continuous recording policies.
        recording_gap_candidates: Active recordings whose initialized durable deadline expired.
        recording_health_untracked: Active recordings without an initialized health deadline.
        node_states: Enabled infrastructure node counts by bounded state.
        node_authority_modes: Enabled node counts by bounded authority mode.
        node_stale: Enabled nodes with stale heartbeat timestamps.
        node_saturated: Nodes at/above configured placement headroom by bounded role.
        node_capacity_unmeasured: Nodes lacking trustworthy capacity/load per role.
        placement_active: Active placement assignments by bounded role.
        placement_unapplied: Active assignments whose current generation is not applied.
        ai_policies_enabled: Enabled camera AI policies.
        outbox_oldest_pending_age_seconds: Age of the oldest pending/retry outbox item.
    """

    camera_states: dict[str, int] = field(default_factory=dict)
    camera_transport_ready: int = 0
    camera_health_stale: int = 0
    media_paths_present: int = 0
    recording_active: int = 0
    recording_gap_candidates: int = 0
    recording_health_untracked: int = 0
    node_states: dict[str, int] = field(default_factory=dict)
    node_authority_modes: dict[str, int] = field(default_factory=dict)
    node_stale: int = 0
    node_saturated: dict[str, int] = field(default_factory=dict)
    node_capacity_unmeasured: dict[str, int] = field(default_factory=dict)
    placement_active: dict[str, int] = field(default_factory=dict)
    placement_unapplied: dict[str, int] = field(default_factory=dict)
    ai_policies_enabled: int = 0
    outbox_oldest_pending_age_seconds: float = 0.0


def _bounded_counts(
    rows: list[tuple[str, int]],
    allowed: tuple[str, ...],
) -> dict[str, int]:
    result = {key: 0 for key in allowed}
    for raw_key, raw_count in rows:
        key = raw_key if raw_key in result and raw_key != "other" else "other"
        result[key] += int(raw_count or 0)
    return result


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def collect_operational_snapshot(
    *,
    now: datetime | None = None,
) -> OperationalSnapshot:
    """Collect bounded operational aggregates from durable control-plane state.

    Args:
        now: Optional deterministic UTC clock value for stale/gap/age calculations.

    Returns:
        OperationalSnapshot containing only low-cardinality aggregate measurements.

    Raises:
        Exception: Database query failures propagate so the metrics layer can mark
            the operational refresh unhealthy without returning fabricated zeros.
    """
    now = _normalize_utc(now or datetime.now(timezone.utc))
    health_stale_before = now - timedelta(
        seconds=max(1, int(settings.health_heartbeat_seconds) * 2)
    )
    node_stale_before = now - timedelta(
        seconds=max(1.0, float(settings.placement_node_stale_seconds))
    )

    async with SessionLocal() as session:
        camera_rows = (
            await session.execute(
                select(
                    CameraHealthStateEntity.state,
                    func.count(CameraHealthStateEntity.camera_id),
                ).group_by(CameraHealthStateEntity.state)
            )
        ).all()
        camera_aggregate = (
            await session.execute(
                select(
                    func.coalesce(
                        func.sum(
                            case(
                                (CameraHealthStateEntity.ready.is_(True), 1),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            case(
                                (CameraHealthStateEntity.path_present.is_(True), 1),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            case(
                                (CameraHealthStateEntity.observed_at < health_stale_before, 1),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                )
            )
        ).one()

        recording_active = int(
            (
                await session.execute(
                    select(func.count(RecordingPolicyEntity.id)).where(
                        RecordingPolicyEntity.enabled.is_(True),
                        RecordingPolicyEntity.mode == "continuous",
                    )
                )
            ).scalar_one()
            or 0
        )
        recording_gap_candidates = int(
            (
                await session.execute(
                    select(func.count(RecordingPolicyEntity.id))
                    .select_from(RecordingPolicyEntity)
                    .join(
                        RecordingHealthStateEntity,
                        RecordingHealthStateEntity.camera_id
                        == RecordingPolicyEntity.camera_id,
                    )
                    .where(
                        RecordingPolicyEntity.enabled.is_(True),
                        RecordingPolicyEntity.mode == "continuous",
                        RecordingHealthStateEntity.gap_deadline_at.is_not(None),
                        RecordingHealthStateEntity.gap_deadline_at <= now,
                    )
                )
            ).scalar_one()
            or 0
        )
        recording_health_untracked = int(
            (
                await session.execute(
                    select(func.count(RecordingPolicyEntity.id))
                    .select_from(RecordingPolicyEntity)
                    .outerjoin(
                        RecordingHealthStateEntity,
                        RecordingHealthStateEntity.camera_id
                        == RecordingPolicyEntity.camera_id,
                    )
                    .where(
                        RecordingPolicyEntity.enabled.is_(True),
                        RecordingPolicyEntity.mode == "continuous",
                        RecordingHealthStateEntity.gap_deadline_at.is_(None),
                    )
                )
            ).scalar_one()
            or 0
        )

        nodes = list(
            (
                await session.execute(
                    select(InfrastructureNodeEntity).where(
                        InfrastructureNodeEntity.enabled.is_(True)
                    )
                )
            ).scalars().all()
        )

        placement_rows = (
            await session.execute(
                select(
                    PlacementAssignmentEntity.role,
                    func.count(PlacementAssignmentEntity.id),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    or_(
                                        PlacementAssignmentEntity.applied_generation.is_(None),
                                        PlacementAssignmentEntity.applied_generation
                                        != PlacementAssignmentEntity.generation,
                                    ),
                                    1,
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                )
                .where(PlacementAssignmentEntity.active.is_(True))
                .group_by(PlacementAssignmentEntity.role)
            )
        ).all()

        ai_policies_enabled = int(
            (
                await session.execute(
                    select(func.count(CameraAIPolicyEntity.id)).where(
                        CameraAIPolicyEntity.enabled.is_(True)
                    )
                )
            ).scalar_one()
            or 0
        )

        oldest_pending = (
            await session.execute(
                select(func.min(EventOutboxEntity.created_at)).where(
                    EventOutboxEntity.status.in_(("pending", "retry"))
                )
            )
        ).scalar_one_or_none()

    camera_states = _bounded_counts(
        [(str(state), int(count)) for state, count in camera_rows],
        CAMERA_STATES,
    )

    node_state_counter: Counter[str] = Counter()
    authority_counter: Counter[str] = Counter()
    saturated_counter: Counter[str] = Counter()
    unmeasured_counter: Counter[str] = Counter()
    stale_nodes = 0

    for node in nodes:
        state = node.state if node.state in NODE_STATES[:-1] else "other"
        authority = (
            node.authority_mode
            if node.authority_mode in AUTHORITY_MODES[:-1]
            else "other"
        )
        node_state_counter[state] += 1
        authority_counter[authority] += 1

        heartbeat = _normalize_utc(node.heartbeat_at)
        if heartbeat < node_stale_before:
            stale_nodes += 1

        snapshot = NodeSnapshot(
            id=node.id,
            region_id=node.region_id,
            roles=frozenset(node.roles_json or []),
            state=node.state,
            enabled=node.enabled,
            capacity=node.capacity_json or {},
            load=dict(node.load_json or {}),
            heartbeat_at=heartbeat,
            authority_mode=node.authority_mode or "fenced_degraded",
        )
        for role in PLACEMENT_ROLES:
            if role not in snapshot.roles:
                continue
            utilization = role_utilization(snapshot, role)
            if utilization is None:
                unmeasured_counter[role] += 1
            elif utilization[0] >= float(settings.placement_headroom):
                saturated_counter[role] += 1

    node_states = {key: int(node_state_counter.get(key, 0)) for key in NODE_STATES}
    node_authority_modes = {
        key: int(authority_counter.get(key, 0)) for key in AUTHORITY_MODES
    }
    placement_active = {role: 0 for role in PLACEMENT_ROLES}
    placement_unapplied = {role: 0 for role in PLACEMENT_ROLES}
    for raw_role, active_count, unapplied_count in placement_rows:
        role = str(raw_role)
        if role not in placement_active:
            continue
        placement_active[role] = int(active_count or 0)
        placement_unapplied[role] = int(unapplied_count or 0)

    oldest_age = 0.0
    if oldest_pending is not None:
        oldest_age = max(0.0, (now - _normalize_utc(oldest_pending)).total_seconds())

    return OperationalSnapshot(
        camera_states=camera_states,
        camera_transport_ready=int(camera_aggregate[0] or 0),
        media_paths_present=int(camera_aggregate[1] or 0),
        camera_health_stale=int(camera_aggregate[2] or 0),
        recording_active=recording_active,
        recording_gap_candidates=recording_gap_candidates,
        recording_health_untracked=recording_health_untracked,
        node_states=node_states,
        node_authority_modes=node_authority_modes,
        node_stale=stale_nodes,
        node_saturated={
            role: int(saturated_counter.get(role, 0)) for role in PLACEMENT_ROLES
        },
        node_capacity_unmeasured={
            role: int(unmeasured_counter.get(role, 0)) for role in PLACEMENT_ROLES
        },
        placement_active=placement_active,
        placement_unapplied=placement_unapplied,
        ai_policies_enabled=ai_policies_enabled,
        outbox_oldest_pending_age_seconds=oldest_age,
    )
