from __future__ import annotations

import time
from collections.abc import Callable

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.services.operational_metrics import (
    AUTHORITY_MODES,
    CAMERA_STATES,
    PLACEMENT_ROLES,
    NODE_STATES,
    collect_operational_snapshot,
)
from app.services.outbox import outbox_counts
from app.services.reconciler import stats as reconciler_stats


router = APIRouter(tags=["observability"])

HTTP_REQUESTS_TOTAL = Counter(
    "intelligent_vms_http_requests_total",
    "Total HTTP requests handled by the Intelligent VMS control API.",
    ["method", "path", "status"],
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "intelligent_vms_http_request_duration_seconds",
    "Control API request latency in seconds.",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
HTTP_IN_FLIGHT_REQUESTS = Gauge(
    "intelligent_vms_http_in_flight_requests",
    "Current in-flight control API requests.",
)
READY = Gauge(
    "intelligent_vms_ready",
    "Whether the control API's required database dependency is ready.",
)
RECONCILER_TOTAL_RUNS = Gauge(
    "intelligent_vms_reconciler_total_runs",
    "Total reconciler runs observed in this process.",
)
RECONCILER_LAST_DURATION_SECONDS = Gauge(
    "intelligent_vms_reconciler_last_duration_seconds",
    "Duration of the most recent reconciler run in seconds.",
)
RECONCILER_LAST_SCANNED = Gauge(
    "intelligent_vms_reconciler_last_scanned",
    "Cameras scanned in the most recent reconciler run.",
)
RECONCILER_LAST_CHANGED = Gauge(
    "intelligent_vms_reconciler_last_changed",
    "Changes applied in the most recent reconciler run.",
)
RECONCILER_LAST_FAILED = Gauge(
    "intelligent_vms_reconciler_last_failed",
    "Failures in the most recent reconciler run.",
)
OUTBOX_ITEMS = Gauge(
    "intelligent_vms_outbox_items",
    "Durable transactional outbox rows by state.",
    ["status"],
)
METRICS_REFRESH_ERRORS_TOTAL = Counter(
    "intelligent_vms_metrics_refresh_errors_total",
    "Operational metric refresh errors by bounded subsystem.",
    ["subsystem"],
)
OPERATIONAL_REFRESH_OK = Gauge(
    "intelligent_vms_operational_refresh_ok",
    "Whether the latest durable operational-state refresh succeeded.",
)
CAMERA_HEALTH = Gauge(
    "intelligent_vms_camera_health",
    "Managed cameras by bounded durable health state.",
    ["state"],
)
CAMERA_TRANSPORT_READY = Gauge(
    "intelligent_vms_camera_transport_ready",
    "Cameras whose latest RTSP transport probe succeeded.",
)
CAMERA_HEALTH_STALE = Gauge(
    "intelligent_vms_camera_health_stale",
    "Camera health rows older than twice the configured health heartbeat interval.",
)
MEDIA_PATHS_PRESENT = Gauge(
    "intelligent_vms_media_paths_present",
    "Cameras whose latest health sample observed a MediaMTX path.",
)
RECORDING_ACTIVE = Gauge(
    "intelligent_vms_recording_active",
    "Enabled continuous recording policies.",
)
RECORDING_GAP_CANDIDATES = Gauge(
    "intelligent_vms_recording_gap_candidates",
    "Continuous recordings whose initialized segment completion deadline has expired.",
)
RECORDING_HEALTH_UNTRACKED = Gauge(
    "intelligent_vms_recording_health_untracked",
    "Continuous recordings without an initialized durable health deadline.",
)
NODES = Gauge(
    "intelligent_vms_nodes",
    "Enabled infrastructure nodes by bounded operational state.",
    ["state"],
)
NODE_AUTHORITY = Gauge(
    "intelligent_vms_node_authority",
    "Enabled infrastructure nodes by bounded fencing authority mode.",
    ["mode"],
)
NODE_STALE = Gauge(
    "intelligent_vms_node_stale",
    "Enabled infrastructure nodes with stale heartbeat timestamps.",
)
NODE_SATURATED = Gauge(
    "intelligent_vms_node_saturated",
    "Nodes at or above configured placement headroom by role.",
    ["role"],
)
NODE_CAPACITY_UNMEASURED = Gauge(
    "intelligent_vms_node_capacity_unmeasured",
    "Nodes missing trustworthy capacity/load measurements by role.",
    ["role"],
)
PLACEMENT_ASSIGNMENTS = Gauge(
    "intelligent_vms_placement_assignments",
    "Active placement assignments by role and bounded application state.",
    ["role", "status"],
)
AI_POLICIES_ENABLED = Gauge(
    "intelligent_vms_ai_policies_enabled",
    "Camera AI policies enabled in durable control-plane state.",
)
OUTBOX_OLDEST_PENDING_AGE_SECONDS = Gauge(
    "intelligent_vms_outbox_oldest_pending_age_seconds",
    "Age in seconds of the oldest pending/retry transactional outbox item.",
)


def route_label(request: Request) -> str:
    """Return a bounded route-template label for Prometheus metrics.

    Args:
        request: Incoming Starlette request.

    Returns:
        Matched route template, or __unmatched__ when no route is available.
    """
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    return route_path if route_path else "__unmatched__"


class PrometheusMiddleware(BaseHTTPMiddleware):
    """Measure control-API request count, latency and in-flight concurrency."""

    async def dispatch(self, request: Request, call_next: Callable):
        """Record bounded HTTP metrics around one downstream request.

        Args:
            request: Incoming HTTP request.
            call_next: Downstream ASGI request handler.

        Returns:
            Downstream response.

        Raises:
            Exception: Downstream application failures propagate after metrics update.
        """
        if request.url.path == "/metrics":
            return await call_next(request)

        start = time.perf_counter()
        status_code = "500"
        HTTP_IN_FLIGHT_REQUESTS.inc()
        try:
            response = await call_next(request)
            status_code = str(response.status_code)
            return response
        finally:
            duration = time.perf_counter() - start
            HTTP_IN_FLIGHT_REQUESTS.dec()
            path = route_label(request)
            HTTP_REQUESTS_TOTAL.labels(
                method=request.method,
                path=path,
                status=status_code,
            ).inc()
            HTTP_REQUEST_DURATION_SECONDS.labels(
                method=request.method,
                path=path,
            ).observe(duration)


async def refresh_operational_metrics() -> None:
    """Refresh bounded operational Prometheus gauges from durable state.

    Returns:
        None after best-effort refresh. Subsystem failures are exposed through
        refresh-health metrics while the metrics endpoint remains scrapeable.
    """
    RECONCILER_TOTAL_RUNS.set(reconciler_stats.total_runs)
    RECONCILER_LAST_DURATION_SECONDS.set(reconciler_stats.last_duration_seconds)
    RECONCILER_LAST_SCANNED.set(reconciler_stats.last_scanned)
    RECONCILER_LAST_CHANGED.set(reconciler_stats.last_changed)
    RECONCILER_LAST_FAILED.set(reconciler_stats.last_failed)

    outbox_ok = True
    try:
        counts = await outbox_counts()
    except Exception:
        outbox_ok = False
        METRICS_REFRESH_ERRORS_TOTAL.labels(subsystem="outbox").inc()
    else:
        for state in ("pending", "retry", "dead", "delivered"):
            OUTBOX_ITEMS.labels(status=state).set(int(counts.get(state, 0)))

    operational_ok = True
    try:
        snapshot = await collect_operational_snapshot()
    except Exception:
        operational_ok = False
        OPERATIONAL_REFRESH_OK.set(0)
        METRICS_REFRESH_ERRORS_TOTAL.labels(subsystem="operational_db").inc()
    else:
        OPERATIONAL_REFRESH_OK.set(1)

        for state in CAMERA_STATES:
            CAMERA_HEALTH.labels(state=state).set(
                int(snapshot.camera_states.get(state, 0))
            )
        CAMERA_TRANSPORT_READY.set(snapshot.camera_transport_ready)
        CAMERA_HEALTH_STALE.set(snapshot.camera_health_stale)
        MEDIA_PATHS_PRESENT.set(snapshot.media_paths_present)
        RECORDING_ACTIVE.set(snapshot.recording_active)
        RECORDING_GAP_CANDIDATES.set(snapshot.recording_gap_candidates)
        RECORDING_HEALTH_UNTRACKED.set(snapshot.recording_health_untracked)

        for state in NODE_STATES:
            NODES.labels(state=state).set(int(snapshot.node_states.get(state, 0)))
        for mode in AUTHORITY_MODES:
            NODE_AUTHORITY.labels(mode=mode).set(
                int(snapshot.node_authority_modes.get(mode, 0))
            )
        NODE_STALE.set(snapshot.node_stale)

        for role in PLACEMENT_ROLES:
            NODE_SATURATED.labels(role=role).set(
                int(snapshot.node_saturated.get(role, 0))
            )
            NODE_CAPACITY_UNMEASURED.labels(role=role).set(
                int(snapshot.node_capacity_unmeasured.get(role, 0))
            )
            PLACEMENT_ASSIGNMENTS.labels(role=role, status="active").set(
                int(snapshot.placement_active.get(role, 0))
            )
            PLACEMENT_ASSIGNMENTS.labels(role=role, status="unapplied").set(
                int(snapshot.placement_unapplied.get(role, 0))
            )

        AI_POLICIES_ENABLED.set(snapshot.ai_policies_enabled)
        OUTBOX_OLDEST_PENDING_AGE_SECONDS.set(
            snapshot.outbox_oldest_pending_age_seconds
        )

    READY.set(1 if outbox_ok and operational_ok else 0)


@router.get("/metrics")
async def metrics() -> Response:
    """Render the current Prometheus exposition payload.

    Returns:
        Prometheus text-format HTTP response.
    """
    await refresh_operational_metrics()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
