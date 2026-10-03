# Media path reconciliation

Camera records are desired state. MediaMTX runtime paths are replaceable runtime state.

## Behavior

A bounded background loop:
1. obtains a single-writer advisory lock on PostgreSQL;
2. lists current MediaMTX runtime paths;
3. reads a bounded batch of enabled cameras;
4. reconstructs the view source from encrypted camera credentials and stored sub/main path;
5. recreates missing paths;
6. records run/change/failure counts.

It intentionally does **not** delete unknown MediaMTX paths in this version.

## Configuration

- `MEDIA_RECONCILE_ENABLED`
- `MEDIA_RECONCILE_INTERVAL_SECONDS`
- `MEDIA_RECONCILE_BATCH_SIZE`
- `MEDIA_RECONCILE_MAX_CHANGES_PER_RUN`

Defaults are conservative to reduce reconnect storms.

## Multi-replica behavior

PostgreSQL transaction-scoped advisory locking elects one reconciler for a run. SQLite/dev assumes one process.

At regional 100K+ scale, this evolves into regional placement/reconciliation services with per-media-node queues. One central API process is not expected to reconcile 100K streams.

## Failure behavior

If MediaMTX is unavailable:
- camera DB state is not changed;
- the run is logged as failed;
- a later bounded interval retries.

Unknown runtime paths are not removed automatically, preventing accidental teardown of manually managed/diagnostic streams.
