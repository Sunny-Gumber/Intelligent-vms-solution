# Phase 7 Step 1C-B — Generation / Lease Fencing

## Goal

Prevent stale media, recording or future AI owners from continuing/reasserting work after placement ownership moves.

## Authority

`placement_assignments` is the ownership source of truth.

Each assignment has:
- node_id
- generation
- applied_generation
- lease_expires_at

Generation changes only when ownership changes to another node.

## Durable revocation

When ownership moves A -> B:

1. create `placement_revocations` for A and the revoked generation;
2. increment assignment generation;
3. clear `applied_generation`;
4. set B as current owner;
5. retain legacy cleanup-node state until cleanup/ACK.

Revocations are indexed by node ID so node agents do not scan all assignments.

Failback cancels obsolete revocations for the newly current node.

## Control-plane execution serialization

Generation/lease state alone is not enough if an old reconciler request is already
in flight while placement changes ownership.

In PostgreSQL production mode, all operations that can change assignment ownership
or start/reconfigure/delete distributed execution share one transaction-scoped
advisory lock:

- placement ownership/generation mutation;
- distributed media/recording reconciliation;
- immediate recording policy start/stop;
- distributed camera deletion.

Therefore an A/gen1 MediaMTX mutation cannot remain in flight while placement commits
A/gen1 -> B/gen2. If placement owns the fence first, mutation paths fail/skip and
retry after loading the new generation. If a mutation owns the fence first, placement
waits for the next controller run and cannot advance generation until that mutation
transaction finishes.

This serialization closes the cross-controller stale-reassertion race. The node-agent
revocation tombstone and lease watchdog remain the second line of defense against
stale node-local processes.

SQLite does not provide this distributed advisory-lock guarantee and remains a
single-process development/test mode, not the HA production ownership boundary.

## Node fence snapshot

Node-scoped endpoint:

`GET /api/v1/infrastructure/nodes/{node_id}/fences`

Returns:
- server_time;
- active assignments owned by the node;
- pending durable revocations for that node;
- execution key/path;
- generation and lease expiry.

Snapshot size is bounded by `NODE_FENCE_SNAPSHOT_MAX_ITEMS`; overflow fails closed instead of silently truncating ownership state.

## Node local fence state

When enabled, node-agent persists:
- last known server clock offset;
- active assignment generation and lease;
- execution key;
- fenced/revoked state.

Default state file:
`/var/lib/vms-node/fence-state.json`

State is written atomically.

## Lease expiry

Node uses control-plane server time offset rather than assuming identical wall clocks.

When a cached active lease expires:
- media/recording path is deleted locally;
- fence state remains and deletion is re-enforced on later cycles;
- control-plane reconnect/lease renewal does not automatically start work; the central reconciler must re-apply desired state.

This milestone intentionally fences on lease expiry even if central control is unavailable. Step 1C-C adds controlled regional offline autonomy without weakening stale-owner safety.

## Revocation tombstone

After a revocation path is deleted and ACKed, the node retains a local tombstone.

Why:
- a stale local process could re-add a path after cleanup;
- the tombstone causes the node-agent to delete that execution key again every fence cycle.

A newer active assignment for the same camera/role overwrites the tombstone and permits failback.

## Applied generation

Path existence is insufficient.

Reconciliation marks `applied_generation = generation` only after the current generation has been applied to the current node.

If:
`applied_generation != generation`

the reconciler re-applies the MediaMTX configuration even if the path already exists.

This is essential for failback where an old path may still carry an old recording-hook generation.

## Recording evidence fence

Distributed recording hooks include:
- recording_node_id
- assignment_generation

The control API accepts completed segments only when:
- recording assignment is active;
- node ID equals current owner;
- generation equals current generation;
- lease is still valid.

A stale recorder can therefore neither be indexed nor accepted after failover even if it survives briefly.

## Controller concurrency

Placement mutation continues to use the PostgreSQL advisory transaction lock.

Multiple control replicas can read fence state, but one placement transaction at a time changes assignment ownership/generation.

## AI

The current repository contains AI orchestration but no node-local AI execution worker.

Step 1C-B persists AI fence generations/leases. There is no AI process to stop today. Any future AI worker must consume the same local fence state before starting/continuing a job.

## Failure examples

### A -> B
A gen1 -> revocation(A,1) -> B gen2 -> applied_generation null -> reconcile B -> applied_generation 2.

### Stale A reconnect
A receives durable revoke/tombstone and removes old path. Recording callbacks carrying gen1 are rejected.

### B lease expires during control loss
B locally removes media/record path after lease + configured grace.

### B -> A failback
Pending old revoke for A is cancelled; assignment becomes A with a new generation; applied_generation clears; A path is re-applied before being considered ready.

## Non-goals

- offline regional lease extension (Step 1C-C);
- guaranteed event outbox (later Phase 7);
- real hardware scale certification (Phase 8).
