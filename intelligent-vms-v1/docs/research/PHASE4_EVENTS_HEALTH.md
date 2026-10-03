# Phase 4 research — events and health

## Research Agent conclusion

Phase 4 separates three concepts:

1. **camera transport availability** — a lightweight regional probe against the camera's RTSP service;
2. **media-plane activity/quality** — MediaMTX path/session state and Prometheus metrics;
3. **device/business/AI events** — normalized into the common Kafka/ClickHouse event envelope.

A source-on-demand media path can be idle while the camera is perfectly healthy. Therefore MediaMTX path readiness is diagnostic context, not the authoritative camera-online signal.

A MediaMTX management endpoint outage is a node-level incident and must not generate thousands of individual camera-offline alarms.

## ONVIF

ONVIF Core defines WS-Discovery for local discovery and the device service as the entry point for detailed capabilities.

Profile T covers advanced video streaming and includes motion/tampering events, metadata, PTZ and other conditional capabilities.

Profile M standardizes metadata and events for analytics applications. Its standardized metadata stream is mandatory for Profile-M conformant products, while ONVIF event-service and MQTT event paths are conditional.

Phase 4 therefore keeps the VMS event envelope vendor-neutral. A later ONVIF event worker can map PullPoint/metadata events into the same event topic without changing storage/UI contracts.

References:
- https://www.onvif.org/profiles/profile-t/
- https://www.onvif.org/profiles/profile-m/
- https://www.onvif.org/profiles/specifications/

## Media health

MediaMTX exposes:
- Control API active-path listing;
- Prometheus-compatible path and protocol metrics;
- inbound/outbound bytes;
- RTP packet loss, input errors and jitter for RTSP sessions.

The first Phase-4 health implementation uses a bounded RTSP TCP-connect probe for camera transport availability. MediaMTX readiness is stored only as diagnostic context. Health detail uses an allowlist so raw source URIs and camera credentials cannot be persisted accidentally.

Prometheus metrics are the next source for bitrate/packet-loss/jitter diagnostics.

References:
- https://mediamtx.org/docs/features/control-api
- https://mediamtx.org/docs/features/metrics

## Health semantics

- `unknown`: insufficient observations.
- `degraded`: transient transport failure below offline threshold.
- `online`: RTSP transport reachability confirmed.
- `offline`: consecutive transport failures reach threshold.

Default development hysteresis:
- 3 failed observations before offline;
- 2 successful observations before recovery.

Thresholds are configuration, not protocol truth.

## Scale / HA

Health workers claim bounded camera batches through a locked cursor row, release the DB transaction, probe concurrently outside the DB, then update state in a short second transaction. Hysteresis counters are persisted so multiple workers can share batches.

100K global cameras must not be probed across a WAN from one process. Production places these workers regionally beside media nodes and keeps each node/failure domain bounded.
