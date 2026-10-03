#!/usr/bin/env python3
import argparse
from dataclasses import dataclass


@dataclass
class RecordingInputs:
    """Input assumptions for first-order recording/storage sizing.

    Attributes:
        cameras: Total camera count.
        bitrate_kbps: Recording bitrate per camera.
        retention_days: Recording retention duration.
        segment_seconds: Recording segment duration.
        recording_fraction: Fraction of cameras recorded.
        replication: Storage protection multiplier.
        headroom: Design headroom multiplier.
        playback_sessions: Concurrent playback sessions.
        playback_kbps: Playback bitrate per session.
    """

    cameras: int
    bitrate_kbps: float
    retention_days: int
    segment_seconds: int = 900
    recording_fraction: float = 1.0
    replication: float = 1.0
    headroom: float = 1.30
    playback_sessions: int = 0
    playback_kbps: float = 1024.0


def calculate(i: RecordingInputs) -> dict[str, float]:
    """Calculate first-order recording, storage and playback dimensions.

    Args:
        i: Recording sizing assumptions.

    Returns:
        Dictionary containing active recordings, ingress, retention storage,
        segment-close rate, playback egress and mixed NIC budget.
    """
    active = i.cameras * i.recording_fraction
    record_gbps = active * i.bitrate_kbps / 1_000_000
    raw_tb_day = active * i.bitrate_kbps * 1000 / 8 * 86400 / 1e12
    raw_pb_retention = raw_tb_day * i.retention_days / 1000
    provisioned_pb = raw_pb_retention * i.replication * i.headroom
    closes_per_second = active / i.segment_seconds
    average_segment_mb = i.bitrate_kbps * 1000 / 8 * i.segment_seconds / 1e6
    playback_gbps = i.playback_sessions * i.playback_kbps / 1_000_000
    mixed_nic_gbps = (record_gbps + playback_gbps) * i.headroom
    return {
        "active_recording_cameras": active,
        "record_ingress_gbps": record_gbps,
        "raw_tb_day": raw_tb_day,
        "raw_pb_retention": raw_pb_retention,
        "provisioned_pb": provisioned_pb,
        "segment_closes_per_second": closes_per_second,
        "average_segment_mb": average_segment_mb,
        "playback_egress_gbps": playback_gbps,
        "mixed_nic_budget_gbps": mixed_nic_gbps,
    }


def main():
    """Parse recorder sizing inputs and print calculated capacity dimensions."""
    p = argparse.ArgumentParser(description="First-order Intelligent VMS recorder sizing")
    p.add_argument("--cameras", type=int, required=True)
    p.add_argument("--bitrate-kbps", type=float, required=True)
    p.add_argument("--retention-days", type=int, default=30)
    p.add_argument("--segment-seconds", type=int, default=900)
    p.add_argument("--recording-fraction", type=float, default=1.0)
    p.add_argument("--replication", type=float, default=1.0)
    p.add_argument("--headroom", type=float, default=1.30)
    p.add_argument("--playback-sessions", type=int, default=0)
    p.add_argument("--playback-kbps", type=float, default=1024.0)
    args = p.parse_args()
    result = calculate(RecordingInputs(**vars(args)))
    for key, value in result.items():
        print(f"{key}: {value:.3f}")


if __name__ == "__main__":
    main()
