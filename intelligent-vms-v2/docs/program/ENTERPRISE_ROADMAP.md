# IVMS V2 enterprise architecture and delivery plan

Date: 2026-10-31. Status: planned requirements, not implemented capabilities.
Code baseline: `12febab0d1689f994c0f07ad28ba0834080ebddf`.
Tracking: PR #177 and qualification issue #178. V2 remains alpha.

## Delivery ownership and boundaries

Claude implements agreed product corrections from a checksum-verified current
source snapshot. ChatGPT reviews, integrates and verifies repository/CI results.
Never substitute invented interfaces for missing files. Work only in V2 on its
existing feature branch. No V1 modification, main merge or deployment is included.
Keep PR #177 draft. Vendor capabilities are inspiration, not evidence about IVMS.

## Service and workspace architecture

Separate background services from interactive workspaces. A workspace is a
permission-controlled view; it need not be a separately installed application.
Support a single-machine deployment initially, with explicit service boundaries
that permit later distributed placement.

| Component | Responsibility | Authorization boundary |
| --- | --- | --- |
| Management service | Identity, permissions, configuration, camera registry, policy | Server-enforced permissions and site/camera scope |
| Media gateway | Authorized live transport and viewer connection control | Session-bound admission and revocation |
| Recording service | Acquisition, durable segments, indexes, retention, recovery | Assigned camera ownership and storage access |
| Device integration service | Camera/NVR discovery, capabilities, archive search and retrieval | Encrypted device credentials; scoped requests |
| Event service | Event ingestion, alarms and operator workflow | Current permission and scope on every delivery boundary |
| Health service | Recording gaps, disk/NAS health, worker state and diagnostics | Health visibility separate from video/configuration privileges |
| Administration workspace | Provision devices, storage, policies, users and server roles | Explicit administration privileges |
| Operator workspace | Live, playback, PTZ, alarms and permitted exports | Least privilege; no implicit administrator rights |
| Supervisor workspace | Wall assignments, incident coordination and audit review | Explicit supervisory privileges, still camera-scoped |
| Health workspace | Fault triage and maintenance actions | Separate read-health and perform-maintenance permissions |

These are logical boundaries. Do not introduce independent microservices merely
to match this table. Reuse existing components where their contracts are sound.

## Flexible display requirements

- Retain preset grids as shortcuts; also support snapping, resizing, merging and
  splitting tiles, with deliberate rules for overlap and minimum usable size.
- Save named layouts with owner, sharing permissions and version conflict checks.
- Restore camera identity and placement, never persisted live-session tokens.
- Support a staged path to multiple monitors and assigned video-wall displays.
- Allow selected-tile playback while other tiles remain live; show mode clearly.
- Alarm enlargement must preserve and restore the prior layout predictably.
- Use tile size to select camera streams under a declared decoder/bandwidth budget.
- Test cancellation, rapid camera replacement, unmount, resource cleanup, reload,
  permission changes and independent failure of one tile.
- Start with the adjustable grid. Free-position canvases and multi-monitor wall
  orchestration follow only after the ordinary client lifecycle is reliable.

## Recording and storage modes

| Mode | Recording authority | IVMS behavior |
| --- | --- | --- |
| Direct | IVMS recorder | Write to approved local or mounted storage, maintain index and retention |
| NVR-managed | External NVR | Search and play supported remote archives without compulsory copying |
| Edge retrieval | Camera/compatible device | Retrieve available missing intervals following interruption |
| Hybrid | Explicit per-camera policy | Combine approved sources with optional independent recording/archive copies |

NAS is storage, not an NVR archive protocol. A device's live RTSP support is not
proof of archive-search, historical playback or export support. Capability
discovery and a tested vendor/model/firmware matrix must determine which actions
are offered. Do not claim universal NVR compatibility.

Unified timeline entries must retain camera identity, source, start/end time,
timezone conversion, availability, integrity and retrieval status. Handle clock
offset, missing ranges, overlapping copies, retention changes and unreachable
devices explicitly. Remote playback, gap recovery and backup are distinct jobs.
Copy jobs need bounded concurrency/bandwidth, cancellation, retries, resumability,
deduplication and auditable completion. A failed copy must not be labelled saved.

Mounted NAS support requires testing sustained write behavior, disconnect/full
volume recovery and file/index consistency. Do not assume atomic rename across
volumes. Define local buffering limits and what happens when they are exhausted.

## Availability terminology and design constraints

Specify resilience independently for management/API, database, recording, live
media, storage and device connectivity. No single global 'active-active' flag.

- Active-active management: multiple usable service instances backed by a
  consistent database; compatible session, revocation and rate-limit behavior.
- Recorder takeover: assigned cameras move to available capacity after failure.
  Require leases, generation/fencing checks and an explicit partition policy.
- Redundant recording: intentional independent copies recorded concurrently;
  each copy has its own storage namespace and ownership. This is different from
  accidental duplicate writers after a network partition.
- Shared storage: its own failure domain. Two recorders do not make one NAS HA.
- Recovery: validate ownership before failback; reconcile indexes and footage
  without deleting the only good copy or interrupting unaffected cameras.

Before implementation, approve measurable recovery-time and data-loss targets,
capacity reserve, quorum/witness behavior, partition behavior, and supported
failure combinations. Never promise zero loss from standby takeover alone.
Measure camera reconnect and buffering behavior on actual target hardware.

## Ordered work batches and acceptance

| Batch | Scope | Exit gate |
| --- | --- | --- |
| 2 — next | Event-stream authentication, continuing authorization and trusted client IP | Backend/frontend protocol matches; negative and revocation tests; full CI |
| 3 | Browser live lifecycle and genuine fallback | Browser tests for cancellation, ICE/WHEP, reconnection, cleanup and media flow |
| 4 | Role workspaces and flexible grid | API authorization matrix, layout persistence/concurrency tests and usable client demo |
| 5 | Device archive adapters and unified playback | One named NVR/firmware plus edge device qualified for search/playback/export; honest capability limits |
| 6 | NAS/hybrid recording and recovery | Disconnect/full-disk/restart tests, bounded buffering, verified files/indexes and copy integrity |
| 7 | Distributed ownership and failover | Partition, worker-loss, lease-expiry, storage-loss and failback tests with measured recovery targets |
| 8 | Redundant recording and active-active management | Independent-copy proof, multi-instance security consistency, load and failure evidence |
| 9 | Target deployment qualification | Windows/Linux installer, real cameras, browser/desktop, soak and operational recovery evidence |

Batch 2 must not implement all later features. New platform requirements do not
waive current security defects. Do not count static source guards, successful
compilation or mocks as runtime/hardware qualification.

## Current evidence and gaps

Baseline CI run 41187175474: all five jobs passed; 256 tests passed, six gateway
tests skipped. Those six gateway tests separately passed locally on official
checksum-verified MediaMTX v1.21.2 using a synthetic camera, including selective
RTSP revocation. This does not qualify WebRTC, HLS revocation, Windows playback,
NVR archive integrations, deployed NAS, distributed failover or active-active.

The Windows client source has Basic-auth handling; its actual playback remains
unqualified. Some older limitation text predates these results. Preserve dated
evidence and update claims only as the relevant acceptance test is executed.
