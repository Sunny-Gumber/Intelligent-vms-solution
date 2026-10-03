# Phase 7 Regional Chaos Qualification

Tracking: #209

## Purpose

This is the deterministic software exit gate for Phase 7 distributed/regional behavior. It does not certify real hardware throughput, WAN equipment, storage arrays, camera firmware, or 100K media capacity; those remain Phase 8 and external qualification work.

## CI command

`python tools/phase7_chaos_matrix.py`

The runner executes every named scenario with explicit pytest node IDs, a per-scenario timeout, and skip rejection. Any missing/failing/skipped scenario fails CI.

## Required matrix

| # | Failure injection | Safety invariant |
|---:|---|---|
| 1 | Media node loss | stale owner is not renewed; failover rotates generation and persists revocation |
| 2 | Recording node loss | new recording generation is re-applied; stale recording evidence is rejected |
| 3 | Controller interruption/restart | persisted fence/autonomy state survives restart; expired ownership fences |
| 4 | Temporary PostgreSQL failure | worker loop survives transient DB exception and continues after recovery |
| 5 | Kafka outage/recovery | valid events remain retryable, never outage-DLQ'd, and publish after recovery |
| 6 | Stale heartbeat | delayed telemetry cannot appear fresh or renew autonomy |
| 7 | Old failover node unreachable | cleanup obligation is retained and retried after node returns |
| 8 | A -> B -> C -> A | cleanup/failback state remains safe across repeated ownership changes |
| 9 | Delayed fence/revocation | old snapshot/revoke cannot downgrade/delete a newer generation |
| 10 | WAN/control isolation | bounded pre-granted autonomy continues only to its hard deadline |
| 11 | Reconnect storm | idempotent bounded backlog survives repeated failures and drains on recovery |
| 12 | Spool near-full/full | queue fails closed at capacity and dead-letter retention remains bounded |
| 13 | Fence-state disk failure | revoke is not ACKed if durable local tombstone write fails |
| 14 | Tenant/site/service isolation | tenant/site/node-scoped identities cannot cross authorization boundaries |

## Exit decision

Phase 7 may close only when:

- the full normal unit suite passes;
- all 14 chaos scenarios pass in both relevant CI workflows;
- migrations/Compose/images remain green;
- Reviewer finds no BLOCKER/HIGH split-brain, silent-loss, recording-safety, or authorization issue.

## Deferred to Phase 8 / external qualification

The following are intentionally not claimed by this software gate:

- physical server power/network failure under sustained media load;
- switch/NIC packet loss and jitter behavior;
- real disk-full filesystem/object-store behavior under sustained recording;
- PostgreSQL/Kafka/ClickHouse HA cluster failover on production hardware;
- 24–72 hour real-camera soak;
- 500/2K/10K/100K throughput or hardware sizing;
- real vendor camera interoperability.

Those require measured environment evidence and cannot be inferred from deterministic CI.