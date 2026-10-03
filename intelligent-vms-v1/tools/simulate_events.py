#!/usr/bin/env python3
"""Synthetic metadata-plane load generator.

This does NOT simulate video bitrate. It validates camera-keyed event ingestion only.
Example:
  python tools/simulate_events.py --api http://localhost:8000 --cameras 100000 --events 100000 --concurrency 100
"""
import argparse
import asyncio
import random
import time
import uuid
from datetime import datetime, timezone
import httpx

EVENT_TYPES = ["motion", "human", "vehicle", "intrusion", "camera_offline", "tamper"]


def event(i: int, cameras: int) -> dict:
    """Create one synthetic normalized event for metadata-plane load testing.

    Args:
        i: Event sequence index.
        cameras: Synthetic camera population size.

    Returns:
        Synthetic VMS event dictionary.
    """
    camera_num = i % cameras
    return {
        "event_id": str(uuid.uuid4()),
        "tenant_id": f"tenant-{camera_num % 10:02d}",
        "site_id": f"site-{camera_num % 500:04d}",
        "camera_id": f"cam-{camera_num:07d}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": random.choice(EVENT_TYPES),
        "object_type": random.choice([None, "person", "vehicle"]),
        "source": random.choice(["camera", "ai", "vms"]),
        "confidence": round(random.uniform(0.65, 0.99), 3),
        "severity": random.choice(["info", "low", "medium", "high"]),
        "attributes": {"synthetic": True},
    }


async def worker(queue: asyncio.Queue, client: httpx.AsyncClient, url: str, stats: dict):
    """Consume synthetic events from a queue and submit them to the VMS API.

    Args:
        queue: Work queue containing event dictionaries and a None sentinel.
        client: Shared HTTP client.
        url: Event-ingest API URL.
        stats: Mutable accepted/failed counters.

    Returns:
        None after the worker receives its sentinel.
    """
    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            return
        try:
            r = await client.post(url, json=item)
            if r.status_code == 202:
                stats["ok"] += 1
            else:
                stats["failed"] += 1
        except Exception:
            stats["failed"] += 1
        finally:
            queue.task_done()


async def run(args):
    """Run the configured synthetic event-ingest workload.

    Args:
        args: Parsed CLI arguments with API, camera, event and concurrency settings.

    Returns:
        None after printing throughput summary.
    """
    queue = asyncio.Queue(maxsize=args.concurrency * 10)
    stats = {"ok": 0, "failed": 0}
    url = args.api.rstrip("/") + "/api/v1/events"
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(timeout=10, limits=limits) as client:
        workers = [asyncio.create_task(worker(queue, client, url, stats)) for _ in range(args.concurrency)]
        start = time.perf_counter()
        for i in range(args.events):
            await queue.put(event(i, args.cameras))
        for _ in workers:
            await queue.put(None)
        await queue.join()
        await asyncio.gather(*workers)
        elapsed = time.perf_counter() - start
    rate = stats["ok"] / elapsed if elapsed else 0
    print(f"accepted={stats['ok']} failed={stats['failed']} elapsed={elapsed:.2f}s rate={rate:.1f} events/s")


def main():
    """Parse synthetic event-load arguments and execute the async workload."""
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--cameras", type=int, default=100000)
    p.add_argument("--events", type=int, default=10000)
    p.add_argument("--concurrency", type=int, default=50)
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
