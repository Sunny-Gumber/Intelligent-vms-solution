#!/usr/bin/env python3
import argparse


def capacity(cameras: int, kbps: float, days: int):
    """Calculate first-order aggregate bitrate and raw retention storage.

    Args:
        cameras: Camera count.
        kbps: Payload bitrate per camera in Kbps.
        days: Retention duration in days.

    Returns:
        Tuple of aggregate Gbps, raw TB per day and raw PB across retention.
    """
    total_gbps = cameras * kbps / 1_000_000
    bytes_per_day = cameras * kbps * 1000 / 8 * 86400
    tb_day = bytes_per_day / 1e12
    pb_total = tb_day * days / 1000
    return total_gbps, tb_day, pb_total


def main():
    """Parse CLI arguments and print first-order capacity results."""
    p=argparse.ArgumentParser()
    p.add_argument('--cameras', type=int, default=100000)
    p.add_argument('--kbps', type=float, default=1024)
    p.add_argument('--days', type=int, default=180)
    a=p.parse_args()
    gbps,tbday,pb=capacity(a.cameras,a.kbps,a.days)
    print(f'Aggregate payload bitrate: {gbps:.2f} Gbps')
    print(f'Raw payload per day:       {tbday:.2f} TB/day')
    print(f'Raw payload for {a.days}d:   {pb:.2f} PB')
    print('Excludes protocol/filesystem overhead, replication, parity and snapshots.')

if __name__=='__main__':
    main()
