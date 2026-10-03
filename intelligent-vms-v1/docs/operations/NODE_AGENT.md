# Node Agent (Phase 7 Step 1C-A)

`services/node-agent/` runs on each infrastructure node and reports node heartbeat telemetry to control-api.

## Scope

- Sends periodic heartbeat telemetry only:
  - `POST /api/v1/infrastructure/nodes/{NODE_ID}/heartbeat`
- Trusted node registration, region/roles, and MediaMTX/public/playback endpoints remain **admin-managed** through:
  - `PUT /api/v1/infrastructure/nodes/{NODE_ID}`
- If heartbeat returns 404, the agent reports that registration is required and continues bounded retries; it does not self-register with a node-scoped service token.
- Does **not** mutate camera placement or recording policy.
- Does **not** stop media/recording workloads when control-api is unreachable.

## Required environment

- `NODE_ID`
- `REGION_ID`
- `NODE_ROLES` (`media`, `recording`, `ai`, comma-separated)
- `CONTROL_API_URL` (absolute `http/https`, no query/fragment/embedded credentials)
- `NODE_AGENT_TOKEN` (service token; never commit in git)

Capacity is **not** supplied by the node agent. Administrators register safe capacity limits on the infrastructure-node record.

If `NODE_ROLES` contains `media` or `recording`, `MEDIAMTX_API_URL` is required.

## Optional environment

- `NODE_NAME` (default `NODE_ID`)
- `HEARTBEAT_INTERVAL_SECONDS` (default `10`, min `2`)
- `HEARTBEAT_TIMEOUT_SECONDS` (default `3`)
- `HEARTBEAT_BACKOFF_INITIAL_SECONDS` (default `1`)
- `HEARTBEAT_BACKOFF_MAX_SECONDS` (default `60`)
- `HEARTBEAT_BACKOFF_JITTER_RATIO` (default `0.2`)
- `RECORDING_MOUNT_PATH` (default `/recordings`)
- `NETWORK_INTERFACE` (optional specific NIC)

## Measured vs configured values

Configured-safe capacity is admin-managed in the control plane. Measured runtime load is sampled from node telemetry (CPU/RAM/disk/net/uptime and MediaMTX probe). The agent does not hardcode camera-per-server claims and cannot raise its own capacity.

For recording nodes, this step reliably reports active recorder-path count but does not yet invent a recording Mbps value. If the control plane has a positive `max_record_mbps` limit without a trustworthy `record_mbps` metric, placement treats the node as ineligible. AI placement likewise remains ineligible until the AI runtime/scheduler reports `ai_mpix_s` and `active_ai_jobs`.

Hardware limit certification remains Phase 8 work.

## Compose profile

The service is profile-gated to preserve default single-node behavior:

```bash
docker compose --profile regional-node-agent up --build
```

Default `docker compose up` remains unchanged (node-agent disabled).


## Registration order

Before starting a regional node agent:

1. Admin registers the node and trusted endpoint URLs in control-api.
2. Issue a node-scoped service token containing the matching `node_id`.
3. Start the node agent.
4. The first operation is a heartbeat; no trusted endpoint data is sent by the agent.
5. If the node record is missing, the agent logs `node_registration_required` and retries with bounded backoff.

This keeps trusted routing endpoints under administrative control and prevents a compromised node-service token from redirecting the control plane.


## Operational state ownership

`active`, `draining`, and `maintenance` are administrator-controlled infrastructure states.

The node agent heartbeat cannot change them. This prevents a node-scoped service token from putting itself back into placement after an operator drains or quarantines the node.
