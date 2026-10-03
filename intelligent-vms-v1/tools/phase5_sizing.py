#!/usr/bin/env python3
"""Structural load model for Phase-5 event subscriptions and alarms.

No hardware coefficients are invented. Measured coefficients can be supplied from
the exact target server to translate structural load into CPU/RAM requirements.
"""
import argparse
import math


def structural_load(
    *,
    cameras: int,
    event_capable_fraction: float,
    subscriptions_per_worker: int,
    events_per_camera_hour: float,
    avg_event_bytes: int,
    avg_candidate_rules: float,
    alarm_open_fraction: float,
    media_nodes: int,
    diagnostic_scrape_seconds: float,
):
    """Calculate structural Phase-5 event/alarm load without hardware assumptions.

    Args:
        cameras: Total camera count.
        event_capable_fraction: Fraction of cameras with ONVIF event support.
        subscriptions_per_worker: Planned PullPoint subscriptions per worker.
        events_per_camera_hour: Average events per event-capable camera-hour.
        avg_event_bytes: Average event payload bytes.
        avg_candidate_rules: Average alarm-rule candidates checked per event.
        alarm_open_fraction: Fraction of events expected to open alarms.
        media_nodes: Media-node count sampled for diagnostics.
        diagnostic_scrape_seconds: Diagnostic scrape interval.

    Returns:
        Structural subscription, event, alarm and diagnostic load dimensions.
    """
    event_cameras = int(math.ceil(cameras * max(0.0, min(1.0, event_capable_fraction))))
    workers = math.ceil(event_cameras / max(1, subscriptions_per_worker))
    events_per_second = event_cameras * events_per_camera_hour / 3600.0
    event_mbps = events_per_second * avg_event_bytes * 8 / 1_000_000
    rule_checks_per_second = events_per_second * max(0.0, avg_candidate_rules)
    alarm_inserts_per_second = events_per_second * max(0.0, min(1.0, alarm_open_fraction))
    diagnostic_scrapes_per_second = media_nodes / max(0.1, diagnostic_scrape_seconds)
    return {
        "event_capable_cameras": event_cameras,
        "persistent_pullpoint_subscriptions": event_cameras,
        "onvif_event_workers": workers,
        "events_per_second": events_per_second,
        "event_payload_mbps": event_mbps,
        "rule_candidate_checks_per_second": rule_checks_per_second,
        "alarm_inserts_per_second": alarm_inserts_per_second,
        "diagnostic_scrapes_per_second": diagnostic_scrapes_per_second,
    }


def measured_budget(
    load: dict,
    *,
    subscription_ram_bytes: float | None,
    worker_base_ram_mb: float | None,
    event_normalizations_per_core: float | None,
    rule_checks_per_core: float | None,
    headroom: float,
    subscriptions_per_worker: int,
):
    """Translate structural Phase-5 load using supplied measured coefficients.

    Args:
        load: Structural load dictionary from structural_load.
        subscription_ram_bytes: Measured RAM per active subscription.
        worker_base_ram_mb: Measured base worker RAM.
        event_normalizations_per_core: Measured event normalization throughput.
        rule_checks_per_core: Measured alarm-rule check throughput.
        headroom: Design headroom multiplier.
        subscriptions_per_worker: Planned subscriptions per worker.

    Returns:
        CPU/RAM budget fields for supplied measurements only.
    """
    out = {}
    if subscription_ram_bytes is not None and worker_base_ram_mb is not None:
        out["onvif_worker_ram_mb"] = (
            worker_base_ram_mb
            + subscriptions_per_worker * subscription_ram_bytes / 1_000_000
        ) * headroom
    if event_normalizations_per_core:
        out["event_worker_cpu_cores_global"] = (
            load["events_per_second"] / event_normalizations_per_core * headroom
        )
    if rule_checks_per_core:
        out["alarm_worker_cpu_cores_global"] = (
            load["rule_candidate_checks_per_second"] / rule_checks_per_core * headroom
        )
    return out


def main():
    """Parse Phase-5 sizing inputs and print structural/measured outputs."""
    p = argparse.ArgumentParser()
    p.add_argument("--cameras", type=int, default=100000)
    p.add_argument("--event-capable-fraction", type=float, default=1.0)
    p.add_argument("--subscriptions-per-worker", type=int, default=200)
    p.add_argument("--events-per-camera-hour", type=float, default=2.0)
    p.add_argument("--avg-event-bytes", type=int, default=1800)
    p.add_argument("--avg-candidate-rules", type=float, default=3.0)
    p.add_argument("--alarm-open-fraction", type=float, default=0.05)
    p.add_argument("--media-nodes", type=int, default=50)
    p.add_argument("--diagnostic-scrape-seconds", type=float, default=15.0)
    p.add_argument("--subscription-ram-bytes", type=float)
    p.add_argument("--worker-base-ram-mb", type=float)
    p.add_argument("--event-normalizations-per-core", type=float)
    p.add_argument("--rule-checks-per-core", type=float)
    p.add_argument("--headroom", type=float, default=1.3)
    a = p.parse_args()
    load = structural_load(
        cameras=a.cameras,
        event_capable_fraction=a.event_capable_fraction,
        subscriptions_per_worker=a.subscriptions_per_worker,
        events_per_camera_hour=a.events_per_camera_hour,
        avg_event_bytes=a.avg_event_bytes,
        avg_candidate_rules=a.avg_candidate_rules,
        alarm_open_fraction=a.alarm_open_fraction,
        media_nodes=a.media_nodes,
        diagnostic_scrape_seconds=a.diagnostic_scrape_seconds,
    )
    budget = measured_budget(
        load,
        subscription_ram_bytes=a.subscription_ram_bytes,
        worker_base_ram_mb=a.worker_base_ram_mb,
        event_normalizations_per_core=a.event_normalizations_per_core,
        rule_checks_per_core=a.rule_checks_per_core,
        headroom=a.headroom,
        subscriptions_per_worker=a.subscriptions_per_worker,
    )
    for key, value in {**load, **budget}.items():
        print(f"{key}: {value:.3f}" if isinstance(value, float) else f"{key}: {value}")
    if not budget:
        print("CPU/RAM budget not calculated: provide measurements from target hardware.")


if __name__ == "__main__":
    main()
