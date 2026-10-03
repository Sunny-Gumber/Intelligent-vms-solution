# Regional media execution

Phase 7 distributed execution is feature-gated.

## Safe activation order

1. Apply Alembic migrations through `0008`.
2. Register every media/recording node through `PUT /api/v1/infrastructure/nodes/{node_id}`.
3. Confirm current node heartbeats and capacity/load data.
4. Map sites to regions.
5. Run/observe placement until cameras have current assignments and leases.
6. Verify each node exposes required management endpoints.
7. Set `PLACEMENT_EXECUTION_ENABLED=true`.
8. Observe reconciliation and camera `desired_state`: `pending-placement -> assigned -> provisioned`.
9. Verify live view, recording, timeline and playback before widening rollout.

Do not enable distributed execution on a region with no eligible nodes.

## Node endpoint contract

Media nodes use:

- `api_url`: MediaMTX control API reachable from the VMS control/reconciler.
- `webrtc_public_base`: browser-reachable WebRTC base.
- `hls_public_base`: browser-reachable HLS base.
- `metrics_url`: optional metrics endpoint for later regional diagnostics.

Recording nodes additionally use:

- `playback_internal_url`: MediaMTX playback API reachable from the control API.

Endpoint URLs must be HTTP/HTTPS, contain no embedded credentials, query string or fragment.

## Example combined node

```json
{
  "name": "Noida Media 01",
  "region_id": "in-north-01",
  "roles": ["media", "recording"],
  "state": "active",
  "enabled": true,
  "endpoints": {
    "api_url": "http://10.20.0.11:9997",
    "webrtc_public_base": "https://media01.example.internal/webrtc",
    "hls_public_base": "https://media01.example.internal/hls",
    "playback_internal_url": "http://10.20.0.11:9996"
  },
  "capacity": {
    "max_ingress_mbps": 8000,
    "max_egress_mbps": 8000,
    "max_sources": 4000,
    "max_record_mbps": 0,
    "max_recordings": 4000
  }
}
```

The numeric values above are examples of API shape, not hardware claims. Phase 8 supplies measured capacity coefficients.

Until a trustworthy node-side `record_mbps` measurement is qualified, keep `max_record_mbps` at `0` so placement uses the measured `active_recordings/max_recordings` dimension. A positive `max_record_mbps` without a measured `record_mbps` intentionally makes that recording node ineligible.

## Failover safety

Assignment ownership includes a generation and lease.

When placement changes nodes:

1. the old node is appended to the assignment cleanup list;
2. the reconciler provisions/confirms the path on the new node;
3. only after new-node readiness does it remove the old path;
4. failed cleanup remains durable and is retried;
5. repeated failovers retain all cleanup obligations;
6. failback to a former node removes that now-current node from the cleanup list.

This ordering is especially important for recording to prevent duplicate long-running recorders.

## Camera onboarding

When distributed execution is disabled, camera onboarding keeps the existing immediate local MediaMTX behavior.

When enabled, onboarding commits the camera as `pending-placement`. The placement controller chooses a node, then reconciliation provisions it. The API does not advertise a live stream URL until the assigned node is provisioned.

## Recording

Enabling a policy:

- applies immediately if a valid recording assignment already exists;
- otherwise stores desired state and lets placement/reconciliation apply it.

Disabling an active recording policy must successfully remove the current recording path before the policy is committed disabled.

Each recording hook reports the actual recording node ID. `RECORDING_HOOK_CALLBACK_URL` must be reachable from every recording node.

## Playback

Timeline and playback requests resolve the current `recording_node_id` and use that node's `playback_internal_url`; they no longer assume one global playback server.

## Deletion

Distributed camera deletion first removes all current and stale media/recording paths. If an assigned node is unavailable, deletion fails safely instead of deleting database ownership and leaving an orphan recorder that could return later.

## Remaining Phase 7 work

- regional node heartbeat agent/metrics collector;
- lease/fencing at node-side workers, not only central assignment state;
- transactional event outbox;
- regional autonomous operation during WAN/control-plane loss;
- chaos/load qualification in Phase 8.
