# Changelog

All notable changes are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0-alpha] — 2026-10-20

First greenfield source drop.

> **This is an ALPHA engineering build. It is not production-ready and has not
> been validated against physical camera hardware.** See
> `docs/LIMITATIONS.md` and `docs/requirements/feature-matrix.md`.

### Added — verified by automated test

- Recording pipeline: FFmpeg segment recorder producing real, playable MP4
  files by stream copy, with atomic finalization and index consistency.
- Crash recovery: segments left mid-write are finalized or marked corrupt and
  can never remain in a `writing` state.
- Retention: age and free-space policies, oldest-first deletion, tombstone
  rows preserved for reporting, dry-run support.
- Export with evidence: SHA-256 hashing, append-only chain of custody, and a
  hard refusal to produce a clip when source footage is missing.
- Playback: manifests, timeline density, gap detection, calendar availability,
  and precise errors for missing media.
- Security: Argon2id passwords with a production cost floor, JWT access tokens
  with server-side revocation, refresh rotation with reuse detection, TOTP MFA
  with replay rejection, account lockout, AES-256-GCM credential vault with
  per-camera AAD, SSRF guard with DNS-rebinding pinning, path confinement.
- Authorization: 26 permissions with per-site and per-camera scope that fails
  closed; API keys cannot exceed their owner's permissions.
- Alarms: a strict state machine with optimistic concurrency, deduplication,
  suppression and escalation.
- PTZ: control arbitration with priority preemption, server-side movement
  clamping, and a dead-man watchdog.
- Storage: multiple volumes, health state machine, failover, software-RAID
  awareness, diagnostics.
- Analytics: ingestion with mandatory provenance, zone rules (intrusion,
  tripwire, loitering), counting, footage linking, and privacy controls for
  regulated capabilities.
- Licensing: Ed25519-signed offline licence files with tamper detection and no
  phone-home.
- 157 automated tests; the schema is built by the real Alembic migration on
  every run.

### Added — delivered as source, not verified

- React/TypeScript web client (never built).
- .NET 8 WPF Windows client (never compiled).
- Windows installer, systemd units, nginx configuration (never executed).

### Known gaps

Server-side inference models, facial recognition matching, server-side ANPR
OCR, pre-event recording, LDAP, distributed recording, N+1 failover, HA,
rolling upgrades, object left behind/removed. Each returns
`501 not_implemented` or is rejected at startup rather than failing silently.

Full status: `docs/requirements/feature-matrix.md` — 301 requirements,
201 implemented, 87 partial, 12 designed-only, 1 not implemented.
