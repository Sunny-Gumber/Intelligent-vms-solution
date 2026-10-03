# Node fencing operations

## Enable

Fencing remains off by default.

For a distributed regional node:

```env
NODE_FENCING_ENABLED=true
FENCE_POLL_INTERVAL_SECONDS=5
FENCE_EXPIRY_GRACE_SECONDS=2
FENCE_CLOCK_SKEW_WARN_SECONDS=5
FENCE_STATE_PATH=/var/lib/vms-node/fence-state.json
```

Use persistent storage for `/var/lib/vms-node`.

## Preconditions

1. Node is admin-registered.
2. Trusted endpoints/capacity are configured by admin.
3. Node service token contains matching `node_id`.
4. Placement execution is enabled only after nodes/regions are ready.
5. Node fence-state volume is writable.

## Expected logs

- `fence_clock_skew`: node clock differs materially from control server.
- `fence_snapshot_failed`: node could not refresh ownership state.
- `fence_execution_stopped`: expired/revoked work was removed.
- `fence_revoked`: durable revocation deleted and acknowledged.
- `fence_revoke_failed`: stale path could not be removed; revocation stays pending.

## Control loss

Step 1C-B behavior is intentionally conservative:
- cached lease remains valid until expiry;
- after expiry, cached media/recording execution is fenced locally even if control-api remains unreachable.

Do not increase lease duration to hide WAN problems. Step 1C-C introduces explicit regional autonomy.

## Clock policy

Fence snapshot includes server time.

Node stores last observed server/local offset and uses it when evaluating cached leases.

Large offset produces a warning. Operators should still keep NTP/PTP healthy.

## Recovery

When control returns:
1. placement renews/reassigns leases;
2. node receives current assignment snapshot;
3. central reconciler re-applies any generation not marked applied;
4. newer active generation clears stale local tombstones.

## Failover verification

For a recording camera:
1. capture current node/generation;
2. stop node A heartbeat;
3. allow placement to assign B;
4. verify generation increments;
5. reconnect A;
6. verify A cannot accept old generation and its path is removed;
7. verify B recording hook carries the new generation;
8. fail back to A and verify the path is re-applied with the newest generation.

## Safety warning

Do not delete `fence-state.json` as a routine troubleshooting step. It contains local stale-owner tombstones needed to defend against reassertion after control-plane ACK.
