# High availability and recording redundancy — DESIGN ONLY

**Status: NOT IMPLEMENTED.** This document exists so the design is on record
and so the gap is unambiguous. `FederationAggregator.cross_node_failover()`
and `distributed_recording()` raise `501 not_implemented`. Requirements
M10-R16, M18-R15 and M18-R17 are marked "designed only" in the feature matrix.

## Why it is not implemented

N+1 recording failover needs three things this release does not have:

1. **Distributed leadership** — exactly one node must own a camera at a time,
   or two recorders write overlapping segments and the archive becomes
   ambiguous. That needs a lease mechanism (PostgreSQL advisory locks, etcd,
   or Raft) with proven fencing.
2. **Shared or replicated storage semantics** — a failover node must be able to
   write into the same logical archive without the index diverging.
3. **Tested split-brain behaviour** — the failure mode that matters is a
   network partition, and asserting that it works requires fault injection
   against real hardware.

Shipping an untested failover is worse than shipping none: operators would
believe they have redundancy they do not have.

## Proposed design (for a future release)

### Camera ownership leases

Each recording node acquires a lease per camera in PostgreSQL:

```sql
UPDATE recording_tasks
   SET worker_id = :node, heartbeat_at = now()
 WHERE camera_id = :camera
   AND (worker_id IS NULL OR heartbeat_at < now() - interval '30 seconds')
```

The conditional UPDATE is the fencing primitive: only one node can win. The
schema already carries `worker_id` and `heartbeat_at`, and
`recovery.release_stale_tasks()` already reclaims abandoned leases — that part
IS implemented and tested.

### N+1 standby

N active recording nodes plus one standby. The standby polls for cameras whose
lease has expired and acquires them. Recovery time is bounded by the lease TTL
(30 s default), which means **a planned gap of up to 30 seconds** on failover —
honesty about that gap is part of the design, not a defect to hide.

### N+N dual recording

Two nodes record the same camera to different volumes concurrently. Doubles
bandwidth and storage. Playback prefers the primary and falls back per segment.
Requires the index to carry a replica identifier (a schema change).

### Control-plane HA

The control API is stateless apart from in-process rate limiters and the event
queue. Running several behind a load balancer requires:
- a shared rate-limit store (see `docs/LIMITATIONS.md`),
- a shared event bus so WebSocket subscribers on any node see every event,
- leader election for singleton loops (retention, escalation).

### Required validation before claiming any of this

- Partition the network between nodes; assert no double-recording.
- Kill the primary mid-segment; measure the real gap; assert the index is
  consistent.
- Fail the database; assert no node believes it holds a lease it has lost.
- 72-hour soak with induced failures.
