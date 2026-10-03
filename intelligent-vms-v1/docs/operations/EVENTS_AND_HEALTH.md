# Events and camera health operations

## APIs

- `GET /api/v1/health/summary` — scoped health counts.
- `GET /api/v1/health/cameras` — paged camera last-known health.
- `GET /api/v1/events` — bounded event search.
- `POST /api/v1/events` — normalized event ingest for authorized operators/services.

Event search defaults to the latest 24 hours and is capped by configured maximum window/limit.

## Health states

- unknown — not enough observations;
- degraded — transient readiness failure;
- online — stream path ready;
- offline — failure threshold reached.

A media-node management failure is not interpreted as every camera failing.

## Troubleshooting

### Camera offline
1. inspect camera health row;
2. inspect MediaMTX path state;
3. inspect RTSP session metrics;
4. confirm camera/VLAN reachability;
5. confirm credentials;
6. confirm recorder/live paths independently.

### Event search unavailable
Check ClickHouse health and event-writer lag. This should not stop live view or recording.

### Events missing
Check Kafka topic, event-writer consumer and ClickHouse insert errors.

## Security

Health detail intentionally omits raw source configuration. Do not add source URLs, passwords or full MediaMTX config objects to health responses/logs.
