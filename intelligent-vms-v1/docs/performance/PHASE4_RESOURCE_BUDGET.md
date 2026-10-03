# Phase 4 CPU / RAM / NIC / storage budget

## Rule

No production "channels per server" number is accepted until measured on the target CPU, RAM, NIC, storage and camera codec mix.

The sizing tool separates structural load from hardware coefficients:

```bash
python tools/phase4_sizing.py \
  --cameras 100000 \
  --cameras-per-media-node 2000 \
  --health-poll-seconds 10 \
  --events-per-camera-hour 2
```

Example structural load at those assumptions:
- 50 media-node failure domains;
- 10,000 camera readiness observations/s globally;
- ~333 health heartbeat writes/s globally at 300 s heartbeat;
- ~55.6 events/s if each camera averages 2 events/hour.

These are arithmetic loads, not benchmark claims.

## CPU

Measure separately:
- health observations/s per physical core;
- event API accepts/s per physical core;
- Kafka writer rows/s per physical core;
- ClickHouse filtered-query QPS and p95 latency;
- MediaMTX CPU/Gbps for source-copy forwarding and recording.

Steady production target: keep normal sustained CPU below ~70% on each node, reserving failure/recovery capacity. Validate with your actual SLA and platform.

## RAM

Measure:
- base RSS for each service;
- incremental bytes per active camera/path;
- Kafka producer/consumer buffers;
- ClickHouse working-set/cache;
- MediaMTX per-stream/per-viewer memory.

Use the measured coefficient flags in `phase4_sizing.py`; do not substitute guesses.

## NIC

Health/event traffic is small relative to video. Media nodes are sized by:
- camera ingest;
- live-view egress;
- recording/backfill;
- replication.

Sustain below the chosen safe-utilization ceiling; for dense nodes evaluate 25/100 GbE.

## Disk / ROM / persistent storage

Control-plane PostgreSQL stores state/config only. ClickHouse stores event metadata. Video remains on recording storage.

Measure:
- DB IOPS and WAL;
- ClickHouse bytes/event after compression;
- recording sequential write throughput;
- segment-close metadata rate;
- replay/read traffic;
- RAID/erasure rebuild condition.

## Benchmark acceptance sequence

1. baseline idle RSS/CPU;
2. 500 cameras per media node;
3. 1,000;
4. 2,000;
5. mixed live viewers;
6. continuous recording;
7. event burst;
8. media-node restart;
9. disk degraded/rebuild;
10. 24-hour soak.

Increase density only if CPU/RAM/NIC/disk p95/p99 and thermal behavior stay inside budget.
