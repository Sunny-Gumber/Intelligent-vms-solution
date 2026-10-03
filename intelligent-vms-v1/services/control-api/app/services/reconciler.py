import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import settings
from app.core.security import decrypt_secret
from app.services.camera_lifecycle import main_live_source, main_live_stream_key, third_source
from app.db.session import SessionLocal
from app.services.coordination import try_placement_execution_lock
from app.models.entities import CameraEntity, RecordingPolicyEntity, ServiceStateEntity
from app.models.placement import InfrastructureNodeEntity, PlacementAssignmentEntity
from app.services.mediamtx import mediamtx
from app.services.node_media import NodeEndpointError, node_clients
from app.services.recording import provision_recording
from app.services.rtsp import build_rtsp_uri
from app.services.stream_keys import make_role_stream_key

log = logging.getLogger(__name__)
CURSOR_KEY = "media_reconcile_cursor"


@dataclass
class ReconcileStats:
    """Track the most recent media-reconciliation execution statistics.

    Attributes:
        last_started_monotonic: Monotonic start time of the latest run.
        last_duration_seconds: Latest run duration.
        last_scanned: Number of cameras scanned.
        last_changed: Number of external media/recording mutations applied.
        last_failed: Number of failed reconciliation operations.
        last_cursor: Persisted camera pagination cursor.
        total_runs: Completed reconciliation-run count.
    """

    last_started_monotonic: float = 0.0
    last_duration_seconds: float = 0.0
    last_scanned: int = 0
    last_changed: int = 0
    last_failed: int = 0
    last_cursor: str | None = None
    total_runs: int = 0


stats = ReconcileStats()


def source_for_camera(camera: CameraEntity) -> str:
    """Build the credential-bearing RTSP source URI used by the media node.

    Args:
        camera: Persisted camera entity with encrypted credentials and stream paths.

    Returns:
        RTSP source URI for the preferred substream or main stream.

    Raises:
        RuntimeError: If credential decryption is requested without VMS_SECRET_KEY.
        ValueError: If camera host/port/path values cannot form a valid RTSP URI.
    """
    path = camera.sub_path or camera.main_path
    return build_rtsp_uri(
        camera.host,
        camera.rtsp_port,
        path,
        decrypt_secret(camera.username_enc),
        decrypt_secret(camera.password_enc),
    )


async def _leader_lock(session) -> bool:
    # The reconciler shares the exact same transaction-scoped lock as placement.
    # Therefore placement cannot advance A/gen1 -> B/gen2 while a gen1 external
    # MediaMTX mutation is still in flight.
    return await try_placement_execution_lock(session)


async def _next_camera_batch(session):
    batch_size = max(1, settings.media_reconcile_batch_size)
    state = await session.get(ServiceStateEntity, CURSOR_KEY, with_for_update=True)
    if state is None:
        state = ServiceStateEntity(key=CURSOR_KEY, value_json={})
        session.add(state)
        await session.flush()

    cursor = (state.value_json or {}).get("camera_id")

    def query_from(after: str | None):
        q = select(CameraEntity).where(CameraEntity.enabled.is_(True))
        if after:
            q = q.where(CameraEntity.id > after)
        return q.order_by(CameraEntity.id).limit(batch_size)

    cameras = list((await session.execute(query_from(cursor))).scalars().all())
    if not cameras and cursor:
        cameras = list((await session.execute(query_from(None))).scalars().all())
    return cameras, state


def _live_path_needs_apply(
    present: set[str],
    reader_limits: dict[str, int | None],
    stream_key: str,
) -> bool:
    """Return whether a live path is absent or has stale reader-safety policy."""
    return (
        stream_key not in present
        or reader_limits.get(stream_key) != settings.live_view_max_readers_per_path
    )


def _assignment_live(row: PlacementAssignmentEntity | None, now: datetime) -> bool:
    if row is None or not row.active:
        return False
    expiry = row.lease_expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry > now


async def reconcile_batch(
    cameras: list[CameraEntity],
    present: set[str],
    policies: dict[str, RecordingPolicyEntity] | None = None,
) -> tuple[int, int]:
    """Run the backward-compatible bounded single-node reconciliation helper.

    Phase 2B/3 tests and integrations use this helper directly. The distributed
    controller uses the internal distributed reconciler instead.

    Args:
        cameras: Camera entities to reconcile.
        present: Mutable set of currently configured MediaMTX path names.
        policies: Optional recording policies keyed by camera identifier.

    Returns:
        Tuple containing number of applied changes and failed operations.
    """
    changed = 0
    failed = 0
    policies = policies or {}
    max_changes = max(1, settings.media_reconcile_max_changes_per_run)
    # This compatibility helper receives names only, so treat already-present
    # paths as policy-current. Production reconciliation inspects maxReaders.
    reader_limits = {key: settings.live_view_max_readers_per_path for key in present}

    for camera in cameras:
        if not camera.enabled or changed >= max_changes:
            continue
        third_key = camera.third_stream_key or make_role_stream_key(camera.stream_key, "third")
        if _live_path_needs_apply(present, reader_limits, camera.stream_key):
            try:
                await mediamtx.add_or_replace_path(camera.stream_key, source_for_camera(camera))
                present.add(camera.stream_key)
                camera.desired_state = "provisioned"
                changed += 1
            except Exception:
                failed += 1
                log.exception(
                    "media_reconcile_path_failed camera_id=%s stream_key=%s",
                    camera.id,
                    camera.stream_key,
                )

        if camera.sub_path and changed < max_changes:
            main_key = main_live_stream_key(camera)
            if _live_path_needs_apply(present, reader_limits, main_key):
                try:
                    await mediamtx.add_or_replace_path(main_key, main_live_source(camera))
                    present.add(main_key)
                    changed += 1
                except Exception:
                    failed += 1
                    log.exception("main_live_reconcile_path_failed camera_id=%s", camera.id)
        elif not camera.sub_path and changed < max_changes:
            stale_main_key = make_role_stream_key(camera.stream_key, "main")
            if stale_main_key in present:
                try:
                    await mediamtx.delete_path(stale_main_key)
                    present.discard(stale_main_key)
                    changed += 1
                except Exception:
                    failed += 1
                    log.exception("main_live_reconcile_cleanup_failed camera_id=%s", camera.id)

        if (
            camera.third_path
            and camera.third_stream_key
            and _live_path_needs_apply(present, reader_limits, camera.third_stream_key)
            and changed < max_changes
        ):
            try:
                source = third_source(camera)
                if source is not None:
                    await mediamtx.add_or_replace_path(camera.third_stream_key, source)
                    present.add(camera.third_stream_key)
                    changed += 1
            except Exception:
                failed += 1
                log.exception(
                    "third_reconcile_path_failed camera_id=%s stream_key=%s",
                    camera.id,
                    camera.third_stream_key,
                )
        elif (
            not camera.third_path
            and third_key in present
            and changed < max_changes
        ):
            try:
                await mediamtx.delete_path(third_key)
                present.discard(third_key)
                changed += 1
            except Exception:
                failed += 1
                log.exception(
                    "third_reconcile_cleanup_failed camera_id=%s stream_key=%s",
                    camera.id,
                    third_key,
                )

        policy = policies.get(camera.id)
        if (
            policy
            and policy.enabled
            and policy.mode == "continuous"
            and policy.record_stream_key not in present
            and changed < max_changes
        ):
            try:
                await provision_recording(camera, policy)
                present.add(policy.record_stream_key)
                changed += 1
            except Exception:
                failed += 1
                log.exception(
                    "record_reconcile_path_failed camera_id=%s stream_key=%s",
                    camera.id,
                    policy.record_stream_key,
                )
    return changed, failed


async def _legacy_reconcile(
    cameras: list[CameraEntity],
    policies: dict[str, RecordingPolicyEntity],
    *,
    max_changes: int,
) -> tuple[int, int, str | None]:
    changed = failed = 0
    last_processed = None
    try:
        configured = await mediamtx.list_config_paths()
        configured_items = (
            configured.get("items", []) if isinstance(configured, dict) else []
        )
        present = {
            item.get("name")
            for item in configured_items
            if isinstance(item, dict) and item.get("name")
        }
        reader_limits = {
            item["name"]: item.get("maxReaders")
            for item in configured_items
            if isinstance(item, dict) and item.get("name")
        }
    except Exception:
        log.exception("legacy_media_reconcile_list_failed")
        return 0, 1, None

    for camera in cameras:
        if changed >= max_changes:
            break
        third_key = camera.third_stream_key or make_role_stream_key(camera.stream_key, "third")
        if _live_path_needs_apply(present, reader_limits, camera.stream_key):
            try:
                await mediamtx.add_or_replace_path(camera.stream_key, source_for_camera(camera))
                present.add(camera.stream_key)
                camera.desired_state = "provisioned"
                changed += 1
            except Exception:
                failed += 1
                log.exception("media_reconcile_path_failed camera_id=%s", camera.id)

        if camera.sub_path and changed < max_changes:
            main_key = main_live_stream_key(camera)
            if _live_path_needs_apply(present, reader_limits, main_key):
                try:
                    await mediamtx.add_or_replace_path(main_key, main_live_source(camera))
                    present.add(main_key)
                    changed += 1
                except Exception:
                    failed += 1
                    log.exception("main_live_reconcile_path_failed camera_id=%s", camera.id)
        elif not camera.sub_path and changed < max_changes:
            stale_main_key = make_role_stream_key(camera.stream_key, "main")
            if stale_main_key in present:
                try:
                    await mediamtx.delete_path(stale_main_key)
                    present.discard(stale_main_key)
                    changed += 1
                except Exception:
                    failed += 1
                    log.exception("main_live_reconcile_cleanup_failed camera_id=%s", camera.id)

        if (
            camera.third_path
            and camera.third_stream_key
            and _live_path_needs_apply(present, reader_limits, camera.third_stream_key)
            and changed < max_changes
        ):
            try:
                source = third_source(camera)
                if source is not None:
                    await mediamtx.add_or_replace_path(camera.third_stream_key, source)
                    present.add(camera.third_stream_key)
                    changed += 1
            except Exception as exc:
                failed += 1
                log.error("third_reconcile_path_failed camera_id=%s error_class=%s", camera.id, exc.__class__.__name__)
        elif (
            not camera.third_path
            and third_key in present
            and changed < max_changes
        ):
            try:
                await mediamtx.delete_path(third_key)
                present.discard(third_key)
                changed += 1
            except Exception as exc:
                failed += 1
                log.error("third_reconcile_cleanup_failed camera_id=%s error_class=%s", camera.id, exc.__class__.__name__)

        policy = policies.get(camera.id)
        if policy and policy.enabled and policy.mode == "continuous" and policy.record_stream_key not in present:
            try:
                await provision_recording(camera, policy)
                present.add(policy.record_stream_key)
                changed += 1
            except Exception:
                failed += 1
                log.exception("record_reconcile_path_failed camera_id=%s", camera.id)
        last_processed = camera.id
    return changed, failed, last_processed


async def _distributed_reconcile(
    cameras: list[CameraEntity],
    policies: dict[str, RecordingPolicyEntity],
    assignments: dict[tuple[str, str], PlacementAssignmentEntity],
    nodes: dict[str, InfrastructureNodeEntity],
    *,
    max_changes: int,
) -> tuple[int, int, str | None]:
    changed = failed = 0
    last_processed = None
    now = datetime.now(timezone.utc)
    clients = {}
    present_by_node: dict[str, set[str] | None] = {}
    reader_limits_by_node: dict[str, dict[str, int | None]] = {}

    async def client_and_present(node_id: str):
        nonlocal failed
        if node_id in present_by_node:
            node = nodes.get(node_id)
            if node is None or present_by_node[node_id] is None:
                return None, None, None
            return clients[node_id], present_by_node[node_id], reader_limits_by_node[node_id]

        node = nodes.get(node_id)
        if node is None:
            present_by_node[node_id] = None
            failed += 1
            log.error("reconcile_node_missing node_id=%s", node_id)
            return None, None, None
        try:
            client = await node_clients.media(node)
            configured = await client.list_config_paths()
            configured_items = (
                configured.get("items", []) if isinstance(configured, dict) else []
            )
            present = {
                item.get("name")
                for item in configured_items
                if isinstance(item, dict) and item.get("name")
            }
            reader_limits = {
                item["name"]: item.get("maxReaders")
                for item in configured_items
                if isinstance(item, dict) and item.get("name")
            }
            clients[node_id] = client
            present_by_node[node_id] = present
            reader_limits_by_node[node_id] = reader_limits
            return client, present, reader_limits
        except Exception:
            present_by_node[node_id] = None
            failed += 1
            log.exception("reconcile_node_unreachable node_id=%s", node_id)
            return None, None, None

    async def cleanup_stale_nodes(
        row: PlacementAssignmentEntity,
        stream_keys: list[str],
        *,
        current_ready: bool,
    ) -> None:
        nonlocal changed, failed
        pending = list(row.cleanup_node_ids_json or [])
        if not current_ready or not pending:
            return
        remaining: list[str] = []
        for old_id in pending:
            if old_id == row.node_id:
                continue
            if changed + len(stream_keys) > max_changes:
                remaining.append(old_id)
                continue
            client, _present, _limits = await client_and_present(old_id)
            if client is None:
                remaining.append(old_id)
                continue
            try:
                for stream_key in stream_keys:
                    await client.delete_path(stream_key)
                    changed += 1
                log.info(
                    "placement_old_paths_removed camera_id=%s role=%s old_node=%s",
                    row.camera_id,
                    row.role,
                    old_id,
                )
            except Exception as exc:
                remaining.append(old_id)
                failed += 1
                log.error(
                    "placement_old_path_cleanup_failed camera_id=%s role=%s old_node=%s error_class=%s",
                    row.camera_id,
                    row.role,
                    old_id,
                    exc.__class__.__name__,
                )
        row.cleanup_node_ids_json = remaining

    for camera in cameras:
        if changed >= max_changes:
            break

        media_assignment = assignments.get((camera.id, "media"))
        if _assignment_live(media_assignment, now):
            client, present, reader_limits = await client_and_present(media_assignment.node_id)
            media_ready = False
            if client is not None and present is not None and reader_limits is not None:
                needs_apply = (
                    _live_path_needs_apply(present, reader_limits, camera.stream_key)
                    or media_assignment.applied_generation != media_assignment.generation
                )
                explicit_main_key = make_role_stream_key(camera.stream_key, "main")
                explicit_main_needs_apply = bool(
                    camera.sub_path
                    and (
                        needs_apply
                        or _live_path_needs_apply(present, reader_limits, explicit_main_key)
                    )
                )
                explicit_main_needs_delete = bool(
                    not camera.sub_path and explicit_main_key in present
                )
                third_needs_apply = bool(
                    camera.third_path
                    and camera.third_stream_key
                    and (
                        needs_apply
                        or _live_path_needs_apply(
                            present,
                            reader_limits,
                            camera.third_stream_key,
                        )
                    )
                )
                third_needs_delete = bool(
                    not camera.third_path
                    and camera.third_stream_key
                    and camera.third_stream_key in present
                )
                required_changes = int(needs_apply) + int(explicit_main_needs_apply or explicit_main_needs_delete) + int(third_needs_apply or third_needs_delete)
                insufficient_budget = changed + required_changes > max_changes
                if insufficient_budget:
                    # A role refresh needs enough budget for both stream paths;
                    # do not accept a partly refreshed generation or starve
                    # recording with repeated partial media mutations.
                    needs_apply = False
                    explicit_main_needs_apply = False
                    third_needs_apply = False
                main_ready = camera.stream_key in present
                if needs_apply:
                    try:
                        await client.add_or_replace_path(
                            camera.stream_key,
                            source_for_camera(camera),
                        )
                        present.add(camera.stream_key)
                        main_ready = True
                        changed += 1
                    except Exception as exc:
                        main_ready = False
                        failed += 1
                        log.error(
                            "assigned_media_provision_failed camera_id=%s node_id=%s generation=%s error_class=%s",
                            camera.id,
                            media_assignment.node_id,
                            media_assignment.generation,
                            exc.__class__.__name__,
                        )

                explicit_main_ready = not camera.sub_path or explicit_main_key in present
                if explicit_main_needs_delete and not insufficient_budget:
                    try:
                        await client.delete_path(explicit_main_key)
                        present.discard(explicit_main_key)
                        changed += 1
                    except Exception as exc:
                        explicit_main_ready = False
                        failed += 1
                        log.error(
                            "assigned_main_live_cleanup_failed camera_id=%s node_id=%s error_class=%s",
                            camera.id,
                            media_assignment.node_id,
                            exc.__class__.__name__,
                        )
                if explicit_main_needs_apply:
                    try:
                        await client.add_or_replace_path(
                            explicit_main_key,
                            main_live_source(camera),
                        )
                        present.add(explicit_main_key)
                        explicit_main_ready = True
                        changed += 1
                    except Exception as exc:
                        explicit_main_ready = False
                        failed += 1
                        log.error(
                            "assigned_main_live_provision_failed camera_id=%s node_id=%s error_class=%s",
                            camera.id,
                            media_assignment.node_id,
                            exc.__class__.__name__,
                        )

                third_ready = True
                if camera.third_path and camera.third_stream_key:
                    third_apply_ok = not (third_needs_apply or insufficient_budget)
                    if third_needs_apply:
                        try:
                            source = third_source(camera)
                            if source is not None:
                                await client.add_or_replace_path(
                                    camera.third_stream_key,
                                    source,
                                )
                                present.add(camera.third_stream_key)
                                third_apply_ok = True
                                changed += 1
                        except Exception as exc:
                            third_apply_ok = False
                            failed += 1
                            log.error(
                                "assigned_third_provision_failed camera_id=%s node_id=%s error_class=%s",
                                camera.id,
                                media_assignment.node_id,
                                exc.__class__.__name__,
                            )
                    third_ready = (
                        camera.third_stream_key in present and third_apply_ok
                    )
                elif (
                    camera.third_stream_key
                    and camera.third_stream_key in present
                    and not insufficient_budget
                ):
                    try:
                        await client.delete_path(camera.third_stream_key)
                        present.discard(camera.third_stream_key)
                        changed += 1
                    except Exception as exc:
                        failed += 1
                        third_ready = False
                        log.error(
                            "assigned_third_cleanup_failed camera_id=%s node_id=%s error_class=%s",
                            camera.id,
                            media_assignment.node_id,
                            exc.__class__.__name__,
                        )

                media_ready = main_ready and explicit_main_ready and third_ready
                if insufficient_budget:
                    media_ready = False
                if needs_apply:
                    if media_ready:
                        media_assignment.applied_generation = media_assignment.generation
                    else:
                        media_assignment.applied_generation = None
                else:
                    media_ready = (
                        media_ready
                        and media_assignment.applied_generation
                        == media_assignment.generation
                    )
                if media_ready:
                    camera.media_node_id = media_assignment.node_id
                    camera.desired_state = "provisioned"
                elif (
                    media_assignment.applied_generation != media_assignment.generation
                    or explicit_main_needs_apply
                    or explicit_main_needs_delete
                    or third_needs_apply
                    or third_needs_delete
                    or insufficient_budget
                ):
                    camera.desired_state = "pending-source-refresh"
            await cleanup_stale_nodes(
                media_assignment,
                [
                    camera.stream_key,
                    *([main_live_stream_key(camera)] if camera.sub_path else []),
                    *([camera.third_stream_key] if camera.third_stream_key else []),
                ],
                current_ready=media_ready,
            )

        policy = policies.get(camera.id)
        recording_assignment = assignments.get((camera.id, "recording"))
        if (
            policy
            and policy.enabled
            and policy.mode == "continuous"
            and _assignment_live(recording_assignment, now)
        ):
            client, present, _reader_limits = await client_and_present(recording_assignment.node_id)
            record_ready = False
            if client is not None and present is not None:
                needs_apply = (
                    policy.record_stream_key not in present
                    or recording_assignment.applied_generation
                    != recording_assignment.generation
                )
                if needs_apply:
                    try:
                        await provision_recording(
                            camera,
                            policy,
                            client=client,
                            recording_node_id=recording_assignment.node_id,
                            assignment_generation=recording_assignment.generation,
                        )
                        present.add(policy.record_stream_key)
                        recording_assignment.applied_generation = (
                            recording_assignment.generation
                        )
                        changed += 1
                    except Exception:
                        failed += 1
                        log.exception(
                            "assigned_record_provision_failed camera_id=%s node_id=%s generation=%s",
                            camera.id,
                            recording_assignment.node_id,
                            recording_assignment.generation,
                        )
                record_ready = (
                    policy.record_stream_key in present
                    and recording_assignment.applied_generation
                    == recording_assignment.generation
                )
                if record_ready:
                    policy.recording_node_id = recording_assignment.node_id
            await cleanup_stale_nodes(
                recording_assignment,
                [policy.record_stream_key],
                current_ready=record_ready,
            )

        last_processed = camera.id

    return changed, failed, last_processed


async def run_once() -> ReconcileStats:
    """Run one bounded media/recording reconciliation iteration.

    Returns:
        Shared ReconcileStats updated with the latest run measurements.

    Raises:
        Exception: Database, placement-lock or uncaught reconciliation failures
            propagate to the caller.
    """
    start = time.monotonic()
    scanned = changed = failed = 0
    cursor = None

    async with SessionLocal() as session:
        async with session.begin():
            if not await _leader_lock(session):
                return stats

            cameras, state = await _next_camera_batch(session)
            scanned = len(cameras)
            if not cameras:
                state.value_json = {"camera_id": None}
                return stats

            camera_ids = [camera.id for camera in cameras]
            policies = {
                row.camera_id: row
                for row in (
                    await session.execute(
                        select(RecordingPolicyEntity).where(
                            RecordingPolicyEntity.camera_id.in_(camera_ids)
                        )
                    )
                ).scalars().all()
            }

            if settings.placement_execution_enabled:
                assignment_rows = (
                    await session.execute(
                        select(PlacementAssignmentEntity).where(
                            PlacementAssignmentEntity.camera_id.in_(camera_ids),
                            PlacementAssignmentEntity.role.in_(["media", "recording"]),
                        )
                    )
                ).scalars().all()
                assignments = {(row.camera_id, row.role): row for row in assignment_rows}
                node_ids = {
                    node_id
                    for row in assignment_rows
                    for node_id in ([row.node_id] + list(row.cleanup_node_ids_json or []))
                    if node_id
                }
                nodes = {}
                if node_ids:
                    node_rows = (
                        await session.execute(
                            select(InfrastructureNodeEntity).where(
                                InfrastructureNodeEntity.id.in_(node_ids)
                            )
                        )
                    ).scalars().all()
                    nodes = {node.id: node for node in node_rows}
                changed, failed, last_processed = await _distributed_reconcile(
                    cameras,
                    policies,
                    assignments,
                    nodes,
                    max_changes=max(1, settings.media_reconcile_max_changes_per_run),
                )
            else:
                changed, failed, last_processed = await _legacy_reconcile(
                    cameras,
                    policies,
                    max_changes=max(1, settings.media_reconcile_max_changes_per_run),
                )

            if last_processed is None:
                cursor = (state.value_json or {}).get("camera_id")
            elif last_processed != cameras[-1].id:
                cursor = last_processed
            elif len(cameras) >= max(1, settings.media_reconcile_batch_size):
                cursor = last_processed
            else:
                cursor = None
            state.value_json = {"camera_id": cursor}

    stats.last_started_monotonic = start
    stats.last_duration_seconds = time.monotonic() - start
    stats.last_scanned = scanned
    stats.last_changed = changed
    stats.last_failed = failed
    stats.last_cursor = cursor
    stats.total_runs += 1
    log.info(
        "media_reconcile_complete mode=%s scanned=%d changed=%d failed=%d cursor=%s duration=%.3f",
        "distributed" if settings.placement_execution_enabled else "legacy",
        scanned,
        changed,
        failed,
        cursor or "wrap",
        stats.last_duration_seconds,
    )
    return stats


class ReconcilerLoop:
    """Manage periodic media/recording reconciliation as a background task."""

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self):
        """Start periodic reconciliation when enabled.

        Returns:
            None. Repeated starts are idempotent while a task already exists.
        """
        if not settings.media_reconcile_enabled or self._task:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="media-reconciler")

    async def stop(self):
        """Cancel and release the periodic reconciliation task.

        Returns:
            None after the background task has stopped.
        """
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self):
        while not self._stop.is_set():
            try:
                await run_once()
            except Exception:
                log.exception("media_reconcile_run_failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=max(5.0, settings.media_reconcile_interval_seconds),
                )
            except asyncio.TimeoutError:
                pass


reconciler_loop = ReconcilerLoop()
