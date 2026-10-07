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
issuing a conditional database write. Omission retains values, duplicate camera IDs normalize
to a set, and clearing a null-site camera filter requires all-site authority.
Explicit nulls for mutable PATCH fields return 422; null is not a reset operation.
Missing/moved/inconsistent cameras fail closed, including metadata-only changes
and soft-delete. Persisted camera filters exceeding the current 1000-entry API
bound or containing malformed identifiers also fail closed before a camera query.
Out-of-scope access uses the existing 404 convention; invalid camera references
and tenant/site inconsistency use the existing 422 convention.

PATCH (including metadata/enabled changes) and admin soft-delete use the same
atomic conditional UPDATE: ID, authorized tenant/site/camera filter and observed
`updated_at` must still match. The UPDATE changes `updated_at` explicitly and
returns the written row; zero matching rows returns HTTP 409 after rollback.
Each API write advances the revision by at least one microsecond, including when
the wall clock repeats or moves backwards; no new version column is necessary.
No ORM field is dirtied before the comparison, and the response describes this
write rather than a post-commit refresh that could observe a subsequent writer.
The scope predicate is explicit, so scope safety does not rely on timestamp
uniqueness. The timestamp additionally detects ordinary same-scope edits.
POST creates a new row and cannot invalidate an existing rule's authorization.
These are all supported rule writers; the alarm worker only reads rules.

PostgreSQL's READ COMMITTED UPDATE rechecks the predicate after a competing
writer commits. Camera JSON is compared as JSONB (the persisted column remains
JSON); SQLite compares normalized JSON and its serialized writers either
recheck the predicate or reject a stale read transaction. Busy/locked,
serialization/deadlock and statement-deadline failures return 409 after rollback.
There are no server retries: the caller must retry the entire request, including
authorization. PostgreSQL transaction-local lock/statement deadlines are 2/5
seconds; SQLite's connection busy deadline is 2 seconds. Other database dialects
fail closed with 503. Cancellation propagates and request-session closure rolls
back unfinished work. No schema/version migration or new endpoint is required.

Validation takes no application row locks. Each write updates exactly one rule,
so there is no multi-rule/camera lock ordering and no application-wide mutex;
unrelated PostgreSQL rows proceed independently. SQLite inherently has a single
database writer, with bounded contention. A stale site-limited mutation after
all-site broadening conflicts with 409; its fresh retry returns 404, preserving
the broadening writer's complete persisted row.

Camera tenant/site relocation is not exposed by ordinary camera PATCH. Direct
database camera relocation/deletion and future lifecycle operations are outside
this rule-writer transaction guarantee: they must preserve camera identity/scope
or add their own coordinated validation before being supported. A later camera
move changes effective matching until reconciliation; this is not a waiver of
supported concurrent rule writes. Direct SQL that changes rule scope is detected
by the explicit scope comparison, but arbitrary SQL bypassing revision updates
does not receive the API's same-scope lost-update guarantee. The matcher continues
to treat empty filters as wildcard. Existing malformed/stale filters fail closed.

Executable HTTP/persistence matrix: `tests/test_alarm_write_scope.py`. It substitutes
synthetic identity acquisition only, retaining real role and scope authorization.
`tests/test_alarm_write_transactions.py` runs independent-session HTTP races and
persisted-state checks on SQLite and explicitly configured PostgreSQL. Independent
QA retains its original token-auth/race ordering and security assertion, then
also executes that matrix and `tests/postgres/alarm_rule_write_contention.py`
against PostgreSQL 17, including actual lock waits, 409 cleanup and cancellation.

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
