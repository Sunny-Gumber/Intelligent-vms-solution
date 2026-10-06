# Phase 5 Architecture — ONVIF Event Workers, Alarm Engine & Diagnostics

## Runtime separation

```text
ONVIF cameras
    │ persistent PullPoint long polls
    ▼
regional ONVIF event workers
    │ normalized vms.events.v1
    ▼
Kafka ────────────────┬───────────────────────┐
                     ▼                       ▼
              ClickHouse writer         Alarm worker
                                             │
                                      PostgreSQL alarms
                                             │
                                      Operator API/UI
```

Long-poll sockets do not live in FastAPI. Alarm evaluation does not run in the ClickHouse writer. Each plane can scale/restart independently.

## ONVIF worker ownership

Each worker can be constrained to one `media_node_id` and a deterministic `SHARD_COUNT/SHARD_INDEX`. This avoids duplicate subscriptions when several workers serve the same region. `SCAN_LIMIT` must cover the assigned media-node camera population.

Default developer density is 200 persistent camera subscriptions/worker. This is not a production sizing claim; actual socket/RAM/CPU limits are measured on target hardware.

## Event safety

- Event XAddr and camera-returned PullPoint addresses require the exact tenant/site camera CIDR policy and are pinned to one approved IP before credential-bearing requests.
- Scoped SOAP requests re-apply site pinning at the final network boundary; missing event scope fails closed rather than falling back to the global allowlist.
- XML is parsed by defusedxml.
- SOAP responses are streamed under a cumulative byte cap, including chunked responses without Content-Length.
- The whole SOAP operation has a bounded deadline; PullMessages keeps explicit headroom over the requested long-poll interval.
- exponential retry backoff avoids reconnect storms.
- WS-Addressing ReferenceParameters returned by the device are preserved.
- SetSynchronizationPoint is best-effort because vendor behavior varies.

## Alarm engine

Rules are relational control-plane state. Event matching supports:
- tenant;
- optional site;
- event type or wildcard;
- optional event severity;
- optional camera allowlist;
- alarm severity;
- cooldown.

The worker indexes cached rules by tenant/site/event type so evaluation does not scan the entire global rule set.

Alarm deduplication uses a deterministic SHA-256 key of rule/camera/event-type/cooldown-bucket. A database unique constraint makes the cooldown race-safe across alarm-worker replicas.

Initialized ONVIF property events never open an alarm.

## Diagnostics

Current diagnostic API reads MediaMTX Prometheus text and exposes:
- path state;
- inbound/outbound byte counters;
- short-interval Mbps when two samples are available in the same API replica;
- RTP packets;
- lost packets;
- RTP input errors;
- jitter.

For production history and HA, Prometheus/Grafana is the source of truth for rates/time-series; the API remains a current-state convenience view.

## Known next reliability improvement

Health transitions and normalized camera events currently publish directly to Kafka. Production-grade guaranteed delivery from PostgreSQL state transitions requires an outbox pattern, scheduled for the reliability/distributed phase.
