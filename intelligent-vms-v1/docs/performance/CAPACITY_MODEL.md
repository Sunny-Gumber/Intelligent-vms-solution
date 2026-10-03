# Intelligent VMS capacity model

Status: Performance Agent V1. Values below are engineering starting points, not vendor-certified channel counts. Benchmark measurements replace assumptions as the software matures.

## Principle

There is no honest single "cameras per server" number. Nodes are limited by different resources:

- control plane: requests, DB connections, state size
- media relay: ingress/egress bitrate, socket count, viewer sessions, packet work
- recording: sustained write throughput, capacity, metadata/segment rate, filesystem/object latency
- event plane: events/s and query rate
- AI: decoded pixels/s, model cost, accelerator memory/throughput
- playback: concurrent read bitrate and transcode/decode requirements
- network: aggregate full-duplex traffic plus failure headroom

## Core formulas

Let:
- C = cameras
- Bm = average main-stream bitrate Kbps
- Bs = average sub-stream bitrate Kbps
- R = recording fraction 0..1
- D = retention days
- V = average concurrent live viewers
- Lv = average live-view stream bitrate Kbps
- H = headroom multiplier (recommended starting point 1.30)
- Rep = storage replication/parity effective multiplier

Recording ingress Gbps:
`C * Bm * R / 1,000,000`

Raw storage TB/day:
`C * Bm * R * 1000/8 * 86400 / 1e12`

Provisioned storage:
`TB/day * D * Rep * H`

Media egress Gbps:
`V * Lv / 1,000,000`

Node NIC budget should satisfy:
`H * (ingress + egress + replication/backfill traffic)`

## Failure headroom

Normal planning target: keep steady-state critical resources below ~70-75% so one node/rack failure can be absorbed. N+1 is mandatory inside each availability/failure domain for services that must continue after one node failure.

Do not use the same headroom assumption for storage capacity and transient network failover without testing.

## Provisional node classes

These are starting classes for lab/benchmark selection, not final production bills of material.

### Control/API node
Purpose: stateless API, auth, orchestration.
Starting class:
- 8-16 modern x86_64/ARM64 cores
- 32-64 GB RAM
- mirrored enterprise SSD boot
- 10/25 GbE depending on API/event topology
Scale trigger: CPU >65%, p95 latency, DB pool pressure, queue depth.

### Regional media relay node
Purpose: source-copy RTSP ingest, WebRTC/HLS/RTSP relay; avoid video transcoding.
Starting class:
- 16-32 high-clock cores
- 32-64 GB RAM
- 25 GbE minimum for meaningful density; 100 GbE for high-density regions
- small mirrored SSD for OS/logs; video storage separate
Scale trigger: NIC >65-70%, packet loss/jitter, socket/session ceiling, CPU softirq saturation.
GPU: not required for source-copy relay.

### Recording node
Purpose: write source streams and segment/index them.
Starting class:
- 16-32 cores
- 64-128 GB RAM
- 25/100 GbE
- HBA/JBOD or high-throughput object/NAS path
- mirrored enterprise NVMe for OS and local queues/index/WAL where applicable
- capacity disks/object storage sized by formula
Scale trigger: sustained disk latency, write queue, segment close latency, NIC, hot-tier capacity.
GPU: not required for source-copy recording.

### Event/search nodes
Kafka-compatible brokers and ClickHouse are sized independently. Start with 3-node production clusters, fast NVMe, 64-256 GB RAM depending on retention/query volume, and 10/25+ GbE. Final size is driven by benchmark event rate and indexed data volume.

### AI nodes
Never mix AI channel capacity into normal recording channel capacity.
Size using:
- input resolution/FPS after sampling
- decoded pixels/s
- model
- precision (FP32/FP16/INT8)
- batch policy
- accelerator
- target latency

GPU/accelerator choice is deferred until model benchmarks. CPU/iGPU inference can be supported for small sites; centralized high-density AI requires measured GPU throughput.

## RAM model

Media relay RAM should remain bounded per session. Track:
- RSS/base process
- memory per active source
- memory per viewer
- HLS segment/cache memory
- queues/backpressure

Benchmark coefficients:
`RAM = base + sources*k_source + viewers*k_viewer + hls_paths*k_hls + margin`

Do not preallocate RAM per 100K logical camera if streams are sharded.

## CPU model

Separate:
1. packet relay/source copy
2. TLS/encryption
3. WebRTC packetization
4. HLS segmentation
5. video decode
6. AI inference
7. transcoding

Decode/transcode/AI dominate CPU/GPU and must not silently enter the source-copy media path.

Benchmark coefficient model:
`CPU_cores_required = workload_units / measured_units_per_core * H`

## Storage optimization

Primary optimizations:
- source-copy; no unnecessary transcode
- configurable continuous/event recording
- fMP4/segment sizes tuned to recovery and object-store request economics
- hot local tier + warm object/dense disk + archive
- retention policy per tenant/camera
- sub-stream only for UI/AI where sufficient
- avoid storing duplicate relayed streams
- async backfill after WAN recovery

"ROM" for this project means persistent storage. Production sizing must include filesystem/object metadata, parity/replication, reserved free space, index/snapshot growth and export workspace.

## Example: 100,000 cameras at 1 Mbps

Payload recording ingress: 100 Gbps.
Raw payload: ~1.08 PB/day.
180 days raw: ~194.4 PB before replication/headroom.

This cannot be treated as one recorder. A realistic topology distributes recording across sites/regions and storage failure domains.

## Benchmark gates before production sizing

We will measure:
- source connections/node
- ingress Gbps/node
- concurrent WebRTC viewers/node
- HLS viewers/node
- CPU per Gbps for relay
- RAM per source/viewer
- segment writes/s
- sustained recorder GB/s
- event ingest/s
- ClickHouse query p95/p99
- failover recovery time
- reconnect storm behavior
- AI FPS/model/accelerator

Each release updates this file with measured coefficients and hardware used.

## Optimization policy

Boss will reject any feature that:
- forces central video traversal when local/regional processing works;
- adds mandatory decode/transcode to recording;
- causes O(camera_count) polling from one central process;
- stores secrets in logs/config repositories;
- lacks resource metrics needed for capacity planning.
