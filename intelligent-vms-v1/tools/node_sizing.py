#!/usr/bin/env python3
import argparse
from dataclasses import dataclass


@dataclass
class Inputs:
    """Input assumptions for first-order VMS node/storage sizing.

    Attributes:
        cameras: Camera count.
        main_kbps: Recording bitrate per camera.
        recording_fraction: Fraction of cameras recorded.
        retention_days: Retention duration.
        viewers: Concurrent live viewers.
        live_kbps: Live-view bitrate per viewer.
        replication: Storage replication/parity multiplier.
        headroom: Design headroom multiplier.
    """

    cameras: int
    main_kbps: float
    recording_fraction: float
    retention_days: int
    viewers: int
    live_kbps: float
    replication: float
    headroom: float


def size(i: Inputs) -> dict:
    """Calculate first-order recording, storage and live-egress requirements.

    Args:
        i: Validated sizing assumptions.

    Returns:
        Dictionary of record ingress, raw/provisioned storage and live egress.
    """
    record_gbps = i.cameras * i.main_kbps * i.recording_fraction / 1_000_000
    bytes_day = i.cameras * i.main_kbps * i.recording_fraction * 1000 / 8 * 86400
    raw_tb_day = bytes_day / 1e12
    raw_pb = raw_tb_day * i.retention_days / 1000
    provisioned_pb = raw_pb * i.replication * i.headroom
    live_egress_gbps = i.viewers * i.live_kbps / 1_000_000
    return {
        "record_ingress_gbps": record_gbps,
        "raw_tb_day": raw_tb_day,
        "raw_pb_retention": raw_pb,
        "provisioned_pb": provisioned_pb,
        "live_egress_gbps": live_egress_gbps,
    }


def main():
    """Parse CLI sizing inputs and print calculated capacity dimensions."""
    p = argparse.ArgumentParser(description="VMS first-order capacity calculator")
    p.add_argument("--cameras", type=int, required=True)
    p.add_argument("--main-kbps", type=float, required=True)
    p.add_argument("--recording-fraction", type=float, default=1.0)
    p.add_argument("--retention-days", type=int, default=30)
    p.add_argument("--viewers", type=int, default=0)
    p.add_argument("--live-kbps", type=float, default=512)
    p.add_argument("--replication", type=float, default=1.0)
    p.add_argument("--headroom", type=float, default=1.30)
    a = p.parse_args()
    result = size(Inputs(**vars(a)))
    for k, v in result.items():
        print(f"{k}: {v:.3f}")


if __name__ == "__main__":
    main()
