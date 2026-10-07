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

- Event XAddr and camera-returned PullPoint addresses require the exact tenant/site camera CIDR policy; every credential-bearing SOAP request connects to one freshly approved pinned IP.
- Camera-returned PullPoint addresses are stored as pinned destinations, while their original logical authority is retained for WS-Addressing, HTTP Host and HTTPS SNI/certificate verification.
- Scoped SOAP requests re-apply site pinning at the final network boundary; a DNS change outside the site is rejected before the HTTP client receives credentials, and missing event scope fails closed rather than falling back to the global allowlist.
- XML is parsed by defusedxml.
- Every consumed HTTP response in an HTTP Digest exchange is streamed under the configured cumulative byte cap before any authenticated retry can be sent; this includes chunked 401 challenges without Content-Length and the final SOAP response. Terminal 401/403 bodies are closed without consumption.
- Malformed, incomplete or HTTPX-unsupported Digest challenge fields are normalized at the Digest auth-flow boundary to `DEVICE_SERVICE_INVALID` / HTTP 502 without consuming the challenge body or sending an authenticated retry. Credential rejection after a valid challenge remains `AUTH_FAILED` / HTTP 401.
- Credential-bearing ONVIF requests explicitly request `Accept-Encoding: identity`; non-identity Content-Encoding is rejected before body consumption so HTTP content-decoder expansion cannot bypass the application byte cap. The retained application payload is therefore at most the configured cap per consumed response; the transport may still materialize one transport-delivered chunk before the iterator returns control.
- The whole SOAP operation, including Digest challenge/retry processing and final response streaming, has a bounded deadline; PullMessages keeps explicit headroom over the requested long-poll interval.
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
