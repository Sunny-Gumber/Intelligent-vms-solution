# Phase 4 independent review

Status: PASS FOR DEVELOPMENT MERGE WHEN CI IS GREEN. Not production certification.

## BLOCKERS found and returned to Development

### 1. Source-on-demand false offline
Initial implementation treated MediaMTX live-path readiness as camera availability. Live paths are source-on-demand, so an idle healthy camera could be marked offline.

Resolution: camera availability now uses a bounded RTSP TCP transport probe. MediaMTX readiness remains diagnostic only.

### 2. Probe work inside DB transaction
Network probes initially ran while an advisory DB transaction/lock was held.

Resolution: worker now claims a bounded cursor batch in a short transaction, probes outside DB, and applies results in a second short transaction.

### 3. Process-local hysteresis
Success/failure counters were initially process-local and unsafe across multiple workers.

Resolution: counters are persisted per camera and clamped at configured thresholds.

## HIGH findings

### Transition event durability
Health DB state commits before Kafka publication, which is correct for DB lock isolation, but a broker outage can lose a transition event after state commit.

Disposition: production reliability phase should add an outbox or transactional event-delivery pattern.

### RTSP TCP probe depth
TCP-connect confirms service/network reachability but not authenticated RTSP video decode.

Disposition: acceptable for Phase 4 availability foundation. Add lower-frequency authenticated RTSP/ONVIF deep probes in a later diagnostics phase.

## MEDIUM findings

- JWT site lists do not scale elegantly to thousands of sites; enterprise authorization should use policy/role bindings rather than giant token claims.
- ClickHouse event search returns a bounded latest page but does not yet provide a stable cursor for deep historical paging.
- MediaMTX Prometheus packet-loss/jitter metrics are not yet ingested into VMS diagnostic history.

## Security positives

- event filters use ClickHouse parameters rather than interpolated external values;
- query time range and row count are bounded;
- tenant/site scope is applied;
- health detail allowlist excludes source URIs/credentials;
- camera credential storage behavior is unchanged;
- management-node failure does not generate mass false camera events.

## Scale positives

- cursor-based bounded batch claims;
- configurable probe concurrency/timeouts;
- network probes outside DB transactions;
- stable cameras stop changing hysteresis counters once thresholds are reached;
- heartbeat persistence is rate limited;
- CPU/RAM sizing is tied to measured coefficients rather than invented per-camera values.

## Production certification remains blocked by

1. real-camera multi-vendor tests;
2. 24-hour+ soak;
3. Kafka outbox/durable transition delivery;
4. Prometheus/Grafana operational metrics;
5. OIDC production UI;
6. distributed media/recording placement;
7. object-storage archive and legal hold;
8. regional failover testing.
