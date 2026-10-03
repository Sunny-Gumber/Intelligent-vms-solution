# Regional Autonomy Operations Runbook

## Default state

Offline autonomy is disabled by default:

```
PLACEMENT_OFFLINE_AUTONOMY_SECONDS=0
```

Do not enable it until node fencing and regional spool tests are green in the
target release.

## Safe enablement

1. Confirm PostgreSQL is used for distributed production mode.
2. Confirm Step 1C-B fencing is enabled and node-agent state is on durable storage.
3. Register regional nodes and trusted endpoints.
4. Configure a node-scoped service token.
5. Start `regional-spool` with a persistent volume.
6. Set a non-zero autonomy window, initially conservatively.
7. Point regional recording callbacks, events and optional heartbeats at the spool.
8. Verify node authority mode reports `central_online`.
9. Simulate WAN loss.
10. Verify authority mode changes to `regional_autonomous`.
11. Confirm recording paths stay present through normal lease expiry.
12. Confirm central placement reports/deals with autonomy-deferred failover.
13. Let the autonomy deadline expire in a controlled test.
14. Confirm the node enters `fenced_degraded` and local paths are removed.
15. Restore central connectivity and verify reconciliation/backfill.

## Suggested initial lab setting

Example only:

```
PLACEMENT_LEASE_SECONDS=60
PLACEMENT_OFFLINE_AUTONOMY_SECONDS=300
```

This is not a production recommendation; Phase 8/9 failure testing determines
the final operational value.

## Regional spool configuration

Required secrets:

```
NODE_AGENT_TOKEN=<node scoped token>
RECORDING_HOOK_TOKEN=<recording hook shared secret>
REGIONAL_SPOOL_TOKEN=<regional internal shared secret>
```

Optional routing:

```
HEARTBEAT_SPOOL_URL=http://regional-spool:8090/v1/heartbeat
REGIONAL_SPOOL_URL=http://regional-spool:8090
RECORDING_HOOK_CALLBACK_URL=http://regional-spool:8090/v1/recording/segments/complete
```

Spool bounds:

```
SPOOL_MAX_ITEMS=100000
SPOOL_MAX_DEAD_LETTERS=10000
SPOOL_MAX_BODY_BYTES=262144
SPOOL_BATCH_SIZE=100
SPOOL_BACKOFF_MAX_SECONDS=60
```

## Operator interpretation

### central_online

Fresh fence snapshots are being received.

### regional_autonomous

Central fence snapshots are unavailable, but at least one cached assignment has
a live pre-granted autonomy window.

Do not force central failover before that grant expires unless an out-of-band
hard fence has definitely stopped the old node.

### fenced_degraded

No central authority and no live offline grant remains. Existing fenced work must
stay stopped until authority is re-established.

## Spool health

`GET /health` returns:

- queued item count;
- dead-letter count.

The regional spool is not exposed to the public network in the reference Compose
topology.

## Recovery

On WAN restoration:

1. Fence snapshot succeeds.
2. Node mode returns to `central_online`.
3. Heartbeat latest-state item drains.
4. Recording/event spool drains in bounded batches.
5. Current assignment generation is reconciled.
6. Historical recording metadata is accepted only inside its old generation's
   recorded validity window.
7. Terminal stale metadata moves to dead-letter instead of retrying forever.

## Full spool

If the queue reaches its configured bound, ingestion returns 507.

This is an explicit data-protection failure. Alert and restore connectivity or
increase local durable capacity after investigating the cause. Do not configure
silent dropping.

## WAN flap

Heartbeat coalescing updates only the latest payload and **does not reset the
existing retry schedule**, preventing one retry per heartbeat during an outage.

## Known limitation

Generic event delivery is currently at-least-once. The next Phase 7 reliable event
delivery milestone adds the transactional outbox/idempotent-consumer boundary.

## Heartbeat freshness across WAN recovery

Regional heartbeats carry an explicit `observed_at` timestamp. The control plane uses that observation time, not the later spool-delivery time, for node freshness. A delayed heartbeat therefore cannot make stale telemetry appear current after WAN recovery.

Placement also requires `authority_mode=central_online` before renewing the short lease or offline autonomy grant. Heartbeats reporting `regional_autonomous` or `fenced_degraded` remain ineligible until the node has successfully fetched and applied a fresh central fence snapshot.

Future observation timestamps are clamped to central server time so node clock skew cannot extend freshness.
