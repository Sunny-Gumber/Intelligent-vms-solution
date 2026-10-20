"""System metrics and Prometheus exposition.

All values are measured with psutil at request time. Nothing is simulated;
when a metric cannot be read on the current platform it is omitted rather
than reported as zero.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass

import psutil
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest
from sqlalchemy import func, select

from app.core.timeutil import utcnow

_PROCESS = psutil.Process()
_LAST: dict[str, tuple[float, int, int]] = {}


@dataclass
class SystemMetrics:
    cpu_percent: float
    memory_percent: float
    memory_used_mb: float
    memory_total_mb: float
    process_rss_mb: float
    load_average_1m: float | None
    disk_read_mb_s: float | None
    disk_write_mb_s: float | None
    net_in_mbps: float | None
    net_out_mbps: float | None
    open_files: int | None
    threads: int
    uptime_s: float

    def to_dict(self) -> dict:
        return asdict(self)


def collect(interval: float = 0.0) -> SystemMetrics:
    virtual = psutil.virtual_memory()
    try:
        load = psutil.getloadavg()[0]
    except (AttributeError, OSError):
        load = None

    net_in, net_out = _rate("net", lambda c: (c.bytes_recv, c.bytes_sent),
                            psutil.net_io_counters, scale=8 / 1e6)
    disk_read, disk_write = _rate("disk", lambda c: (c.read_bytes, c.write_bytes),
                                  psutil.disk_io_counters, scale=1 / 1e6)

    try:
        open_files = len(_PROCESS.open_files())
    except (psutil.AccessDenied, OSError):
        open_files = None

    return SystemMetrics(
        cpu_percent=psutil.cpu_percent(interval=interval),
        memory_percent=virtual.percent,
        memory_used_mb=round(virtual.used / 1e6, 1),
        memory_total_mb=round(virtual.total / 1e6, 1),
        process_rss_mb=round(_PROCESS.memory_info().rss / 1e6, 1),
        load_average_1m=round(load, 2) if load is not None else None,
        disk_read_mb_s=disk_read, disk_write_mb_s=disk_write,
        net_in_mbps=net_in, net_out_mbps=net_out,
        open_files=open_files, threads=_PROCESS.num_threads(),
        uptime_s=round(time.time() - _PROCESS.create_time(), 1))


def _rate(key: str, extract, source, scale: float):  # noqa: ANN001
    try:
        counters = source()
    except (OSError, AttributeError):
        return None, None
    if counters is None:
        return None, None
    now = time.monotonic()
    a, b = extract(counters)
    previous = _LAST.get(key)
    _LAST[key] = (now, a, b)
    if previous is None:
        return None, None
    elapsed = now - previous[0]
    if elapsed <= 0:
        return None, None
    return (round((a - previous[1]) * scale / elapsed, 3),
            round((b - previous[2]) * scale / elapsed, 3))


def prometheus_exposition(extra: dict[str, float] | None = None) -> tuple[bytes, str]:
    registry = CollectorRegistry()
    metrics = collect()
    gauges = {
        "vms_cpu_percent": metrics.cpu_percent,
        "vms_memory_percent": metrics.memory_percent,
        "vms_process_rss_megabytes": metrics.process_rss_mb,
        "vms_process_threads": float(metrics.threads),
        "vms_uptime_seconds": metrics.uptime_s,
    }
    if metrics.net_in_mbps is not None:
        gauges["vms_network_in_mbps"] = metrics.net_in_mbps
        gauges["vms_network_out_mbps"] = metrics.net_out_mbps or 0.0
    gauges.update(extra or {})

    for name, value in gauges.items():
        Gauge(name, name.replace("_", " "), registry=registry).set(value)
    return generate_latest(registry), CONTENT_TYPE_LATEST


def bandwidth_summary(session) -> dict[str, float]:  # noqa: ANN001
    """Aggregate camera bandwidth ESTIMATED from configured/probed bitrates.

    This is explicitly an estimate, not a wire measurement. Measured NIC
    throughput is reported separately by `collect()`.
    """
    from app.db.models import Camera, CameraStream, LiveSession

    recording_kbps = 0
    rows = session.execute(
        select(CameraStream).join(Camera, Camera.id == CameraStream.camera_id)
        .where(Camera.enabled.is_(True), CameraStream.record.is_(True))).scalars().all()
    for stream in rows:
        recording_kbps += stream.bitrate_kbps or 2048

    viewing_kbps = int(session.execute(
        select(func.coalesce(func.sum(LiveSession.estimated_kbps), 0))
        .where(LiveSession.closed_at.is_(None),
               LiveSession.expires_at > utcnow())).scalar_one() or 0)

    return {
        "estimated_ingest_mbps": round(recording_kbps / 1000, 2),
        "estimated_viewing_mbps": round(viewing_kbps / 1000, 2),
        "estimated_total_mbps": round((recording_kbps + viewing_kbps) / 1000, 2),
    }
