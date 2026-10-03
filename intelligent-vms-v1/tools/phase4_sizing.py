#!/usr/bin/env python3
"""First-order Phase-4 control/event/health load model.

This deliberately does not invent CPU/RAM-per-camera coefficients. Supply measured
coefficients from your target hardware to turn structural load into node sizing.
"""
import argparse
import math


def structural_load(
    *,
    cameras: int,
    cameras_per_media_node: int,
    health_poll_seconds: float,
    health_heartbeat_seconds: float,
    events_per_camera_hour: float,
    avg_event_bytes: int,
):
    """Calculate structural Phase-4 health/event load without hardware assumptions.

    Args:
        cameras: Total camera count.
        cameras_per_media_node: Structural camera density per media node.
        health_poll_seconds: Health observation interval.
        health_heartbeat_seconds: Persisted health heartbeat interval.
        events_per_camera_hour: Average normalized events per camera-hour.
        avg_event_bytes: Average event payload bytes.

    Returns:
        Structural node, health and event load dimensions.
    """
    media_nodes = math.ceil(cameras / max(1, cameras_per_media_node))
    observations_per_second = cameras / max(0.1, health_poll_seconds)
    heartbeat_writes_per_second = cameras / max(1.0, health_heartbeat_seconds)
    events_per_second = cameras * events_per_camera_hour / 3600.0
    event_mbps = events_per_second * avg_event_bytes * 8 / 1_000_000
    raw_event_gb_day = events_per_second * avg_event_bytes * 86400 / 1e9
    return {
        "media_nodes": media_nodes,
        "health_observations_per_second": observations_per_second,
        "steady_health_heartbeat_writes_per_second": heartbeat_writes_per_second,
        "events_per_second": events_per_second,
        "event_payload_mbps": event_mbps,
        "raw_event_gb_day": raw_event_gb_day,
    }


def measured_budget(
    load: dict,
    *,
    observations_per_core: float | None,
    events_per_core: float | None,
    health_bytes_per_camera: float | None,
    base_health_ram_mb: float | None,
    headroom: float,
    cameras_per_media_node: int,
):
    """Translate structural Phase-4 load using supplied measured coefficients.

    Args:
        load: Structural load dictionary from structural_load.
        observations_per_core: Measured health observations per CPU core.
        events_per_core: Measured event operations per CPU core.
        health_bytes_per_camera: Measured health-state bytes per camera.
        base_health_ram_mb: Measured base health-worker RAM.
        headroom: Design headroom multiplier.
        cameras_per_media_node: Planned cameras per media node.

    Returns:
        Calculated CPU/RAM budget fields for supplied measurements only.
    """
    out = {}
    if observations_per_core:
        out["health_cpu_cores_global"] = (
            load["health_observations_per_second"] / observations_per_core * headroom
        )
        out["health_cpu_cores_per_media_node"] = out["health_cpu_cores_global"] / load["media_nodes"]
    if events_per_core:
        out["event_ingest_cpu_cores_global"] = load["events_per_second"] / events_per_core * headroom
    if health_bytes_per_camera is not None and base_health_ram_mb is not None:
        out["health_ram_mb_per_media_node"] = (
            base_health_ram_mb
            + cameras_per_media_node * health_bytes_per_camera / 1_000_000
        ) * headroom
    return out


def main():
    """Parse Phase-4 sizing inputs and print structural/measured outputs."""
    p = argparse.ArgumentParser()
    p.add_argument("--cameras", type=int, default=100000)
    p.add_argument("--cameras-per-media-node", type=int, default=2000)
    p.add_argument("--health-poll-seconds", type=float, default=10)
    p.add_argument("--health-heartbeat-seconds", type=float, default=300)
    p.add_argument("--events-per-camera-hour", type=float, default=2)
    p.add_argument("--avg-event-bytes", type=int, default=1500)
    p.add_argument("--observations-per-core", type=float)
    p.add_argument("--events-per-core", type=float)
    p.add_argument("--health-bytes-per-camera", type=float)
    p.add_argument("--base-health-ram-mb", type=float)
    p.add_argument("--headroom", type=float, default=1.3)
    a = p.parse_args()
    load = structural_load(
        cameras=a.cameras,
        cameras_per_media_node=a.cameras_per_media_node,
        health_poll_seconds=a.health_poll_seconds,
        health_heartbeat_seconds=a.health_heartbeat_seconds,
        events_per_camera_hour=a.events_per_camera_hour,
        avg_event_bytes=a.avg_event_bytes,
    )
    budget = measured_budget(
        load,
        observations_per_core=a.observations_per_core,
        events_per_core=a.events_per_core,
        health_bytes_per_camera=a.health_bytes_per_camera,
        base_health_ram_mb=a.base_health_ram_mb,
        headroom=a.headroom,
        cameras_per_media_node=a.cameras_per_media_node,
    )
    for key, value in {**load, **budget}.items():
        print(f"{key}: {value:.3f}" if isinstance(value, float) else f"{key}: {value}")
    if not budget:
        print("CPU/RAM budget: not calculated; pass measured coefficients from target hardware.")


if __name__ == "__main__":
    main()
