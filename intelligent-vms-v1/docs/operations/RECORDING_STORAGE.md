# Recording storage lifecycle

## Phase 3 hot tier

MediaMTX writes per-recording-path fMP4 segments to the local `/recordings` volume. Retention is path-specific through `recordDeleteAfter`.

This is intentionally simple and safe for the first recorder milestone.

## Future warm/archive pipeline

A completed-segment event becomes the unit of storage movement:

```text
completed local segment
        |
        v
checksum -> upload -> remote verify -> mark object available
        |
        +--> keep local until hot retention expires
```

Deletion rules:
- never accept a filesystem delete path directly from a user API;
- storage workers operate only on DB/index-owned segment IDs;
- remote object verification precedes local early-delete;
- legal hold overrides retention;
- retries are idempotent;
- failed upload never makes the local segment appear archived.

## Capacity reserve

Recording nodes must maintain an operational free-space reserve. Later health logic will:
1. warn;
2. stop new camera assignments to the node;
3. escalate;
4. optionally shed lowest-priority recording only under an explicit policy.

The VMS will not silently broad-delete recordings just to recover space.

## Object storage

S3-compatible storage is a warm/archive target, not the central RTSP ingest point. Regional recording protects WAN failure and prevents all 100K video streams from depending on one cloud path.


## Backup and DR boundary

Database backups do not protect fMP4 recording payloads. Protect regional hot
recording volumes with the selected storage platform's snapshot/replication mechanism,
and test restore on that storage. Warm/archive objects require checksum verification,
appropriate immutability/lifecycle rules, and independent provider-level recovery
validation.

See `PHASE9_BACKUP_RESTORE_DR.md`.
