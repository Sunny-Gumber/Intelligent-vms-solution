#!/usr/bin/env python3
import argparse
import math


def structural(cameras:int, media_nodes:int, recording_nodes:int, heartbeat_seconds:float, placement_batch:int, interval_seconds:float):
    """Calculate structural placement and heartbeat control-plane load.

    Args:
        cameras: Total camera count.
        media_nodes: Media-node count.
        recording_nodes: Recording-node count.
        heartbeat_seconds: Node heartbeat interval.
        placement_batch: Cameras scanned per placement iteration.
        interval_seconds: Placement-controller iteration interval.

    Returns:
        Structural heartbeat rate, placement scan rate and full-scan duration.
    """
    return {
        "cameras": cameras,
        "media_nodes": media_nodes,
        "recording_nodes": recording_nodes,
        "node_heartbeats_per_second": (media_nodes + recording_nodes) / max(1.0, heartbeat_seconds),
        "placement_scans_per_second": placement_batch / max(1.0, interval_seconds),
        "full_camera_scan_seconds": math.ceil(cameras / max(1, placement_batch)) * max(1.0, interval_seconds),
    }


def main():
    """Parse Phase-7 sizing inputs and print structural control-plane load."""
    p=argparse.ArgumentParser()
    p.add_argument("--cameras",type=int,default=100000)
    p.add_argument("--media-nodes",type=int,default=100)
    p.add_argument("--recording-nodes",type=int,default=100)
    p.add_argument("--heartbeat-seconds",type=float,default=10)
    p.add_argument("--placement-batch",type=int,default=1000)
    p.add_argument("--interval-seconds",type=float,default=5)
    a=p.parse_args()
    for k,v in structural(a.cameras,a.media_nodes,a.recording_nodes,a.heartbeat_seconds,a.placement_batch,a.interval_seconds).items():
        print(f"{k}: {v:.3f}" if isinstance(v,float) else f"{k}: {v}")

if __name__=="__main__":
    main()
