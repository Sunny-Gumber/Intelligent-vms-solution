# Administrator Guide

## First run

The initial administrator password is written to
`<data_root>/first-run-credentials.txt` with restrictive permissions. It is
never printed to the console or written to a log.

1. Sign in, change the password immediately.
2. Delete the credentials file (`shred -u` on Linux).
3. Enable MFA for every administrator account.
4. Create named accounts for each operator. **Do not share the admin login** —
   the audit trail is only useful if actions map to people.

## Adding cameras

**Discovery** finds ONVIF cameras on the local network. Discovery ranges are
capped deliberately: the product refuses to scan a large network, because a
VMS must not behave like a port scanner on a customer site.

**Manual onboarding** needs the address, credentials and protocol. For plain
RTSP cameras, supply the exact stream path from the camera's documentation.

Validation runs on save and tells you precisely what happened:

| Result | Meaning |
| --- | --- |
| `online` | Reachable, authenticated, at least one playable stream |
| `unauthorized` | The device rejected the credentials |
| `error` with a detail | Reachable but something specific failed |
| `provisioning` | Saved but not yet validated |

### Capabilities are proven, not assumed

Capability flags are only set true when the device actually demonstrated the
capability. If `ptz_continuous` is false, the PTZ API returns `501` rather
than pretending. **Re-validate a camera after a firmware upgrade** so newly
available capabilities are detected.

## Storage

Register volumes before cameras need them. A volume whose path is not writable
by the service account is **rejected at registration** rather than discovered
at 3 a.m. when recording stops.

Set `min_free_percent` per volume. When a volume crosses it:

1. State becomes `degraded`, then `full`
2. An alarm is raised
3. New recordings go to the next usable volume
4. Emergency rotation deletes the oldest footage first and records that it did

RAID status is shown for **software RAID only** (Linux `md`, Windows Storage
Spaces). Hardware RAID controllers report `unknown` — the product does not
imply health it cannot verify.

## Users and permissions

Three built-in roles — Viewer, Operator, Administrator — plus custom roles
built from 26 discrete permissions.

**Scope matters as much as role.** Assign sites or specific cameras. A user
with no scope and no administrator role sees **nothing**: the system fails
closed by design.

Deactivating a user takes effect **immediately**, not when their token
expires.

## Recording schedules

Schedules are evaluated in the **site's local time**, so a block ending at
18:00 still ends at 18:00 after the clocks change. Timestamps are stored and
transmitted in UTC.

An enabled schedule with no blocks means "always record", which matches what
operators expect from a schedule they have enabled but not yet drawn.

## Alarm rules

Rules are data, not code. Match on event type, camera, site and a restricted
condition language, then act: raise an alarm, start recording, capture a
snapshot, notify a channel.

Use `suppress_seconds` generously. One flapping camera can generate thousands
of events per hour and make the alarm list useless.

Four rules ship enabled: camera offline, recording failed, storage critical,
camera credentials rejected.

## Privacy-sensitive features

Facial recognition and ANPR are **disabled by default** and require, together:

1. Enabling in configuration
2. A licence that includes the feature
3. A dedicated per-user permission, separate from playback
4. A written justification on **every** search

Every such search is recorded in a separate privacy access log that the
ordinary retention job does not purge. Biometric and plate data has its own,
shorter retention period.

Confirm your legal basis before enabling these. In many jurisdictions this is
a regulated activity requiring an impact assessment.

## Monitoring the system

| Endpoint | Use |
| --- | --- |
| `/healthz` | Liveness — is the process up |
| `/readyz` | Readiness — can it serve traffic (503 if the database or storage is unusable) |
| `/api/v1/system/health` | Per-component detail |
| `/api/v1/system/metrics` | Prometheus scrape |
| `/api/v1/recordings/health/summary` | Which cameras are actually recording |

Alert on: `recording_failed` count above zero, storage below threshold, and
`/readyz` returning 503.

## Backups

Read `docs/operations/disaster-recovery.md` before you need it. The headline:
**a database backup alone cannot restore the system.** The master key at
`<data_root>/keys/vms-master.key` encrypts every camera password and lives
outside the database. Back it up separately and store it apart from the
database dump.

## Support bundles

```bash
python tools/diag_bundle.py --output /tmp/vms-diag.zip
```

Collects configuration (secrets redacted), recent logs, health, storage
diagnostics and version information. Inspect it before sending it anywhere.
