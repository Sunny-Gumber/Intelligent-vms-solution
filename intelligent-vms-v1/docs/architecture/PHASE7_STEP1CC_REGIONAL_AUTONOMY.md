# Phase 7 Step 1C-C — Regional Autonomy During WAN / Control Loss

## Goal

Allow an already-authorized regional media/recording owner to continue through a
temporary central-control outage without creating split-brain ownership.

This milestone intentionally prefers safety over instant failover.

## Authority model

Each placement assignment has:

- `node_id`
- `generation`
- `lease_expires_at`
- `autonomy_expires_at`
- `applied_generation`

The short central lease remains the normal online authority.

When `PLACEMENT_OFFLINE_AUTONOMY_SECONDS > 0`, the placement controller also
pre-grants a bounded offline authority deadline while the current node is healthy.

The node receives that deadline in its fence snapshot and persists it locally.

## CAP tradeoff

During a WAN partition the central controller cannot safely know whether the old
node is still recording.

Therefore central placement **must not** assign a new owner while the old owner's
pre-granted autonomy could still be valid.

Flow:

```text
healthy node A / gen5
    -> lease 60s
    -> autonomy until T+300s
WAN loss
    -> A may continue gen5 until autonomy deadline
    -> central marks A stale
    -> central defers failover while autonomy is live
autonomy deadline expires
    -> A fences local work
    -> central may assign B / gen6
```

This prevents A/gen5 and B/gen6 from both being valid owners.

## Revocation wins over autonomy

A local revocation tombstone always wins.

A generation that is already revoked cannot use a cached autonomy grant.
Delayed lower-generation snapshots/revocations also cannot tear down a newer
accepted failback generation.

## Node authority modes

Node agent reports one of:

- `central_online`
- `regional_autonomous`
- `fenced_degraded`

The value is persisted locally and included in the next successful heartbeat.

## Historical recording backfill

A recording segment can arrive centrally after failover.

To validate it safely, the revocation row stores the old generation's
`valid_until` value.

Backfill is accepted only when:

- node ID and generation match the historical revocation; and
- the segment completion time is <= that historical `valid_until`.

Segments outside that window are rejected.

## Regional durable spool

The optional `regional-spool` service persists locally:

- completed recording metadata;
- normalized events;
- the latest node heartbeat (coalesced).

SQLite uses WAL + FULL synchronous mode.

The spool:

- deduplicates event IDs and recording spool IDs;
- coalesces heartbeats;
- preserves retry backoff across heartbeat updates;
- applies bounded exponential backoff;
- bounds queue length, dead letters and item size;
- moves terminal 4xx failures to a bounded dead-letter table;
- retries node 404 because registration may be completed later.

The queue is at-least-once. End-to-end transactional event exactly-once semantics
remain part of the later Phase 7 reliable-event-delivery milestone.

## Recording callback routing

Default:

```
RECORDING_HOOK_CALLBACK_URL=http://control-api:8000/internal/v1/recording/segments/complete
```

Regional WAN-durable deployment:

```
RECORDING_HOOK_CALLBACK_URL=http://regional-spool:8090/v1/recording/segments/complete
```

## Event routing

Set:

```
REGIONAL_SPOOL_URL=http://regional-spool:8090
REGIONAL_SPOOL_TOKEN=<shared internal secret>
```

The ONVIF event worker then queues normalized events locally instead of requiring
central Kafka reachability.

## Heartbeat routing

Set:

```
HEARTBEAT_SPOOL_URL=http://regional-spool:8090/v1/heartbeat
```

The heartbeat payload is coalesced locally and forwarded using the configured
node service token.

Fence snapshots still go directly to central control because stale authority must
not be fabricated locally.

## Failure guarantees

- WAN loss does not immediately stop a valid owner when autonomy is enabled.
- Central cannot fail over until the outstanding autonomy deadline ends.
- Revoked generations never gain offline authority.
- Restart during WAN loss restores persisted fence/autonomy state.
- Metadata survives regional process restart through the local spool DB.
- Retry storms are bounded.
- Spool overflow fails closed; data is never silently discarded.

## Non-goals

- exactly-once event outbox across DB/Kafka;
- autonomous new camera placement while central authority is unknown;
- cross-region quorum consensus;
- Phase 8 hardware certification.

## Heartbeat freshness across WAN recovery

Regional heartbeats carry an explicit `observed_at` timestamp. The control plane uses that observation time, not the later spool-delivery time, for node freshness. A delayed heartbeat therefore cannot make stale telemetry appear current after WAN recovery.

Placement also requires `authority_mode=central_online` before renewing the short lease or offline autonomy grant. Heartbeats reporting `regional_autonomous` or `fenced_degraded` remain ineligible until the node has successfully fetched and applied a fresh central fence snapshot.

Future observation timestamps are clamped to central server time so node clock skew cannot extend freshness.
