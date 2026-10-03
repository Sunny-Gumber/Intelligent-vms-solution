# Phase 4 architecture — Event Intelligence & Camera Health

## Data flow

```text
Camera RTSP TCP probe ──> regional health worker ──> PostgreSQL last-known state
                                    │
                                    └─ state transitions ─> Kafka ─> ClickHouse

MediaMTX active paths / metrics ───────── diagnostic context
ONVIF / AI / integrations ────────────────────────> Kafka ─> ClickHouse

Operator API ── scoped bounded query ─────────────> ClickHouse
Operator API ── scoped health query ──────────────> PostgreSQL
```

## Worker transaction pattern

1. lock cursor state and claim a bounded camera batch;
2. commit immediately;
3. run bounded-concurrency RTSP transport probes outside DB locks;
4. lock/update only those camera-health rows;
5. commit;
6. publish transition events to Kafka after the DB commit.

Persistent success/failure counters make hysteresis safe when multiple worker replicas share batches.

## Safety rules

1. Event queries are bounded by time window and row limit.
2. SQL structure is static; external values use ClickHouse query parameters.
3. Auth tenant/site scope is applied before storage query.
4. Health snapshots never store raw MediaMTX source configuration.
5. Source-on-demand path inactivity is not treated as camera failure.
6. Media-node management outage does not fan out into camera-offline events.
7. Kafka publication occurs after health-state DB commit; broker latency does not hold DB locks.
8. Only offline/recovery transitions produce availability events; heartbeat observations do not.

## Scale

The monitor scans a bounded camera batch and maintains a shared durable cursor. Production should run regional workers close to their assigned media nodes/sites. PostgreSQL stores durable last-known state; high-rate time-series metrics go to Prometheus/metrics storage rather than one relational row per metric sample.

The RTSP TCP probe verifies network/service reachability, not successful authentication or decodable video. Stronger health layers can add periodic authenticated RTSP OPTIONS/DESCRIBE or ONVIF probes at a lower cadence without changing the health-state API.

## Future ONVIF event worker

The worker will:
- maintain PullPoint subscriptions where supported;
- renew/recreate subscriptions;
- normalize topic/source/data into the common event schema;
- rate-limit malformed/noisy cameras;
- use vendor adapters only behind the normalization boundary.

## Future diagnostic health

Prometheus MediaMTX metrics add:
- bitrate derived from byte deltas;
- packet loss;
- RTP input errors;
- jitter;
- viewer counts.

These metrics should be aggregated regionally and only health transitions/summaries should enter the central event plane.
