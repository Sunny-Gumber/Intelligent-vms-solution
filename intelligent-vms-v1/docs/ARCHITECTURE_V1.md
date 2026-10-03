# Intelligent VMS — System Architecture V1

## 1. Product objective

Build a vendor-neutral intelligent VMS that can run on-premises, hybrid or cloud and scale from tens of cameras to more than 100,000 channels. The architecture must not have a hard central-media bottleneck.

### Design ceiling

The data model and IDs should tolerate 1,000,000 logical cameras even though the first production objective is 100,000+.

## 2. Non-negotiable principles

1. **Separate control plane from media plane.** Camera configuration and user state can be centralized; continuous video cannot.
2. **Scale by aggregate bitrate, not channel count alone.** A 100K system at 512 Kbps behaves very differently from 100K at 4 Mbps.
3. **Regionalize failure domains.** One node, rack, site or region failure must not collapse the VMS.
4. **Use metadata for intelligence.** Search AI/event metadata first, then resolve the associated recording time range.
5. **No mandatory transcode for normal recording.** Store source codecs whenever possible.
6. **Treat AI as a schedulable plane.** Do not run every AI model on every camera continuously.
7. **Use open camera interfaces.** ONVIF Profile T/G/M where applicable; design for Profile V without depending on it while it remains a release candidate.
8. **Secrets never become public API fields.** Camera credentials belong in a vault/KMS in production.
9. **Every mutable service is horizontally partitionable.** Avoid singleton stateful components in hot paths.
10. **Observability is part of the product.** Health, latency, recording gaps and stream quality are first-class events.

## 3. Logical architecture

```text
                                  +----------------------+
                                  |   Global Control     |
                                  |  API / Identity / UI |
                                  +----------+-----------+
                                             |
                     +-----------------------+-----------------------+
                     |                       |                       |
               +-----v------+          +-----v------+          +-----v------+
               | Region A   |          | Region B   |          | Region N   |
               | Controller |          | Controller |          | Controller |
               +-----+------+          +-----+------+          +-----+------+
                     |                       |                       |
          +----------+----------+  +---------+----------+  +---------+----------+
          |                     |  |                    |  |                    |
     +----v----+           +----v----+             +----v----+            +----v----+
     | Media   | ...       | Record  |             | Media   | ...        | AI      |
     | Nodes   |           | Nodes   |             | Nodes   |            | Workers |
     +----+----+           +----+----+             +----+----+            +----+----+
          |                     |                       |                      |
       Cameras             Hot/Warm storage          Cameras                GPU pool
          |                     |                       |                      |
          +---------------------+-----------+-----------+----------------------+
                                            |
                                      Event Backbone
                                            |
                        +-------------------+-------------------+
                        |                                       |
                  +-----v------+                         +------v------+
                  | ClickHouse |                         | Notification|
                  | Event/Search|                        | / Workflow  |
                  +------------+                         +-------------+
```

## 4. Service decomposition

### Global control plane

- Tenant management
- Sites and regions
- Camera registry
- User/RBAC
- Audit log
- License/entitlement
- Configuration orchestration
- Placement decisions
- API gateway

PostgreSQL is appropriate for strongly consistent control state. Camera/event media does not live here.

### Regional controller

- Owns a bounded set of sites/cameras
- Places cameras onto media nodes
- Reconciles desired vs actual media configuration
- Buffers control changes when central services are unavailable
- Reports summarized health upstream

### Media node

- Pulls RTSP/RTSPS camera streams
- Exposes WebRTC/HLS/RTSP to clients or internal services
- Optionally records selected streams
- Emits connection/packet/jitter/stream metrics
- Never owns global truth about cameras

Media nodes should be deliberately sharded. Avoid extremely high-concurrency runtime configuration writes to one node; the regional controller serializes/reconciles path mutations per node and can replace a degraded node instead of treating it as irreplaceable infrastructure.

### Recording node

- Receives or pulls source stream without transcoding where possible
- Writes short media parts and larger logical segments
- Maintains recording manifest/index
- Replicates or migrates segments through hot/warm/archive tiers
- Detects recording gaps

### Event ingest

All analytics, health, camera and integration events enter a common envelope and are keyed by tenant/site/camera. Kafka partitions preserve per-key ordering while allowing consumer groups to scale processing horizontally.

### Event/search store

ClickHouse stores high-volume, append-oriented event metadata for fast filtering/aggregation. Large binary snapshots/clips belong in object storage; the database stores references.

### AI scheduler

- Receives policies such as `human_detection`, `anpr`, `face_detection`
- Chooses camera edge analytics, regional GPU inference or cloud inference
- Uses low-resolution/sub-stream where adequate
- Tracks GPU capacity and model version
- Produces normalized events

## 5. Camera capability model

Each camera has a capability document independent of manufacturer:

```json
{
  "video": {"h264": true, "h265": true, "main": true, "sub": true},
  "audio": {"input": true, "output": false},
  "ptz": true,
  "edge_storage": true,
  "events": ["motion", "tamper"],
  "analytics": ["tripwire", "intrusion"],
  "metadata": ["objects", "plate"],
  "onvif_profiles": ["T", "G", "M"]
}
```

The application uses capability flags rather than hard-coded brand checks wherever possible.

## 6. Common event envelope

```json
{
  "event_id": "uuid",
  "tenant_id": "tenant-01",
  "site_id": "site-01",
  "camera_id": "uuid",
  "timestamp": "2026-09-23T12:00:00Z",
  "event_type": "intrusion",
  "object_type": "person",
  "source": "camera|vms|ai|integration",
  "confidence": 0.94,
  "zone_id": "gate-a",
  "severity": "medium",
  "recording": {"start": "...", "end": "..."},
  "snapshot_uri": "s3://...",
  "attributes": {}
}
```

Partition key: normally `tenant_id + camera_id`, allowing ordered processing for one camera without forcing global ordering.

## 7. Live-view path

```text
Camera RTSP main -----> Recording
Camera RTSP sub  -----> Media node -----> WebRTC -----> Browser
                              |
                              +-----------> AI worker (policy dependent)
```

WebRTC is the preferred low-latency browser path. HLS is fallback / large-scale distribution where a little more latency is acceptable.

## 8. Recording architecture

Recommended default:

- Source-copy recording; transcode only where explicitly needed.
- fMP4/CMAF-friendly segmentation.
- Small parts for recovery and upload resilience.
- Logical segments indexed by camera and time.
- Recording manifest separate from filesystem path assumptions.

Storage tiers:

```text
Hot:     local NVMe / high-throughput NAS, e.g. 1–14 days
Warm:    object or dense disk, e.g. 14–90 days
Archive: lower-cost object tier, e.g. 90–180+ days
```

Retention is a policy, not a hardcoded deletion timer.

## 9. 100K capacity sanity checks

At 100,000 cameras:

- 512 Kbps average source bitrate ≈ 51.2 Gbps aggregate payload bitrate.
- 1 Mbps average ≈ 100 Gbps.
- 2 Mbps average ≈ 200 Gbps.

Approximate raw payload storage before overhead/replication:

- 512 Kbps: ~553 TB/day.
- 1 Mbps: ~1.08 PB/day.
- 2 Mbps: ~2.16 PB/day.

This is why central cloud ingest of every stream is not always economical. Hybrid/site recording with centralized metadata can be a better topology for many deployments.

## 10. Multi-node placement

The placement engine scores candidate media nodes by:

- same region/site affinity
- current ingress bitrate
- camera count
- CPU/RAM
- NIC utilization
- WebRTC sessions
- storage responsibility
- node health

A camera gets an assigned `media_node_id`. Reassignment is a controlled state transition, not an implicit side effect.

## 11. Failure behavior

### Central control unavailable
Regional systems continue streaming/recording from cached desired state. Changes queue until control returns.

### Media node fails
Regional controller reassigns affected cameras. Clients resolve a new endpoint through the control/API layer.

### WAN/MPLS fails
Local recording continues. Metadata/events queue locally. Optional camera-SD backfill is used where Profile G/vendor capability allows it.

### Kafka unavailable
Regional event spool persists critical events and replays when the backbone returns.

### Object storage unavailable
Recording remains in local hot storage and migration retries without blocking live ingest.

## 12. Security model

- TLS externally; mTLS for service-to-service where practical.
- OAuth/OIDC for users/apps.
- RBAC + tenant/site scope.
- KMS/vault for camera and storage secrets.
- Signed short-lived media access tokens.
- Immutable audit trail.
- Encryption at rest.
- Network segmentation: cameras do not need public inbound exposure.
- Future Profile V compatibility for secure cloud uplink.

## 13. Observability

Per camera:

- reachable / authenticated
- source connected
- FPS
- bitrate
- codec/resolution
- RTP packet loss/jitter where available
- recording continuity
- clock drift
- AI worker/model health

Per node:

- ingress/egress Gbps
- active camera paths
- viewer sessions
- CPU/RAM
- NIC saturation
- queue lag
- storage write latency

All abnormal states become normalized health events.

## 14. Scale-testing strategy

Do not jump from 20 real cameras to 100K real streams.

1. Functional: 1–20 real streams.
2. Media-node soak: hundreds/thousands of synthetic RTSP streams per suitable host.
3. Control plane: 100K–1M logical camera records.
4. Event plane: millions of synthetic events at controlled rates.
5. Failure tests: kill media nodes, brokers, databases, WAN links.
6. Region tests: multiple independent clusters with central orchestration.

## 15. Delivery phases

### Phase 1 — foundation (this repo)
Camera registry, media provisioning, basic live-view URLs, event backbone, ClickHouse sink, developer dashboard.

### Phase 2 — real VMS core
ONVIF discovery/capability probe, camera status, recording, playback timeline, retention, export.

### Phase 3 — event intelligence
Motion/tamper/offline events, alarm rules, notifications, health diagnostics.

### Phase 4 — AI
Human/vehicle, tripwire/intrusion, ANPR metadata, FD/FR pipeline, AI scheduling.

### Phase 5 — distributed scale
Regional controllers, placement service, media-node pools, replicated storage/indexes, multi-region failover.

### Phase 6 — enterprise/cloud
Multi-tenancy hardening, SSO, audit/evidence, Profile V integration when finalized/conformant ecosystem is available, API/SDK ecosystem.
