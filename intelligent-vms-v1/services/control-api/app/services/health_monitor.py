import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

from sqlalchemy import select

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.entities import CameraEntity, CameraHealthStateEntity, ServiceStateEntity
from app.models.placement import InfrastructureNodeEntity
from app.services.outbox import enqueue_event
from app.services.local_event_store import persist_local_event_once
from app.services.mediamtx import mediamtx
from app.services.node_media import node_clients

log = logging.getLogger(__name__)
CURSOR_KEY = "health_monitor_cursor"


@dataclass
class MonitorStats:
    """Track camera-health monitor execution statistics.

    Attributes:
        total_runs: Completed monitor iterations.
        last_duration_seconds: Latest run duration.
        last_scanned: Cameras scanned in the latest run.
        last_changed: Health-state transitions in the latest run.
        media_errors: Accumulated media-path collection failures.
        last_cursor: Persisted pagination cursor.
    """

    total_runs: int = 0
    last_duration_seconds: float = 0.0
    last_scanned: int = 0
    last_changed: int = 0
    media_errors: int = 0
    last_cursor: str | None = None


@dataclass
class Counters:
    """Hold consecutive health probe failure/success counters.

    Attributes:
        failures: Consecutive failed transport probes.
        successes: Consecutive successful transport probes.
    """

    failures: int = 0
    successes: int = 0


stats = MonitorStats()


def evaluate_state(
    current: str,
    *,
    ready: bool,
    counters: Counters,
    failure_threshold: int,
    recovery_threshold: int,
) -> tuple[str, Counters]:
    """Advance camera health state using bounded failure/recovery hysteresis.

    Args:
        current: Current camera health state.
        ready: Result of the latest transport probe.
        counters: Existing consecutive success/failure counters.
        failure_threshold: Failures required before declaring offline.
        recovery_threshold: Successes required before declaring online.

    Returns:
        Tuple of next state and updated counters.
    """
    failure_threshold = max(1, failure_threshold)
    recovery_threshold = max(1, recovery_threshold)

    if ready:
        next_counters = Counters(
            failures=0,
            successes=min(recovery_threshold, counters.successes + 1),
        )
        if current == "online":
            return "online", next_counters
        if next_counters.successes >= recovery_threshold:
            return "online", next_counters
        return current if current != "unknown" else "unknown", next_counters

    next_counters = Counters(
        failures=min(failure_threshold, counters.failures + 1),
        successes=0,
    )
    if next_counters.failures >= failure_threshold:
        return "offline", next_counters
    if current in {"online", "unknown"}:
        return "degraded", next_counters
    return current, next_counters


def safe_path_detail(item: dict | None) -> dict:
    """Return only approved non-sensitive MediaMTX path diagnostic fields.

    Args:
        item: Raw MediaMTX path dictionary, if available.

    Returns:
        Bounded dictionary containing only safe readiness/traffic fields.
    """
    if not isinstance(item, dict):
        return {}
    allowed = ("ready", "tracks", "bytesReceived", "bytesSent", "readers")
    return {key: item[key] for key in allowed if key in item}


async def probe_rtsp_transport(host: str, port: int, timeout_seconds: float) -> bool:
    """Probe RTSP TCP reachability while safely cleaning up the socket.

    Args:
        host: Camera hostname or IP address.
        port: Camera RTSP TCP port.
        timeout_seconds: Maximum connection time in seconds.

    Returns:
        True when the TCP connection succeeds, otherwise False. A writer-close
        cleanup failure is logged without exposing the exception message.
    """
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host=host, port=port),
            timeout=max(0.1, timeout_seconds),
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception as exc:
            log.warning(
                "health_probe_writer_close_failed error_type=%s",
                exc.__class__.__name__,
            )
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def _claim_camera_batch() -> tuple[list[CameraEntity], str | None]:
    """Claim the next camera slice with only a short DB transaction.

    The cursor row is locked, so multiple health workers split batches instead of
    probing the same camera set. Network I/O happens after this transaction commits.
    """
    async with SessionLocal() as session:
        async with session.begin():
            state = await session.get(ServiceStateEntity, CURSOR_KEY, with_for_update=True)
            if state is None:
                state = ServiceStateEntity(key=CURSOR_KEY, value_json={})
                session.add(state)
                await session.flush()

            cursor = (state.value_json or {}).get("camera_id")
            batch = max(1, settings.health_monitor_batch_size)

            def query(after):
                q = select(CameraEntity).where(CameraEntity.enabled.is_(True))
                if after:
                    q = q.where(CameraEntity.id > after)
                return q.order_by(CameraEntity.id).limit(batch)

            cameras = list((await session.execute(query(cursor))).scalars().all())
            if not cameras and cursor:
                cameras = list((await session.execute(query(None))).scalars().all())

            next_cursor = cameras[-1].id if len(cameras) >= batch else None
            state.value_json = {"camera_id": next_cursor}

            # Materialize simple column values before session closes.
            for camera in cameras:
                _ = (
                    camera.id,
                    camera.tenant_id,
                    camera.site_id,
                    camera.host,
                    camera.rtsp_port,
                    camera.stream_key,
                    camera.media_node_id,
                )
            return cameras, next_cursor


def _absorb_media_paths(
    node_id: str,
    paths: object,
    by_name: dict[tuple[str, str], dict],
    untrusted_media_nodes: set[str],
) -> None:
    """Index one MediaMTX path list, or keep prior state when it is not complete.

    Args:
        node_id: Node the list was read from.
        paths: ``list_paths`` payload.
        by_name: Mutable map of ``(node_id, path name)`` to path item.
        untrusted_media_nodes: Nodes whose lists must not be treated as complete.

    Returns:
        None. Truncation and inconsistency are logged and recorded on
        ``untrusted_media_nodes`` so a partial page cannot clear ``path_present``.
    """
    if not isinstance(paths, dict):
        return
    items = paths.get("items", [])
    if not isinstance(items, list):
        items = []
    item_count = paths.get("itemCount")
    short = (
        isinstance(item_count, int)
        and not isinstance(item_count, bool)
        and len(items) < item_count
    )
    if paths.get("truncated") is True or paths.get("inconsistent") is True or short:
        stats.media_errors += 1
        untrusted_media_nodes.add(node_id)
        log.error(
            "health_media_list_untrusted node_id=%s truncated=%s inconsistent=%s collected=%s item_count=%s page_count=%s",
            node_id,
            paths.get("truncated"),
            paths.get("inconsistent"),
            len(items),
            paths.get("itemCount"),
            paths.get("pageCount"),
        )
        return
    for item in items:
        if isinstance(item, dict) and item.get("name"):
            by_name[(node_id, item["name"])] = item


class HealthMonitor:
    """Probe camera reachability and persist hysteresis-based health state/events."""

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self):
        """Start the background health monitor when enabled.

        Returns:
            None. Repeated starts are idempotent while a task already exists.
        """
        if not settings.health_monitor_enabled or self._task:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="camera-health-monitor")

    async def stop(self):
        """Cancel and release the background health-monitor task.

        Returns:
            None after the monitor task has stopped.
        """
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run_once(self):
        """Run one bounded camera-health monitoring iteration.

        Returns:
            Shared MonitorStats updated with the latest run measurements.

        Raises:
            Exception: Database/media-node failures not handled as diagnostics
                propagate to the caller.
        """
        started = time.monotonic()

        cameras, cursor = await _claim_camera_batch()
        scanned = len(cameras)

        # Media path state is diagnostic only because live paths can be
        # source-on-demand and legitimately idle. In distributed mode it must
        # be collected from each assigned node, never from one global server.
        # Nodes in untrusted_media_nodes had a truncated, inconsistent, or
        # failed list. Their absence must not clear a previously observed path.
        by_name: dict[tuple[str, str], dict] = {}
        untrusted_media_nodes: set[str] = set()
        if settings.placement_execution_enabled and cameras:
            node_ids = sorted({camera.media_node_id for camera in cameras if camera.media_node_id})
            async with SessionLocal() as node_session:
                nodes = {}
                if node_ids:
                    rows = (
                        await node_session.execute(
                            select(InfrastructureNodeEntity).where(
                                InfrastructureNodeEntity.id.in_(node_ids)
                            )
                        )
                    ).scalars().all()
                    nodes = {node.id: node for node in rows}

            async def read_node(node_id: str):
                node = nodes.get(node_id)
                if node is None:
                    return node_id, None
                try:
                    client = await node_clients.media(node)
                    return node_id, await client.list_paths()
                except Exception:
                    log.exception("health_media_node_unreachable node_id=%s", node_id)
                    return node_id, None

            results = await asyncio.gather(*(read_node(node_id) for node_id in node_ids))
            for node_id, paths in results:
                if paths is None:
                    stats.media_errors += 1
                    untrusted_media_nodes.add(node_id)
                    continue
                _absorb_media_paths(node_id, paths, by_name, untrusted_media_nodes)
        else:
            try:
                paths = await mediamtx.list_paths()
                _absorb_media_paths(
                    settings.placement_local_node_id,
                    paths,
                    by_name,
                    untrusted_media_nodes,
                )
            except Exception:
                stats.media_errors += 1
                untrusted_media_nodes.add(settings.placement_local_node_id)
                log.exception("health_media_node_unreachable")

        semaphore = asyncio.Semaphore(max(1, settings.health_probe_concurrency))

        async def bounded_probe(camera: CameraEntity):
            async with semaphore:
                result = await probe_rtsp_transport(
                    camera.host,
                    camera.rtsp_port,
                    settings.health_probe_timeout_seconds,
                )
                return camera.id, result

        probe_results = dict(
            await asyncio.gather(*(bounded_probe(camera) for camera in cameras))
        ) if cameras else {}

        now = datetime.now(timezone.utc)
        changed = 0
        ids = [camera.id for camera in cameras]

        async with SessionLocal() as session:
            async with session.begin():
                existing = {}
                if ids:
                    rows = (
                        await session.execute(
                            select(CameraHealthStateEntity)
                            .where(CameraHealthStateEntity.camera_id.in_(ids))
                            .with_for_update()
                        )
                    ).scalars().all()
                    existing = {row.camera_id: row for row in rows}

                for camera in cameras:
                    node_key = (
                        camera.media_node_id
                        if settings.placement_execution_enabled
                        else settings.placement_local_node_id
                    )
                    row = existing.get(camera.id)
                    if node_key in untrusted_media_nodes:
                        item = None
                        path_present = bool(row.path_present) if row is not None else False
                        media_ready = False
                    else:
                        item = by_name.get((node_key, camera.stream_key))
                        path_present = item is not None
                        media_ready = bool(item and item.get("ready"))
                    transport_ready = bool(probe_results.get(camera.id, False))
                    if row is None:
                        row = CameraHealthStateEntity(
                            camera_id=camera.id,
                            state="unknown",
                            path_present=False,
                            ready=False,
                            failure_count=0,
                            success_count=0,
                            detail_json={},
                            observed_at=now,
                            changed_at=now,
                        )
                        session.add(row)

                    before = row.state
                    after, next_counters = evaluate_state(
                        before,
                        ready=transport_ready,
                        counters=Counters(
                            failures=row.failure_count or 0,
                            successes=row.success_count or 0,
                        ),
                        failure_threshold=settings.health_failure_threshold,
                        recovery_threshold=settings.health_recovery_threshold,
                    )

                    heartbeat_due = (
                        row.observed_at is None
                        or now - row.observed_at
                        >= timedelta(seconds=settings.health_heartbeat_seconds)
                    )
                    state_changed = after != before
                    counters_changed = (
                        row.failure_count != next_counters.failures
                        or row.success_count != next_counters.successes
                    )
                    diagnostic_changed = (
                        row.path_present != path_present
                        or row.ready != transport_ready
                    )

                    if state_changed or counters_changed or heartbeat_due or diagnostic_changed:
                        row.state = after
                        row.path_present = path_present
                        row.ready = transport_ready
                        row.failure_count = next_counters.failures
                        row.success_count = next_counters.successes
                        row.detail_json = {
                            "transport": "rtsp_tcp",
                            "transport_ready": transport_ready,
                            "media_path_present": path_present,
                            "media_ready": media_ready,
                            **safe_path_detail(item),
                        }
                        row.observed_at = now
                        if state_changed:
                            row.changed_at = now
                            changed += 1

                    event_type = None
                    severity = "info"
                    if after == "offline" and before != "offline":
                        event_type = "camera_offline"
                        severity = "high"
                    elif before == "offline" and after == "online":
                        event_type = "camera_recovered"

                    if event_type:
                        event = {
                            "event_id": str(uuid.uuid4()),
                            "tenant_id": camera.tenant_id,
                            "site_id": camera.site_id,
                            "camera_id": camera.id,
                            "timestamp": now.isoformat(),
                            "event_type": event_type,
                            "object_type": None,
                            "source": "vms",
                            "confidence": None,
                            "zone_id": None,
                            "severity": severity,
                            "snapshot_uri": None,
                            "recording_start": None,
                            "recording_end": None,
                            "attributes": {
                                "media_node_id": camera.media_node_id,
                                "stream_key": camera.stream_key,
                                "probe": "rtsp_tcp",
                            },
                        }
                        # Same transaction as the health-state mutation: either
                        # both commit, or neither does. Reduced Windows deployments
                        # persist locally instead of creating an undeliverable outbox row.
                        if settings.event_local_store_enabled:
                            await persist_local_event_once(session, event)
                        elif settings.event_pipeline_enabled:
                            enqueue_event(session, event)

        stats.total_runs += 1
        stats.last_duration_seconds = time.monotonic() - started
        stats.last_scanned = scanned
        stats.last_changed = changed
        stats.last_cursor = cursor
        log.info(
            "health_monitor_complete scanned=%d changed=%d cursor=%s duration=%.3f",
            scanned,
            changed,
            cursor or "wrap",
            stats.last_duration_seconds,
        )
        return stats

    async def _run(self):
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                log.exception("health_monitor_run_failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=max(2.0, settings.health_monitor_interval_seconds),
                )
            except asyncio.TimeoutError:
                pass


health_monitor = HealthMonitor()
