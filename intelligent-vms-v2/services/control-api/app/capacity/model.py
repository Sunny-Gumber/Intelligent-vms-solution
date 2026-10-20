"""Capacity planning model.

HONESTY STATEMENT (GOLDEN RULE 3.4): every number this module produces is the
output of an explicit arithmetic model over the inputs you supply. Nothing
here is a measured benchmark of this software on any hardware, and the product
never presents these figures as tested performance.

Model
-----
    bitrate       = resolution x fps x bits-per-pixel(codec) x complexity
    storage/day   = bitrate x duty cycle x 86400
    CPU           = stream-copy recording is I/O bound (one core per N streams)
                    plus one core per N analytics streams
    RAM           = base + per-stream buffers + per-analytics-model
    NIC           = ingest + viewing + export headroom
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vms_contracts.dto import CapacityEstimate

# Baseline bits-per-pixel-per-frame for typical CCTV scenes. Replace with
# measured values from a pilot site before committing to a hardware order.
BPP: dict[str, float] = {"h264": 0.08, "h265": 0.045, "mjpeg": 0.6}
COMPLEXITY: dict[str, float] = {"low": 0.7, "medium": 1.0, "high": 1.6}

STREAMS_PER_RECORDING_CORE = 32
ANALYTICS_STREAMS_PER_CORE = 4
BASE_RAM_GB = 4.0
RAM_PER_STREAM_MB = 24.0
RAM_PER_ANALYTICS_STREAM_MB = 350.0


@dataclass
class CameraClass:
    count: int
    width: int = 1920
    height: int = 1080
    fps: float = 15.0
    codec: str = "h265"
    complexity: str = "medium"
    duty_cycle: float = 1.0      # 1.0 continuous, ~0.3 typical motion-only
    analytics: bool = False

    @property
    def bitrate_mbps(self) -> float:
        bpp = BPP.get(self.codec.lower(), BPP["h264"])
        factor = COMPLEXITY.get(self.complexity, 1.0)
        return round(self.width * self.height * self.fps * bpp * factor / 1_000_000, 3)


@dataclass
class Workload:
    classes: list[CameraClass] = field(default_factory=list)
    retention_days: int = 14
    live_viewers: int = 4
    streams_per_viewer: int = 16
    concurrent_exports: int = 2
    sub_stream_mbps: float = 1.0

    @property
    def camera_count(self) -> int:
        return sum(c.count for c in self.classes)


def estimate(workload: Workload) -> CapacityEstimate:
    ingest_mbps = sum(c.bitrate_mbps * c.count for c in workload.classes)
    recorded_mbps = sum(c.bitrate_mbps * c.count * c.duty_cycle
                        for c in workload.classes)
    analytics_streams = sum(c.count for c in workload.classes if c.analytics)

    bytes_per_day = int(recorded_mbps * 1_000_000 / 8 * 86400)
    bytes_retention = bytes_per_day * workload.retention_days

    recording_cores = max(2, -(-workload.camera_count // STREAMS_PER_RECORDING_CORE))
    analytics_cores = -(-analytics_streams // ANALYTICS_STREAMS_PER_CORE)
    cores = recording_cores + analytics_cores + 2      # +2 for API and database

    ram_gb = (BASE_RAM_GB + workload.camera_count * RAM_PER_STREAM_MB / 1024
              + analytics_streams * RAM_PER_ANALYTICS_STREAM_MB / 1024)

    viewing_mbps = (workload.live_viewers * workload.streams_per_viewer
                    * workload.sub_stream_mbps)
    export_mbps = workload.concurrent_exports * 50
    nic_gbps = round((ingest_mbps + viewing_mbps + export_mbps) * 1.4 / 1000, 2)

    return CapacityEstimate(
        cameras=workload.camera_count,
        total_ingest_mbps=round(ingest_mbps, 2),
        storage_bytes_per_day=bytes_per_day,
        storage_bytes_for_retention=bytes_retention,
        retention_days=workload.retention_days,
        cpu_cores_recommended=cores,
        ram_gb_recommended=int(round(ram_gb + 0.5)),
        nic_gbps_recommended=max(1.0, nic_gbps),
        assumptions={
            "bits_per_pixel_per_frame": BPP,
            "complexity_multipliers": COMPLEXITY,
            "streams_per_recording_core": STREAMS_PER_RECORDING_CORE,
            "analytics_streams_per_core": ANALYTICS_STREAMS_PER_CORE,
            "recorded_mbps": round(recorded_mbps, 2),
            "viewing_mbps": round(viewing_mbps, 2),
            "model": "analytical; not a measured benchmark",
        })


def reference_workloads() -> dict[str, Workload]:
    """The deployment sizes named in the requirements, as planning inputs."""
    def build(count: int, analytics_fraction: float = 0.25) -> Workload:
        analytics_count = int(count * analytics_fraction)
        return Workload(classes=[
            CameraClass(count=count - analytics_count, codec="h265"),
            CameraClass(count=analytics_count, codec="h265", analytics=True),
        ], retention_days=30)

    return {f"{n}-camera": build(n) for n in (16, 32, 64, 128, 256)}
