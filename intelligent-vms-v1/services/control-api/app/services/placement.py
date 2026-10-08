from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Iterable

from sqlalchemy import and_, exists, func, literal, not_, or_, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm.attributes import set_committed_value

from app.core.config import settings
from app.db.base import utcnow
from app.core.effective_authority import (
    effective_authority_active,
    effective_authority_deadline,
)
from app.core.placement_renewal import (
    PlacementRenewalInvalidated,
    PlacementRenewalRunExceeded,
    assert_settings_renewal_budget,
)
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
# One lease statement, not a second budget page. A 2,000-row statement on this
# development host finished in a small fraction of the default max run; the
# deadline below still cancels a statement that does not. This is one sample's
# chunk size, not a fleet capacity claim.
_RENEWAL_LEASE_WRITE_CHUNK = 2000
_RUN_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "placement_renewal_run_deadline",
    default=None,
)
_EXTENDED_LEASE_IDS: contextvars.ContextVar[set[str] | None] = contextvars.ContextVar(
    "placement_extended_lease_ids",
    default=None,
)
_LEASE_UPDATE = text(
    """
    UPDATE placement_assignments AS a
    SET lease_expires_at = :new_lease,
        autonomy_expires_at = :autonomy
    FROM unnest(
        CAST(:ids AS varchar[]),
        CAST(:node_ids AS varchar[]),
        CAST(:generations AS integer[])
    ) AS observed(id, node_id, generation)
    WHERE a.id = observed.id
      AND a.node_id = observed.node_id
      AND a.generation = observed.generation
      AND a.active IS TRUE
      AND (a.lease_expires_at IS NULL OR a.lease_expires_at < :new_lease)
      AND EXISTS (
          SELECT 1 FROM infrastructure_nodes AS n
          WHERE n.id = observed.node_id
            AND n.region_id = COALESCE(
                (
                    SELECT sr.region_id
                    FROM site_regions AS sr
                    JOIN cameras AS c
                      ON c.tenant_id = sr.tenant_id
                     AND c.site_id = sr.site_id
                    WHERE c.id = a.camera_id
                    LIMIT 1
                ),
                :default_region
            )
      )
      AND (
          a.role <> 'recording'
          OR EXISTS (
              SELECT 1 FROM recording_policies AS rp
              WHERE rp.camera_id = a.camera_id AND rp.enabled IS TRUE
          )
      )
      AND (
          a.role <> 'ai'
          OR EXISTS (
              SELECT 1 FROM camera_ai_policies AS ap
              WHERE ap.camera_id = a.camera_id AND ap.enabled IS TRUE
          )
      )
    RETURNING a.id
    """
)


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


def _is_orm_session(session) -> bool:
    return session is not None and type(session).__module__.startswith("sqlalchemy")


class _FailoverSuperseded(Exception):
    """Another session advanced this assignment before failover could write it."""

    def __init__(self, row: PlacementAssignmentEntity):
        self.row = row
        super().__init__("placement failover superseded")


async def _after_renewal_authority_read() -> None:
    """Test seam after the renewal read and before any lease write.

    Production does nothing. The placement transaction is still read-only when
    the renewal cursor row already exists, so another session can commit.
    """
    return None


async def _before_assignment_update() -> None:
    """Test seam after the assignment is loaded and before failover writes it.

    Production does nothing.
    """
    return None


def _arm_run_deadline() -> contextvars.Token:
    """Start the max-run clock for one locked attempt.

    A max run of zero reserves no time in the budget and does not arm a
    deadline. A positive max run is a real deadline: work past it is not
    committed.

    Returns:
        Token used to reset the deadline when the attempt ends.
    """
    limit = float(settings.placement_renewal_max_run_seconds)
    if limit <= 0:
        return _RUN_DEADLINE.set(None)
    return _RUN_DEADLINE.set(time.monotonic() + limit)


def _raise_if_over_deadline() -> None:
    """Raise when this attempt has spent its configured max run.

    Raises:
        PlacementRenewalRunExceeded: The monotonic deadline has passed. The
            caller rolls the transaction back.
    """
    deadline = _RUN_DEADLINE.get()
    if deadline is None:
        return
    if time.monotonic() > deadline:
        raise PlacementRenewalRunExceeded(
            "placement renewal max run exceeded: "
            f"max_run={settings.placement_renewal_max_run_seconds}s"
        )


async def _before_renewal_lease_write(_session=None) -> None:
    """Stop a lease write that would land after the configured max run.

    Production checks the deadline. A test may wrap this to commit a site or
    policy change between chunks. The session argument is the open placement
    transaction. A chunk that already ran is still uncommitted when this
    raises, and a later chunk that misses authority rolls that chunk back
    before the transaction commits.

    Args:
        _session: Open placement transaction, when the caller has one.
    """
    _raise_if_over_deadline()


def _remember_extended_lease(assignment_id: str) -> None:
    found = _EXTENDED_LEASE_IDS.get()
    if found is not None:
        found.add(assignment_id)


def _is_statement_timeout(error: BaseException) -> bool:
    orig = getattr(error, "orig", None)
    state = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return state == "57014"


async def _arm_statement_timeout(session) -> None:
    """Bound the next PostgreSQL statement by the time still left in max run.

    ``statement_timeout`` is per statement. The value armed here is the time
    still left at this call, not a fresh copy of the full max run. A statement
    that has already started keeps the timeout it was given when it started, so
    the advisory lock can be held until that statement ends. The attempt checks
    the monotonic deadline again before commit and rolls back when the clock is
    already past max run, so that hold does not commit a lease. Callers arm the
    timeout again after a chunk returns so the following statement does not
    inherit a value that was larger than the time now left.

    SQLite has no statement_timeout. The deadline check between lease chunks
    is what bounds that dialect, and a raise rolls the transaction back.
    A max run of 0 leaves the deadline unset, so this function does nothing.
    """
    deadline = _RUN_DEADLINE.get()
    if deadline is None:
        return
    bind = session.get_bind()
    if getattr(getattr(bind, "dialect", None), "name", "") != "postgresql":
        return
    _raise_if_over_deadline()
    remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
    await session.execute(select(func.set_config("statement_timeout", str(remaining_ms), True)))


def _session_is_postgresql(session) -> bool:
    bind = session.get_bind()
    return getattr(getattr(bind, "dialect", None), "name", "") == "postgresql"


def _policy_view(enabled: bool | None):
    if enabled is None:
        return None
    return SimpleNamespace(enabled=enabled)


def _lease_authority_clauses(camera_id: str, role: str, node_id: str) -> list:
    """SQL predicates that repeat the keep rule inside the lease UPDATE.

    PostgreSQL evaluates these against the latest commit. A site move or a
    disabled policy that landed after the Python read then matches no row.
    """
    site_region = (
        select(SiteRegionEntity.region_id)
        .join(
            CameraEntity,
            and_(
                CameraEntity.tenant_id == SiteRegionEntity.tenant_id,
                CameraEntity.site_id == SiteRegionEntity.site_id,
            ),
        )
        .where(CameraEntity.id == camera_id)
        .limit(1)
        .scalar_subquery()
    )
    clauses = [
        exists(
            select(InfrastructureNodeEntity.id).where(
                InfrastructureNodeEntity.id == node_id,
                InfrastructureNodeEntity.region_id
                == func.coalesce(site_region, literal(settings.placement_default_region)),
            )
        )
    ]
    if role == "recording":
        clauses.append(
            exists(
                select(RecordingPolicyEntity.id).where(
                    RecordingPolicyEntity.camera_id == camera_id,
                    RecordingPolicyEntity.enabled.is_(True),
                )
            )
        )
    elif role == "ai":
        clauses.append(
            exists(
                select(CameraAIPolicyEntity.id).where(
                    CameraAIPolicyEntity.camera_id == camera_id,
                    CameraAIPolicyEntity.enabled.is_(True),
                )
            )
        )
    return clauses


async def _extend_owner_lease(
    session,
    row: PlacementAssignmentEntity,
    now: datetime,
    *,
    node_id: str,
    generation: int,
) -> bool:
    """Grant a full lease from now without shortening a later one.

    The write matches the owner and generation renewal or the scan observed.
    A flush cannot lengthen a successor that has since taken the same row.
    SQLite does not take the PostgreSQL advisory lock; this predicate is what
    stops the stale write there too.

    Args:
        session: Open placement transaction.
        row: Assignment loaded for this decision.
        now: Controller evaluation time.
        node_id: Owner id observed when the keep decision was made.
        generation: Generation observed when the keep decision was made.

    Returns:
        True when that exact owner and generation had its lease moved later.
        False when the row already holds this full term, or when the row is
        no longer that owner.

    Raises:
        PlacementRenewalRunExceeded: This attempt is already past its max run.
            The caller rolls the transaction back, so this write is not committed.
    """
    await _before_renewal_lease_write(session)
    if row.node_id != node_id or row.generation != generation:
        return False
    new_lease = now + timedelta(seconds=settings.placement_lease_seconds)
    current = row.lease_expires_at
    if current is not None and _aware_utc(current) >= new_lease:
        return False
    await _arm_statement_timeout(session)
    autonomy = autonomy_deadline(now)
    result = await session.execute(
        update(PlacementAssignmentEntity)
        .where(
            PlacementAssignmentEntity.id == row.id,
            PlacementAssignmentEntity.node_id == node_id,
            PlacementAssignmentEntity.generation == generation,
            PlacementAssignmentEntity.active.is_(True),
            *_lease_authority_clauses(row.camera_id, row.role, node_id),
        )
        .values(
            lease_expires_at=new_lease,
            autonomy_expires_at=autonomy,
        )
        .returning(PlacementAssignmentEntity.id)
        .execution_options(synchronize_session=False)
    )
    if result.scalar_one_or_none() is None:
        await session.refresh(row)
        return False
    # The UPDATE already persisted the lease. Mark it committed so a later
    # flush cannot write it again by primary key onto a successor.
    set_committed_value(row, "lease_expires_at", new_lease)
    set_committed_value(row, "autonomy_expires_at", autonomy)
    _remember_extended_lease(row.id)
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


def _owner_if_kept(nodes, existing, role: str, regions: list[str], now: datetime) -> NodeSnapshot | None:
    """Return the owner only when every supplied region would keep that owner."""
    if existing is None:
        return None
    kept = None
    for region_id in regions:
        found = _owner_to_keep(nodes, existing, role, region_id, now)
        if found is None:
            return None
        kept = found
    return kept


def _failover_region(nodes, existing, role: str, fresh_region: str, scan_region: str, now: datetime) -> str:
    """Pick the region a move must use when the owner is not kept.

    A fresh committed site wins when that region no longer accepts the owner.
    The scan region's answer wins when it is the one that rejects the owner,
    so a scan-page disagreement cannot fall through to a keep.
    """
    if existing is None or _owner_to_keep(nodes, existing, role, fresh_region, now) is None:
        return fresh_region
    return scan_region


async def _keep_owner(
    session,
    row: PlacementAssignmentEntity,
    node: NodeSnapshot,
    recording: dict,
    now: datetime,
    *,
    node_id: str,
    generation: int,
) -> bool:
    """Extend the lease of an owner the scan would keep.

    The scan keep path does not rewrite assignment.region_id, so this path
    does not either. Recording node metadata is updated the same way the scan
    updates it, and only while the row is still the observed owner.
    """
    extended = await _extend_owner_lease(
        session,
        row,
        now,
        node_id=node_id,
        generation=generation,
    )
    if row.role == "recording" and row.node_id == node_id and row.generation == generation:
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


@dataclass
class _RenewalCandidate:
    """One owner the renewal read would keep, before the scan's later read."""

    row: PlacementAssignmentEntity
    camera: CameraEntity
    node_id: str
    generation: int
    recording: dict
    node: NodeSnapshot


async def _collect_renewal_candidates(
    session, nodes: list[NodeSnapshot], now: datetime
) -> tuple[list[_RenewalCandidate], str | None]:
    """Select one page of owners the renewal read would keep.

    This does not write leases and does not dirty the renewal cursor. The
    caller persists the cursor at the end of the transaction. Every candidate
    is checked again against committed site and policy state before a lease
    is written, including a camera the scan page does not load. The page
    advances even when a row is not kept.

    Args:
        session: Open placement transaction that already holds the execution lock.
        nodes: Enabled infrastructure nodes loaded for this run.
        now: Controller evaluation time shared with the camera scan.

    Returns:
        Candidates in page order, and the assignment id the cursor should store.
        At most placement_renewal_batch_size rows were read.
    """
    state = await session.get(ServiceStateEntity, RENEWAL_CURSOR_KEY, with_for_update=True)
    if state is None:
        state = ServiceStateEntity(key=RENEWAL_CURSOR_KEY, value_json={})
        session.add(state)
    current = (state.value_json or {}).get("assignment_id")

    async def _page(after: str | None) -> list[PlacementAssignmentEntity]:
        return list((await session.execute(_renewal_assignment_query(after))).scalars().all())

    rows = await _page(current)
    if not rows and current:
        rows = await _page(None)
    if not rows:
        await _after_renewal_authority_read()
        return [], None

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
    candidates: list[_RenewalCandidate] = []
    for row in rows:
        camera = cameras.get(row.camera_id)
        if camera is None or not camera.enabled:
            continue
        # Snapshot the owner before the keep decision. A later failover on this
        # object must not inherit the lease written for the generation we saw.
        observed_node = row.node_id
        observed_generation = row.generation
        if not _role_required(row.role, recording.get(row.camera_id), ai.get(row.camera_id)):
            continue
        kept = _owner_to_keep(nodes, row, row.role, _site_region_id(site_regions, camera), now)
        if kept is None:
            continue
        candidates.append(
            _RenewalCandidate(
                row=row,
                camera=camera,
                node_id=observed_node,
                generation=observed_generation,
                recording=recording,
                node=kept,
            )
        )

    if len(rows) >= settings.placement_renewal_batch_size:
        cursor = rows[-1].id
    else:
        cursor = None
    await _after_renewal_authority_read()
    return candidates, cursor


@dataclass(frozen=True)
class _CommittedAuthority:
    """Site and policy authority read from a new session after other commits."""

    region_id: str
    recording_enabled: bool | None
    ai_enabled: bool | None


def _cameras_for_authority(cameras: list[CameraEntity], candidates: list[_RenewalCandidate]) -> list[CameraEntity]:
    seen: set[str] = set()
    ordered: list[CameraEntity] = []
    for camera in [*cameras, *(candidate.camera for candidate in candidates)]:
        if camera.id in seen:
            continue
        seen.add(camera.id)
        ordered.append(camera)
    return ordered


def _memory_sqlite(session) -> bool:
    """True when this session's database is a single shared SQLite connection.

    ``sqlite:///:memory:`` uses one connection for every session. A second
    session's rollback then undoes this transaction. A file database and
    PostgreSQL check out a different connection, which is what makes another
    transaction's commit visible.

    Args:
        session: Open placement transaction.

    Returns:
        True when a second session would reuse this connection.
    """
    bind = session.get_bind()
    url = getattr(bind, "url", None)
    if url is None:
        url = getattr(getattr(bind, "engine", None), "url", None)
    if url is None or url.get_backend_name() != "sqlite":
        return False
    return (url.database or "") in {"", ":memory:"}


async def _read_authority(reader, cameras: list[CameraEntity]) -> dict[str, _CommittedAuthority]:
    """Load site region and policy flags through the given session.

    Args:
        reader: Session whose connection performs the read.
        cameras: Cameras whose current site and policies can authorize a lease.

    Returns:
        One entry per camera. A missing policy is None. A missing site row
        uses the default region.
    """
    camera_ids = [camera.id for camera in cameras]
    site_ids = sorted({camera.site_id for camera in cameras})
    tenant_ids = sorted({camera.tenant_id for camera in cameras})
    regions = {
        (region.tenant_id, region.site_id): region.region_id
        for region in (
            await reader.execute(
                select(SiteRegionEntity).where(
                    SiteRegionEntity.site_id.in_(site_ids),
                    SiteRegionEntity.tenant_id.in_(tenant_ids),
                )
            )
        ).scalars().all()
    }
    recording = {
        policy.camera_id: bool(policy.enabled)
        for policy in (
            await reader.execute(
                select(RecordingPolicyEntity).where(RecordingPolicyEntity.camera_id.in_(camera_ids))
            )
        ).scalars().all()
    }
    ai_policies = {
        policy.camera_id: bool(policy.enabled)
        for policy in (
            await reader.execute(
                select(CameraAIPolicyEntity).where(CameraAIPolicyEntity.camera_id.in_(camera_ids))
            )
        ).scalars().all()
    }
    return {
        camera.id: _CommittedAuthority(
            region_id=regions.get(
                (camera.tenant_id, camera.site_id),
                settings.placement_default_region,
            ),
            recording_enabled=recording.get(camera.id),
            ai_enabled=ai_policies.get(camera.id),
        )
        for camera in cameras
    }


async def _load_committed_authority(session, cameras: list[CameraEntity]) -> dict[str, _CommittedAuthority]:
    """Read site region and policy flags that another transaction has committed.

    The placement transaction can still be holding the renewal read's policy
    objects. A new session has an empty identity map and, on a file database
    or PostgreSQL, a different connection, so it sees that commit. SQLite
    ``:memory:`` has only one connection. Reading it here would roll this
    transaction back when that session closes, and the cursor update would
    then match no row. That database is read on the open session.

    Args:
        session: Open placement transaction.
        cameras: Cameras whose current site and policies can authorize a lease.

    Returns:
        One entry per camera. A missing policy is None, which the keep rule
        treats as not required. A missing site row uses the default region.
    """
    if not cameras:
        return {}
    if _memory_sqlite(session):
        return await _read_authority(session, cameras)
    async with SessionLocal() as fresh:
        return await _read_authority(fresh, cameras)


async def _save_renewal_cursor(session, assignment_id: str | None) -> None:
    state = await session.get(ServiceStateEntity, RENEWAL_CURSOR_KEY)
    if state is None:
        session.add(ServiceStateEntity(key=RENEWAL_CURSOR_KEY, value_json={"assignment_id": assignment_id}))
        return
    state.value_json = {"assignment_id": assignment_id}


async def _apply_renewal_candidates(
    session,
    candidates: list[_RenewalCandidate],
    nodes: list[NodeSnapshot],
    now: datetime,
    *,
    scan_camera_ids: set[str],
    scan_recording: dict,
    scan_ai: dict,
    scan_site_regions: dict,
    committed: dict[str, _CommittedAuthority],
) -> int:
    """Extend candidates still authorized by committed site and policy state.

    Every candidate, including a camera the scan page did not load, is judged
    from a new session's committed site region and policy flags. A camera on
    the scan page must also pass the scan's own maps, so the two reads cannot
    disagree and still extend. The lease UPDATE repeats the site and policy
    check. The advisory lock is still held by the caller.

    Args:
        session: Open placement transaction that already holds the execution lock.
        candidates: Owners the renewal read would have kept.
        nodes: Enabled infrastructure nodes loaded for this run.
        now: Controller evaluation time shared with the camera scan.
        scan_camera_ids: Cameras loaded by this run's scan page.
        scan_recording: Recording policies from the scan's read.
        scan_ai: AI policies from the scan's read.
        scan_site_regions: Site regions from the scan's read.
        committed: Site and policy authority from a new session.

    Returns:
        How many of those leases moved later.

    Raises:
        PlacementRenewalRunExceeded: A lease chunk would start after max run.
            Nothing from this transaction has been committed.
        PlacementRenewalInvalidated: A later chunk did not extend every owner
            after an earlier chunk had. The transaction rolls back.
    """
    pending: list[tuple[_RenewalCandidate, dict, NodeSnapshot]] = []
    for candidate in candidates:
        row = candidate.row
        authority = committed.get(row.camera_id)
        if authority is None:
            continue
        if not _role_required(
            row.role,
            _policy_view(authority.recording_enabled),
            _policy_view(authority.ai_enabled),
        ):
            continue
        regions = [authority.region_id]
        recording = candidate.recording
        if row.camera_id in scan_camera_ids:
            if not _role_required(
                row.role,
                scan_recording.get(row.camera_id),
                scan_ai.get(row.camera_id),
            ):
                continue
            regions.append(_site_region_id(scan_site_regions, candidate.camera))
            recording = scan_recording
        kept = _owner_if_kept(nodes, row, row.role, regions, now)
        if kept is None:
            continue
        pending.append((candidate, recording, kept))
    if _session_is_postgresql(session):
        return await _extend_pending_leases_postgres(session, pending, now)
    return await _extend_pending_leases_sqlite(session, pending, now)


async def _extend_pending_leases_sqlite(session, pending, now: datetime) -> int:
    """Extend kept owners one row at a time, in the same chunks PostgreSQL uses.

    SQLite has no array update. A miss in a later chunk, after an earlier chunk
    extended someone, rolls the attempt back instead of committing that earlier
    chunk. A miss in the first chunk leaves the scan free to defer or fail over,
    because no lease from this attempt has been extended yet.

    Args:
        session: Open SQLite placement transaction.
        pending: Kept owners, the recording map their metadata uses, and the node.
        now: Controller evaluation time.

    Returns:
        How many leases moved later.

    Raises:
        PlacementRenewalInvalidated: A later chunk did not extend every owner.
        PlacementRenewalRunExceeded: A write would start after max run.
    """
    new_lease = now + timedelta(seconds=settings.placement_lease_seconds)
    writable: list[tuple[_RenewalCandidate, dict, NodeSnapshot]] = []
    for candidate, recording, kept in pending:
        row = candidate.row
        if row.node_id != candidate.node_id or row.generation != candidate.generation:
            continue
        current = row.lease_expires_at
        if current is not None and _aware_utc(current) >= new_lease:
            _note_recording_owner(candidate, recording, kept)
            continue
        writable.append((candidate, recording, kept))
    extended: set[str] = set()
    chunk_size = max(1, int(_RENEWAL_LEASE_WRITE_CHUNK))
    for offset in range(0, len(writable), chunk_size):
        chunk = writable[offset : offset + chunk_size]
        missed = False
        for candidate, recording, kept in chunk:
            if await _keep_owner(
                session,
                candidate.row,
                kept,
                recording,
                now,
                node_id=candidate.node_id,
                generation=candidate.generation,
            ):
                extended.add(candidate.row.id)
            else:
                missed = True
        if missed and offset > 0 and extended:
            raise PlacementRenewalInvalidated(
                "placement renewal invalidated: a later chunk did not extend every owner"
            )
        await _arm_statement_timeout(session)
    return len(extended)


async def _extend_pending_leases_postgres(session, pending, now: datetime) -> int:
    """Extend kept owners in bounded PostgreSQL statements.

    Each statement repeats the owner, generation, site, and policy predicates.
    A statement that runs past the remaining max run is cancelled and the
    transaction rolls back, so a chunk cannot commit on its own.

    Args:
        session: Open PostgreSQL placement transaction.
        pending: Kept owners, the recording map their metadata uses, and the node.
        now: Controller evaluation time.

    Returns:
        How many leases moved later.

    Raises:
        PlacementRenewalRunExceeded: The next chunk would start after max run.
        PlacementRenewalInvalidated: A later chunk did not extend every owner
            after an earlier chunk had. Earlier writes in this transaction are
            rolled back with it.
    """
    new_lease = now + timedelta(seconds=settings.placement_lease_seconds)
    autonomy = autonomy_deadline(now)
    writable: list[tuple[_RenewalCandidate, dict, NodeSnapshot]] = []
    for candidate, recording, kept in pending:
        row = candidate.row
        if row.node_id != candidate.node_id or row.generation != candidate.generation:
            continue
        current = row.lease_expires_at
        if current is not None and _aware_utc(current) >= new_lease:
            _note_recording_owner(candidate, recording, kept)
            continue
        writable.append((candidate, recording, kept))
    extended: set[str] = set()
    for offset in range(0, len(writable), _RENEWAL_LEASE_WRITE_CHUNK):
        chunk = writable[offset : offset + _RENEWAL_LEASE_WRITE_CHUNK]
        await _before_renewal_lease_write(session)
        await _arm_statement_timeout(session)
        returned = await _extend_lease_chunk(session, chunk, new_lease, autonomy)
        if len(returned) != len(chunk) and extended:
            raise PlacementRenewalInvalidated(
                "placement renewal invalidated: a later chunk did not extend every owner"
            )
        extended.update(returned)
        for assignment_id in returned:
            _remember_extended_lease(assignment_id)
        # The chunk statement kept the timeout armed before it started. Arm the
        # time still left so the next statement cannot spend that old allowance.
        await _arm_statement_timeout(session)
    for candidate, recording, kept in writable:
        row = candidate.row
        if row.id in extended:
            set_committed_value(row, "lease_expires_at", new_lease)
            set_committed_value(row, "autonomy_expires_at", autonomy)
        _note_recording_owner(candidate, recording, kept)
    return len(extended)


def _note_recording_owner(candidate: _RenewalCandidate, recording: dict, kept: NodeSnapshot) -> None:
    row = candidate.row
    if row.role != "recording" or row.node_id != candidate.node_id or row.generation != candidate.generation:
        return
    policy = recording.get(row.camera_id)
    if policy is not None:
        policy.recording_node_id = kept.id


async def _extend_lease_chunk(session, chunk, new_lease: datetime, autonomy) -> set[str]:
    if not chunk:
        return set()
    result = await session.execute(
        _LEASE_UPDATE,
        {
            "new_lease": new_lease,
            "autonomy": autonomy,
            "ids": [candidate.row.id for candidate, _recording, _kept in chunk],
            "node_ids": [candidate.node_id for candidate, _recording, _kept in chunk],
            "generations": [int(candidate.generation) for candidate, _recording, _kept in chunk],
            "default_region": settings.placement_default_region,
        },
    )
    return {row[0] for row in result.fetchall()}


_AUTHORITY_LOCKS = (
    """
    SELECT sr.id
    FROM site_regions AS sr
    JOIN cameras AS c
      ON c.tenant_id = sr.tenant_id
     AND c.site_id = sr.site_id
    JOIN placement_assignments AS a ON a.camera_id = c.id
    WHERE a.id = ANY(CAST(:ids AS varchar[]))
    ORDER BY sr.id
    FOR SHARE OF sr
    """,
    """
    SELECT rp.id
    FROM recording_policies AS rp
    JOIN placement_assignments AS a ON a.camera_id = rp.camera_id
    WHERE a.id = ANY(CAST(:ids AS varchar[]))
    ORDER BY rp.id
    FOR SHARE OF rp
    """,
    """
    SELECT ap.id
    FROM camera_ai_policies AS ap
    JOIN placement_assignments AS a ON a.camera_id = ap.camera_id
    WHERE a.id = ANY(CAST(:ids AS varchar[]))
    ORDER BY ap.id
    FOR SHARE OF ap
    """,
    """
    SELECT n.id
    FROM infrastructure_nodes AS n
    WHERE n.id IN (
        SELECT a.node_id
        FROM placement_assignments AS a
        WHERE a.id = ANY(CAST(:ids AS varchar[]))
    )
    ORDER BY n.id
    FOR SHARE OF n
    """,
)


async def _lock_extended_authority(session, assignment_ids: list[str]) -> None:
    """Hold the site, policy, and node rows for this commit until the transaction ends.

    The locks are taken after the chunk updates and before commit, in a fixed
    order: site region, recording policy, AI policy, then the owner node, each
    by primary key. A site move, a policy disable, or a node region change that
    has not committed yet waits. One that already committed is visible to the
    stale check that follows. Taking the locks before the chunks would make a
    between-chunk change wait and then land after a successful renewal, which
    is the split this attempt rolls back.

    Assignment rows this attempt updated are already locked by those updates.
    A generation or owner change of those rows waits on that lock. The
    assignment foreign key only takes a key share on the node, which does not
    block an update of region_id. The node share lock does. SQLite has no row
    share lock; its writer lock already stops a second connection.

    Args:
        session: Open placement transaction.
        assignment_ids: Assignments whose leases this attempt has extended.

    Raises:
        PlacementRenewalRunExceeded: A lock wait would start after max run, or
            PostgreSQL cancelled it for the statement timeout.
    """
    if not assignment_ids or not _session_is_postgresql(session):
        return
    ids = sorted(assignment_ids)
    for statement in _AUTHORITY_LOCKS:
        await _arm_statement_timeout(session)
        _raise_if_over_deadline()
        await session.execute(text(statement), {"ids": ids})


async def _reject_stale_extended_leases(session) -> None:
    """Roll the attempt back when an extended lease no longer matches authority.

    The check locks the site, policy, and node rows it depends on, then reads
    them. Those locks are held until this transaction commits or rolls back, so
    a site move, a policy change, or a node region change cannot commit in the
    gap after this check returns.
    A change that committed before the lock is visible here. A later chunk that
    does not extend every owner raises before this check. Raising aborts the
    transaction, so an earlier chunk is not committed.

    A lease that commits while these locks are held was granted under the
    authority they protected. A move or a disable that was waiting then commits
    after that lease. The owner keeps it until lease end plus fence grace.
    Later runs do not extend an owner the new site or policy rejects. They
    defer until that deadline, then fail over. They do not renew the stale
    owner again, and the deferral is that one lease term, not an open wait.

    Args:
        session: Open placement transaction.

    Raises:
        PlacementRenewalInvalidated: At least one extended row fails the same
            site, policy, and active-owner predicates the lease UPDATE uses.
        PlacementRenewalRunExceeded: The check would start after max run.
    """
    found = _EXTENDED_LEASE_IDS.get()
    if not found:
        return
    await _lock_extended_authority(session, list(found))
    await _arm_statement_timeout(session)
    _raise_if_over_deadline()
    site_region = (
        select(SiteRegionEntity.region_id)
        .join(
            CameraEntity,
            and_(
                CameraEntity.tenant_id == SiteRegionEntity.tenant_id,
                CameraEntity.site_id == SiteRegionEntity.site_id,
            ),
        )
        .where(CameraEntity.id == PlacementAssignmentEntity.camera_id)
        .correlate(PlacementAssignmentEntity)
        .limit(1)
        .scalar_subquery()
    )
    node_matches = exists(
        select(InfrastructureNodeEntity.id).where(
            InfrastructureNodeEntity.id == PlacementAssignmentEntity.node_id,
            InfrastructureNodeEntity.region_id
            == func.coalesce(site_region, literal(settings.placement_default_region)),
        )
    )
    recording_enabled = exists(
        select(RecordingPolicyEntity.id).where(
            RecordingPolicyEntity.camera_id == PlacementAssignmentEntity.camera_id,
            RecordingPolicyEntity.enabled.is_(True),
        )
    )
    ai_enabled = exists(
        select(CameraAIPolicyEntity.id).where(
            CameraAIPolicyEntity.camera_id == PlacementAssignmentEntity.camera_id,
            CameraAIPolicyEntity.enabled.is_(True),
        )
    )
    stale = (
        await session.execute(
            select(PlacementAssignmentEntity.id).where(
                PlacementAssignmentEntity.id.in_(list(found)),
                or_(
                    PlacementAssignmentEntity.active.is_(False),
                    not_(node_matches),
                    and_(PlacementAssignmentEntity.role == "recording", not_(recording_enabled)),
                    and_(PlacementAssignmentEntity.role == "ai", not_(ai_enabled)),
                ),
            )
        )
    ).scalars().all()
    if stale:
        raise PlacementRenewalInvalidated(
            "placement renewal invalidated: "
            f"{len(stale)} extended leases no longer match site or policy state"
        )


def _execution_keys(camera: CameraEntity, role: str) -> list:
    if role != "media":
        return []
    return [
        camera.stream_key,
        *([make_role_stream_key(camera.stream_key, "main")] if camera.sub_path else []),
        *([camera.third_stream_key] if camera.third_stream_key else []),
    ]


def _adopt_committed_assignment(row: PlacementAssignmentEntity, current) -> None:
    for name in (
        "node_id",
        "generation",
        "region_id",
        "lease_expires_at",
        "autonomy_expires_at",
        "active",
        "reason",
        "assigned_at",
        "applied_generation",
        "cleanup_node_ids_json",
    ):
        set_committed_value(row, name, getattr(current, name))


async def _committed_assignment_state(assignment_id: str):
    async with SessionLocal() as fresh:
        current = (
            await fresh.execute(
                select(PlacementAssignmentEntity).where(PlacementAssignmentEntity.id == assignment_id)
            )
        ).scalar_one_or_none()
        if current is None:
            return None
        return SimpleNamespace(
            node_id=current.node_id,
            generation=current.generation,
            region_id=current.region_id,
            lease_expires_at=current.lease_expires_at,
            autonomy_expires_at=current.autonomy_expires_at,
            active=current.active,
            reason=current.reason,
            assigned_at=current.assigned_at,
            applied_generation=current.applied_generation,
            cleanup_node_ids_json=list(current.cleanup_node_ids_json or []),
        )


def _raise_failover_superseded(row, current, camera, role, observed_node, observed_generation) -> None:
    log.critical(
        "placement_failover_superseded camera_id=%s role=%s observed_node=%s observed_generation=%s",
        camera.id,
        role,
        observed_node,
        observed_generation,
    )
    if current is not None:
        _adopt_committed_assignment(row, current)
    raise _FailoverSuperseded(row)


async def _assign_matching_owner(
    session,
    camera: CameraEntity,
    role: str,
    region_id: str,
    node: NodeSnapshot,
    row: PlacementAssignmentEntity,
    *,
    reason: str,
    now: datetime,
    lease: datetime,
    autonomy: datetime | None,
) -> None:
    """Write failover only while the loaded owner and generation are still current.

    A concurrent commit that advanced the generation makes this a no-op. The
    loaded object is marked with that committed state so the session cannot
    flush the stale owner by primary key.
    """
    observed_node = row.node_id
    observed_generation = row.generation
    ownership_changed = observed_node != node.id
    changed = ownership_changed or row.region_id != region_id
    await _before_assignment_update()
    current = await _committed_assignment_state(row.id)
    if (
        current is None
        or current.node_id != observed_node
        or current.generation != observed_generation
    ):
        _raise_failover_superseded(row, current, camera, role, observed_node, observed_generation)

    values = {
        "active": True,
        "lease_expires_at": lease,
        "autonomy_expires_at": autonomy,
        "updated_at": utcnow(),
    }
    cleanup = list(row.cleanup_node_ids_json or [])
    if changed:
        values.update(
            {
                "node_id": node.id,
                "region_id": region_id,
                "reason": reason,
                "assigned_at": now,
            }
        )
        if ownership_changed:
            cleanup = [old_id for old_id in cleanup if old_id != node.id]
            if observed_node not in cleanup:
                cleanup.append(observed_node)
            values["generation"] = observed_generation + 1
            values["applied_generation"] = None
            values["cleanup_node_ids_json"] = cleanup

    result = await session.execute(
        update(PlacementAssignmentEntity)
        .where(
            PlacementAssignmentEntity.id == row.id,
            PlacementAssignmentEntity.node_id == observed_node,
            PlacementAssignmentEntity.generation == observed_generation,
            PlacementAssignmentEntity.active.is_(True),
        )
        .values(**values)
        .returning(PlacementAssignmentEntity.id)
        .execution_options(synchronize_session=False)
    )
    if result.scalar_one_or_none() is None:
        current = await _committed_assignment_state(row.id)
        _raise_failover_superseded(row, current, camera, role, observed_node, observed_generation)

    for name, value in values.items():
        set_committed_value(row, name, value)

    if ownership_changed:
        valid_until = effective_authority_deadline(
            current.lease_expires_at,
            current.autonomy_expires_at,
            settings.placement_fence_expiry_grace_seconds,
        )
        session.add(
            PlacementRevocationEntity(
                assignment_id=row.id,
                camera_id=row.camera_id,
                role=row.role,
                node_id=observed_node,
                revoked_generation=observed_generation,
                execution_keys_json=_execution_keys(camera, role),
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
    elif _is_orm_session(session):
        await _assign_matching_owner(
            session,
            camera,
            role,
            region_id,
            node,
            row,
            reason=reason,
            now=now,
            lease=lease,
            autonomy=autonomy,
        )
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
    renewal_lock_not_acquired: bool = False,
    renewal_max_run_exceeded: bool = False,
    renewal_invalidated: bool = False,
    cursor: str | None = None,
) -> dict:
    return {
        "scanned": scanned,
        "moved": moved,
        "unplaced": unplaced,
        "deferred_autonomy": deferred_autonomy,
        "renewed": renewed,
        "renewal_budget_exceeded": renewal_budget_exceeded,
        "renewal_lock_not_acquired": renewal_lock_not_acquired,
        "renewal_max_run_exceeded": renewal_max_run_exceeded,
        "renewal_invalidated": renewal_invalidated,
        "cursor": cursor,
    }


async def run_placement_once() -> dict:
    """Run one bounded placement-controller iteration under the execution fence.

    Eligible owners are renewed from a cursor that does not follow the camera
    scan page, and only after the placement execution lock is held. Renewal
    keeps an owner only when committed site and policy state still authorize
    that owner, including a camera the scan page did not load. A missed lock
    is retried with a
    short backoff before this cycle gives up. Every attempt failing is logged.
    A live population above the renewal ceiling skips renewal and still scans.
    A positive max run is a deadline. Past it, this attempt rolls back and
    reports renewal_max_run_exceeded. A later renewal chunk that does not
    extend every owner, after an earlier chunk did, rolls the attempt back
    and reports renewal_invalidated. No partial lease page is committed.

    Returns:
        Dictionary containing scanned, moved, unplaced, deferred, and renewed
        counts, whether the live population exceeded the renewal ceiling,
        whether every lock attempt failed, whether the attempt passed its max
        run, whether a later chunk invalidated an earlier chunk, and the
        persisted camera-scan cursor. Deferred counts are
        assignments whose previous effective authority is still live. Renewed
        counts are owners the scan would keep whose lease moved later on this
        run's renewal page. An exceeded max run returns zeros because that
        attempt was rolled back.

    Raises:
        PlacementRenewalBudgetError: The configured budget is not strictly
            inside the lease. The lease is not shortened. A live population
            above the ceiling does not raise.
        Exception: Database, fencing or placement mutation failures propagate.
    """
    assert_settings_renewal_budget(settings)
    attempts = int(settings.placement_renewal_lock_retry_limit)
    for attempt in range(attempts):
        try:
            result = await _run_placement_once_locked()
        except PlacementRenewalRunExceeded:
            log.critical(
                "placement_renewal_max_run_exceeded max_run_seconds=%s",
                settings.placement_renewal_max_run_seconds,
            )
            return _placement_result(renewal_max_run_exceeded=True)
        except PlacementRenewalInvalidated:
            log.critical("placement_renewal_invalidated")
            return _placement_result(renewal_invalidated=True)
        if result is not None:
            return result
        if attempt + 1 < attempts:
            await asyncio.sleep(float(settings.placement_renewal_lock_retry_seconds))
    log.critical("placement_renewal_lock_not_acquired attempts=%s", attempts)
    return _placement_result(renewal_lock_not_acquired=True)


async def _run_placement_once_locked() -> dict | None:
    """Run one locked placement iteration.

    Returns:
        The run summary, or None when the execution lock was not acquired.

    Raises:
        PlacementRenewalBudgetError: Propagated from callers that validate
            settings before this attempt. This function does not raise it for
            a population above the ceiling.
        PlacementRenewalRunExceeded: The attempt passed its max run, or
            PostgreSQL cancelled a statement for that reason. The transaction
            is rolled back first.
        Exception: Database or placement mutation failures propagate.
    """
    now = datetime.now(timezone.utc)
    scanned = moved = unplaced = deferred_autonomy = renewed = 0
    renewal_budget_exceeded = False
    cursor: str | None = None
    deadline_token = _arm_run_deadline()
    extended_token = _EXTENDED_LEASE_IDS.set(set())
    try:
        async with SessionLocal() as session:
            async with session.begin():
                if not await _leader_lock(session):
                    return None
                await _arm_statement_timeout(session)

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
                candidates: list[_RenewalCandidate] = []
                renewal_cursor: str | None = None
                track_renewal_cursor = False
                if not renewal_budget_exceeded:
                    candidates, renewal_cursor = await _collect_renewal_candidates(session, nodes, now)
                    track_renewal_cursor = True

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
                    committed = await _load_committed_authority(
                        session, _cameras_for_authority([], candidates)
                    )
                    renewed = await _apply_renewal_candidates(
                        session,
                        candidates,
                        nodes,
                        now,
                        scan_camera_ids=set(),
                        scan_recording={},
                        scan_ai={},
                        scan_site_regions={},
                        committed=committed,
                    )
                    if track_renewal_cursor:
                        await _save_renewal_cursor(session, renewal_cursor)
                    await _reject_stale_extended_leases(session)
                    _raise_if_over_deadline()
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
                committed = await _load_committed_authority(
                    session, _cameras_for_authority(cameras, candidates)
                )
                renewed = await _apply_renewal_candidates(
                    session,
                    candidates,
                    nodes,
                    now,
                    scan_camera_ids=set(camera_ids),
                    scan_recording=recording,
                    scan_ai=ai,
                    scan_site_regions=site_regions,
                    committed=committed,
                )

                last_processed: str | None = None
                exhausted_moves = False
                for camera in cameras:
                    if moved >= settings.placement_max_moves_per_run:
                        exhausted_moves = True
                        break

                    scan_region = _site_region_id(site_regions, camera)
                    authority = committed[camera.id]
                    fresh_recording = _policy_view(authority.recording_enabled)
                    fresh_ai = _policy_view(authority.ai_enabled)

                    for role in ("media", "recording", "ai"):
                        if not _role_required(role, fresh_recording, fresh_ai):
                            continue
                        if not _role_required(role, recording.get(camera.id), ai.get(camera.id)):
                            continue

                        existing = assignments.get((camera.id, role))
                        observed_node = existing.node_id if existing is not None else None
                        observed_generation = existing.generation if existing is not None else None
                        kept = _owner_if_kept(
                            nodes,
                            existing,
                            role,
                            [authority.region_id, scan_region],
                            now,
                        )
                        if kept is not None and existing is not None:
                            await _keep_owner(
                                session,
                                existing,
                                kept,
                                recording,
                                now,
                                node_id=observed_node,
                                generation=observed_generation,
                            )
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

                        move_region = _failover_region(
                            nodes, existing, role, authority.region_id, scan_region, now
                        )
                        target = choose_node(nodes, role, move_region, now)
                        if target is None:
                            unplaced += 1
                            continue

                        reason = "initial" if existing is None else "failover"
                        try:
                            row, changed = await _assign(
                                session,
                                camera,
                                role,
                                move_region,
                                target,
                                existing,
                                reason=reason,
                                now=now,
                            )
                        except _FailoverSuperseded as superseded:
                            assignments[(camera.id, role)] = superseded.row
                            deferred_autonomy += 1
                            continue
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
                if track_renewal_cursor:
                    await _save_renewal_cursor(session, renewal_cursor)

                await _reject_stale_extended_leases(session)
                _raise_if_over_deadline()
                return _placement_result(
                    scanned=scanned,
                    moved=moved,
                    unplaced=unplaced,
                    deferred_autonomy=deferred_autonomy,
                    renewed=renewed,
                    renewal_budget_exceeded=renewal_budget_exceeded,
                    cursor=cursor,
                )
    except DBAPIError as error:
        if _is_statement_timeout(error):
            raise PlacementRenewalRunExceeded(
                "placement renewal max run exceeded: "
                f"max_run={settings.placement_renewal_max_run_seconds}s"
            ) from error
        raise
    finally:
        _RUN_DEADLINE.reset(deadline_token)
        _EXTENDED_LEASE_IDS.reset(extended_token)
