# Phase 3 recording capacity model

## First-order throughput

For C cameras at B Kbps and recording fraction R:

`record_ingress_Gbps = C * B * R / 1,000,000`

`payload_TB_day = C * B * R * 1000 / 8 * 86400 / 1e12`

Provisioned usable capacity adds:
- retention days;
- filesystem/object metadata;
- RAID/erasure/replication;
- reserved free space;
- failure/headroom.

## Segment churn

`segment_closes_per_second = recording_cameras / segment_duration_seconds`

Examples at 100K continuous channels:
- 60 s: ~1,667/s
- 300 s: ~333/s
- 900 s: ~111/s
- 3600 s: ~27.8/s

Default 900 s is a metadata/operability compromise, not a universal optimum.

## CPU

Source-copy recording should primarily consume packet/container/filesystem CPU. Decode/transcode is excluded from the normal recording budget.

Benchmark:
- CPU/Gbps source-copy
- segment-finalization CPU
- TLS/storage encryption overhead if enabled
- kernel softirq under high NIC load

## RAM

Measure:
- base recorder RSS
- per-source buffers
- fMP4 part buffers
- write queues
- filesystem cache

`RAM = base + active_sources*k_source + pending_parts*k_part + safety`

No production coefficient is claimed until measured.

## Disk

Recorder selection is constrained by sustained writes, not only capacity.

Required benchmark:
- sustained sequential write GB/s
- fsync/segment-close latency
- metadata operations/s
- degraded RAID write performance
- rebuild impact
- 70–75% steady-state throughput ceiling
- minimum 10–20% free-capacity reserve depending on filesystem/storage design

## NIC

A recording node at source-copy receives main-stream ingress and may serve playback/backfill egress.

Plan:
`NIC_budget > headroom * (record_ingress + playback_egress + replication/backfill)`

Use 25/100 GbE for high-density nodes based on measured load. Do not target line-rate as steady state.

## Playback

Playback read bandwidth:
`concurrent_playback_sessions * requested_stream_bitrate`

It competes with recording writes on the storage subsystem, so recorder benchmarks must run mixed write/read workloads.

## Storage node acceptance

A node is not assigned a camera count until it passes:
1. source-copy ingest soak;
2. mixed playback test;
3. one-disk/failure/rebuild condition if using RAID;
4. MediaMTX restart recovery;
5. filesystem nearly-full threshold behavior;
6. sustained thermal/CPU/NIC observation.

Channel capacity is then:
`min(network_limit, disk_write_limit, CPU_limit, RAM_limit, operational_limit) * safe_utilization`.
