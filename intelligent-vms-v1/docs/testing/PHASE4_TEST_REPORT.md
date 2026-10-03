# Phase 4 QA report

Status: IMPLEMENTATION READY FOR CI; real-media integration still required.

## Automated gates added

- health hysteresis: 3 failures -> offline, 2 successes -> recovery;
- transient failures remain degraded;
- health detail allowlist prevents RTSP source/credential persistence;
- empty allowed-site event scope short-circuits without ClickHouse;
- 100K structural-load arithmetic test;
- Alembic migration must create `camera_health_state`.

## Failure behavior reviewed

- MediaMTX control API unavailable: retain last camera states; increment node error counter; no 2,000-camera alarm storm.
- ClickHouse unavailable: event search returns service unavailable; live/recording control path remains separate.
- Kafka event publication failure after a health transition: DB state remains committed and failure is logged. Durable outbox is a later reliability enhancement.

## Remaining real-environment QA

Required before production certification:
1. two or more real ONVIF camera vendors;
2. camera cable pull/recovery;
3. MediaMTX kill/restart;
4. Kafka/ClickHouse kill/recovery;
5. event burst soak;
6. 24-hour health monitor soak;
7. packet-loss/jitter metrics validation;
8. log scan confirming no camera passwords.
