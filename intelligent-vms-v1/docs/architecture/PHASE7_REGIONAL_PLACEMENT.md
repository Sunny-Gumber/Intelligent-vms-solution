# Phase 7 — Regional placement and failover

## Step 1A scope

This step adds the distributed control-plane primitives without yet moving live media between nodes:

- regional node registry;
- site -> region mapping;
- bounded node heartbeats;
- normalized capacity/load reports;
- durable media/recording/AI assignments;
- deterministic placement with headroom;
- assignment generations and leases;
- singleton placement-controller worker;
- bounded failover moves.

Step 1B makes MediaMTX/recording reconciliation node-aware and executes those assignments.

## Control model

```text
site -> region
camera -> media assignment -> media node
recording policy -> recording assignment -> recording node
AI policy -> AI assignment -> AI node
```

The existing `cameras.media_node_id` and `recording_policies.recording_node_id` remain the operational fields. `placement_assignments` records durable ownership/generation history for safe reconciliation.

## Node heartbeat

Each node publishes:

- roles: media / recording / ai;
- region;
- endpoint metadata;
- capacity;
- current load;
- state: active / draining / maintenance.

A node is eligible only when:

1. enabled;
2. state is active;
3. heartbeat age is within the configured stale threshold;
4. required role is present;
5. calculated utilization stays below placement headroom.

## Capacity fields

Media:
- max_ingress_mbps
- max_egress_mbps
- max_sources
- ingress_mbps
- egress_mbps
- active_sources

Recording:
- max_record_mbps
- max_recordings
- record_mbps
- active_recordings

AI:
- max_ai_mpix_s
- max_ai_jobs
- ai_mpix_s
- active_ai_jobs

Unknown/zero capacity is intentionally not treated as infinite capacity.

## Placement score

Eligible nodes are ordered by:

1. same region (mandatory);
2. lowest maximum utilization ratio across the role-specific dimensions;
3. lowest active workload;
4. stable node ID tie-break.

This makes repeat placement deterministic for the same observed state.

## Leases and fencing

Every assignment has:
- generation;
- lease_expires_at;
- active flag.

Generation increments only on ownership change. Later node-side reconcilers must include/observe generation when acquiring work. Phase 7 Step 1B introduces that execution boundary.

The placement controller uses a PostgreSQL advisory transaction lock so multiple replicas do not independently mutate assignments.

## Failure policy

- draining/maintenance nodes receive no new work;
- stale nodes become ineligible;
- failover is bounded by `PLACEMENT_MAX_MOVES_PER_RUN`;
- a controller outage does not delete current assignments;
- no mass deletion of unknown media paths is performed;
- site/region isolation is preserved.

## 100K rule

The controller processes camera IDs in bounded batches with a persisted cursor. It never loads 100K camera objects plus every node assignment into one request cycle.
