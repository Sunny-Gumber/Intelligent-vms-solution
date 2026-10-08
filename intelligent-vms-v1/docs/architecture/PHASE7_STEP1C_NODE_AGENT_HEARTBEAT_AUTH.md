# Phase 7 Step 1C-A — Regional Node Agent + Heartbeat/Auth Hardening (Implementation Handoff)

## 1) Scope and intent

Implement a **node-local agent** that reports bounded, trustworthy node telemetry to existing Phase 7 infrastructure endpoints, with node-scoped auth enforcement in control-api.

This step is **telemetry + auth hardening only**. It does not change placement algorithm semantics, media reconciliation behavior, or camera assignment execution.

---

## 2) Responsibilities and boundaries

### Node-agent (new service)
- Owns node-local measurement collection and heartbeat delivery.
- Calls control-api for:
  - `POST /api/v1/infrastructure/nodes/{node_id}/heartbeat` (periodic measured load/liveness)
- Does **not** mutate trusted node endpoints, region/role ownership, cameras, recording policies, AI policies, placements, or MediaMTX config.
- Node registration and trusted endpoint configuration remain admin-managed.
- Must not terminate or reconfigure media/recording workloads on heartbeat failures.

### control-api (existing service, hardened)
- Remains source of truth for `infrastructure_nodes` latest accepted snapshot.
- Keeps trusted node registration limited to a global administrator (role `admin` and `tenant_id` `"*"`) and enforces node-scoped service tokens on heartbeat routes. A tenant-scoped administrator is refused.
- Persists heartbeat snapshots; placement-controller consumes persisted values.

---

## 3) Data flow (node-agent -> control-api heartbeat)

1. Admin pre-registers the node and trusted endpoint URLs.
2. Agent boots and validates config.
3. Agent starts with heartbeat POST:
   - collect measured metrics
   - normalize/clamp
   - send measured load only
4. On transport/server failure, apply bounded exponential backoff + jitter.
5. On 404, log registration-required and retry later; the node-scoped service token never performs trusted registration.
6. Existing media/recording/AI processes continue regardless of heartbeat status.

---

## 4) API/contracts

## 4.1 Node registration (admin-managed)
- `PUT /api/v1/infrastructure/nodes/{node_id}`
- Request: existing `NodeUpsert` schema.
- Used by trusted administration/orchestration, not by the node-scoped heartbeat agent.
- Owns trusted endpoints, region/roles, enabled state and initial configured-safe limits.

## 4.2 Heartbeat (node-scoped service)
- `POST /api/v1/infrastructure/nodes/{node_id}/heartbeat`
- Request: existing `NodeHeartbeat` schema (`state?`, `load`).
- Endpoint mutation is forbidden.

## 4.3 Payload mapping (measured vs configured-safe)

Placement-critical keys:
- Capacity is **admin-managed** on the node record:
  - media: `max_ingress_mbps`, `max_egress_mbps`, `max_sources`
  - recording: `max_record_mbps`, `max_recordings`
  - ai: `max_ai_mpix_s`, `max_ai_jobs`
- Step 1C-A measured load:
  - media: `ingress_mbps`, `egress_mbps`, `active_sources`
  - recording: `active_recordings` only; `record_mbps` is intentionally not invented
  - ai: no placement load is fabricated; `ai_mpix_s` and `active_ai_jobs` must come from the AI runtime/scheduler

Telemetry-only measured keys (additional, placement-ignored for now):
- `host_cpu_utilization_pct`
- `ram_total_bytes`, `ram_used_bytes`
- `disk_total_bytes`, `disk_free_bytes`
- `net_rx_bps`, `net_tx_bps`, `net_rx_bytes_total`, `net_tx_bytes_total`
- `process_uptime_seconds`
- `mediamtx_reachable` (0/1 numeric)
- `mediamtx_configured_paths`, `mediamtx_live_sources`, `mediamtx_recording_paths`

Invariants:
- All numeric metrics are non-negative; NaN/inf invalid -> coerced to 0.
- Unknown/unmeasured placement load is never treated as zero utilization.
- A configured positive capacity dimension without its corresponding measured load makes that role ineligible.
- The agent cannot raise its own capacity and never invents camera-per-server constants.

---

## 5) Exact node-agent env contract

Required:
- `NODE_ID` (must equal path node ID used in API calls)
- `REGION_ID`
- `NODE_ROLES` (comma list of `media,recording,ai`, unique, non-empty)
- `CONTROL_API_URL` (absolute http/https, no query/fragment)
- `NODE_AGENT_TOKEN` (bearer token; env/secret mount only)

Recommended defaults:
- `NODE_NAME` (default: `NODE_ID`)
- `HEARTBEAT_INTERVAL_SECONDS` (default 10, min 2)
- `HEARTBEAT_TIMEOUT_SECONDS` (default 3, bounded)
- `HEARTBEAT_BACKOFF_INITIAL_SECONDS` (default 1)
- `HEARTBEAT_BACKOFF_MAX_SECONDS` (default 60)
- `HEARTBEAT_BACKOFF_JITTER_RATIO` (default 0.2)
- `RECORDING_MOUNT_PATH` (default `/recordings`)
- `NETWORK_INTERFACE` (optional; auto-detect if empty)
- `MEDIAMTX_API_URL` (required when role contains `media` or `recording`)

Configured-safe placement limits are not node-agent environment variables. They are registered by a global administrator (role `admin` and `tenant_id` `"*"`) on the infrastructure-node record.

Startup validation failures are fatal for bad URLs, bad role sets, invalid timing/backoff settings, or a missing MediaMTX API URL for media/recording roles.

---

## 6) Security and tenant boundaries

Token model for node-agent -> control-api:
- JWT or equivalent signed token from identity system.
- Required claims for service token:
  - `sub` (node service identity)
  - `roles` includes `service`
  - `tenant_id` = `*`
  - `site_ids` includes `*`
  - `node_id` (mandatory for node-agent calls)

control-api enforcement:
- `PUT /api/v1/infrastructure/nodes/{node_id}`: global-administrator trusted registration/configuration/drain. The caller must have role `admin` and `tenant_id` `"*"`. A tenant-scoped administrator receives HTTP 403 and the node record does not change. Drain is `state=draining` on this route. There is no node-delete route.
- `POST /api/v1/infrastructure/nodes/{node_id}/heartbeat`:
  - a global administrator (role `admin` and `tenant_id` `"*"`) may operate any node;
  - a tenant-scoped administrator is refused with HTTP 403 and the heartbeat does not change node state;
  - service role must have claim `node_id == {node_id}`;
  - mismatch or missing claim -> 403.
- Heartbeat cannot mutate trusted endpoints or capacity.

Additional controls:
- Never log token or Authorization header.
- Bounded HTTP timeouts; no unbounded waits.
- No shell execution, no `shell=True`, no arbitrary command launch.
- Node-agent config accepted from env/secret only.

---

## 7) Failure behavior, retries, idempotency

Heartbeat semantics:
- At-least-once delivery; latest write wins in DB.
- Duplicate heartbeats are safe.

Retry policy:
- Network error/timeout/5xx -> exponential backoff with jitter, capped by `HEARTBEAT_BACKOFF_MAX_SECONDS`.
- 404 on heartbeat -> report registration-required and keep bounded retry; a global administrator (role `admin` and `tenant_id` `"*"`) must create the node record.
- 401/403 -> keep process alive, continue bounded retries, emit auth-failure metric/log.
- Never exit solely due to control-api failure.

Edge behavior:
- If MediaMTX probe fails, send `mediamtx_reachable=0`, keep last-known load values or safe zeros for probe-only fields; do not crash.
- Agent must not set node `enabled`; that remains control-plane/admin-owned.

---

## 8) Data model/migrations

- **No new tables required.**
- Reuse existing `infrastructure_nodes.capacity_json` and `load_json`.
- Additive JSON keys only; no migration for Step 1C-A.
- Keep existing operational fields (`cameras.media_node_id`, `recording_policies.recording_node_id`) unchanged.

---

## 9) Observability

Step 1C-A emits structured logs for heartbeat failures/auth failures/registration-required state and redacts token values.

Prometheus counters/gauges/histograms for node-agent heartbeat outcomes are a Phase 9 observability task; this step does not claim they already exist.

---

## 10) Compose integration (single-node fallback preserved)

- Add `services/node-agent/` Dockerfile and runtime.
- Add compose service under a profile (e.g. `profiles: ["regional-node-agent"]`) so default `docker compose up` behavior is unchanged.
- Profile-on example binds:
  - `NODE_ID=media-local-01`
  - `REGION_ID=${PLACEMENT_DEFAULT_REGION}`
  - role `media` (or `media,recording` for local combined node)
  - `CONTROL_API_URL=http://control-api:8000`
  - token from env only.

When profile is off, current single-node behavior remains intact.

---

## 11) Backward compatibility

- Existing endpoints and schemas remain valid.
- Existing clients without node_id claim are unaffected except for hardened node registration/heartbeat route access (expected).
- Placement controller and current reconciliation loops unchanged.

---

## 12) Test matrix and acceptance criteria

Unit tests (node-agent):
1. Config validation (bad URL, invalid role list, missing MediaMTX URL for media/recording) rejects startup.
2. CPU/RAM/disk normalization clamps invalid values.
3. Network byte-delta -> rate math is correct and non-negative.
4. MediaMTX unreachable path sets reachable flag and does not crash.
5. Backoff progression and cap behavior.
6. Heartbeat payload schema/keys (measured load + telemetry only; no endpoints/capacity).
7. Log redaction: token never present.

Control-api tests:
8. Service token with matching `node_id` can heartbeat its own node.
9. Service token cannot register/change trusted node endpoints.
10. Service token with mismatched/missing `node_id` is rejected (403).
11. A global administrator token (role `admin` and `tenant_id` `"*"`) can register/configure/drain any node. A tenant-scoped administrator token is refused with HTTP 403 and no state change.

Integration tests:
12. Node missing -> heartbeat 404 -> registration-required state + bounded retry, with no unauthorized PUT.
13. Control-api unreachable -> retries/backoff, process remains alive.
14. Compose config valid with node-agent profile; default compose path unchanged.

Acceptance gate:
- Existing tests remain green.
- New Step1C tests green.
- `python -m compileall` passes for new service.
- Node-agent image builds.
- No secrets committed; no tokens printed.

---

## 13) Non-goals (explicit)

- No placement strategy rewrite.
- No camera reassignment logic changes.
- No execution-plane migration (Step 1B dependency).
- No hard-coded 100K/channel hardware claims.
