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
- Malformed, incomplete or HTTPX-unsupported Digest challenge fields are normalized at the Digest auth-flow boundary to `DEVICE_SERVICE_INVALID` / HTTP 502 without consuming the challenge body or sending an authenticated retry. The raw parser/build cause is deliberately not exception-chained because challenge text is camera-controlled and worker `log.exception` output must not expose it. Credential rejection after a valid challenge remains `AUTH_FAILED` / HTTP 401.
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

### Alarm rule read and write scope

Rule listing deliberately includes shared null-site rules in the caller's tenant.
Visibility does not confer mutation authority. Create/update retain admin/operator
role gates; DELETE remains admin-only and soft-disables the rule so historical
alarm instances keep their references. An admin role does not bypass tenant/site
scope. Alarm instance acknowledge/close retain their existing instance-site checks.

Mutation validates the rule tenant first, then its complete matching target:

- A site-specific rule requires authority over that site. Every explicit camera
  must also exist, belong to the rule tenant/site and be authorized.
- A null-site rule with a nonempty camera filter requires authority over every
  referenced camera in the rule tenant, using one batched camera query per check.
- A null-site rule with an empty or omitted camera filter is tenant-wide and
  requires `site_ids:["*"]` in the authorized tenant. All-site scope alone gives
  no cross-tenant authority. Existing explicitly trusted `tenant_id:"*"` semantics
  are preserved without granting any new role-based global exemption.

PATCH authorizes both persisted scope and the complete proposed result before
assigning any ORM field. Omission retains values, duplicate camera IDs normalize
to a set, and clearing a null-site camera filter requires all-site authority.
Explicit nulls for mutable PATCH fields return 422; null is not a reset operation.
Missing/moved/inconsistent cameras fail closed, including metadata-only changes
and soft-delete. Persisted camera filters exceeding the current 1000-entry API
bound or containing malformed identifiers also fail closed before a camera query.
Out-of-scope access uses the existing 404 convention; invalid camera references
and tenant/site inconsistency use the existing 422 convention.

Concurrency limitation: these checks validate the current transaction's observed
rule/camera state. No row locking, optimistic version check or serializable
transaction is added. Concurrent camera moves or rule updates can race validation
and commit; this fix does not claim isolation across those operations. A later
camera move also changes effective camera-rule coverage until reconciliation or
the next mutation check. The matcher continues to treat empty filters as wildcard.

Executable HTTP/persistence matrix: `tests/test_alarm_write_scope.py`. It substitutes
synthetic identity acquisition only, retaining real role and scope authorization.

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
