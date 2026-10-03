# ADR — Native Windows small-site server field-test profile

**Status:** Accepted for issue #303 field-test implementation.  
**Product state:** Release Candidate / External Qualification Pending.

## Context

The enterprise VMS architecture includes PostgreSQL, Redpanda/Kafka, ClickHouse,
MediaMTX, control API, workers and distributed placement. Forcing that complete Linux-
oriented topology through Docker Desktop or WSL2 would not be a credible commercial
Windows server direction.

The field-test core operator loop is narrower:

Camera -> Live -> MAIN/SUB/THIRD -> continuous MAIN recording -> retention ->
timeline/playback -> snapshot -> manual recording intent -> MP4 export.

With distributed placement disabled, this loop already uses MediaMTX local playback
rather than the ClickHouse recording index. Kafka/ClickHouse are therefore not required
for this local operator loop.

## Decision

Create an explicit **windows-small-site** deployment profile using the same product
logic and security/media contracts.

| Component | Windows field-test strategy | Rationale |
|---|---|---|
| Control API/business/security | Native Python 3.12 x64 isolated venv, Windows SCM service | Same source and PostgreSQL models; no forked backend. |
| Browser UI | Served by FastAPI StaticFiles from the same installed web assets | Removes nginx from small-site Windows only; API routes remain authoritative. |
| PostgreSQL | Existing/supported native PostgreSQL x64 Windows service, automatically configured by VMS installer | Keeps PostgreSQL authoritative; no SQLite shortcut. Final commercial installer must bundle/install an approved PostgreSQL runtime. |
| MediaMTX | Official v1.21.1 Windows amd64 binary, SHA-256 pinned, Windows SCM service | Same live/recording/playback engine and API contract. |
| Recording completion hook | Cross-platform Python helper invoked by MediaMTX | Avoids POSIX curl/$VAR assumptions and keeps hook secrets in protected config. |
| Redpanda/Kafka | Not started in windows-small-site | Enterprise/distributed event transport remains unchanged and available in its existing profile. |
| ClickHouse | Not started in windows-small-site | Event history/distributed finalized-segment index remains enterprise/distributed; local playback uses MediaMTX. |
| event-writer / alarm-worker | Not started in windows-small-site | They require the event pipeline. UI capabilities explicitly mark these unavailable. |
| ONVIF event worker | Not required for core small-site loop | Camera add/live/recording do not depend on event ingestion. Existing APIs remain shared. |
| placement/regional node/spool | Disabled | windows-small-site is one local media/recording node; enterprise placement architecture is not removed. |
| Diagnostics/backup | Native PowerShell + PostgreSQL tools | No Bash requirement. |
| Service wrapper | pywin32 SCM host in the isolated VMS runtime | No Docker/WSL or third-party wrapper binary required. |
| Recording storage | Administrator-selected absolute local Windows volume; internal MediaMTX template uses forward-slash Windows form | Supports drive selection without naive slash replacement. UNC/network recording storage is rejected until qualified. |

## Profile capability boundary

The windows-small-site profile explicitly sets:

- event pipeline: unavailable;
- ClickHouse event history: unavailable;
- alarm processing: unavailable;
- distributed placement: unavailable;
- AI compact UI: unavailable for this field profile;
- recording-metadata Kafka fan-out: disabled.

Recording completion still updates PostgreSQL recording-health state. Disabling metadata
fan-out prevents an unbounded undeliverable transactional outbox when Kafka is absent.

The enterprise-distributed profile remains unchanged and retains Kafka, ClickHouse,
event/alarm workers, regional placement, fencing and distributed metadata.

## Service model

The SCM dependency order is:

native PostgreSQL -> IntelligentVMSControl -> IntelligentVMSMedia.

Both VMS services are Automatic (delayed) and receive bounded SCM recovery actions.
The control service hosts Uvicorn/FastAPI and static browser assets. The media service
supervises the official MediaMTX executable. No logged-in desktop user is required.

## Security

Runtime configuration and secrets live under
`%ProgramData%\IntelligentVMS\config` with SYSTEM/Administrators-only ACLs.
Service commands contain only non-secret paths. Field-test HS256 login remains
loopback-only and production OIDC fail-closed rules are unchanged.

## Networking

The field profile binds control/API, RTSP, MediaMTX API, metrics, playback, HLS and
WebRTC signaling to loopback. It therefore creates no inbound Windows Firewall rule.
Camera RTSP/ONVIF access is outbound according to the configured site CIDRs.

Remote browser/LAN field access is intentionally not claimed until a TLS/reverse-proxy
or equivalent secure installer design is qualified. Windows Server can be field-tested
through its local/RDP browser.

## Consequences

Advantages:
- genuine native Windows path;
- no Docker Desktop or WSL2;
- shared product code/database/media architecture;
- lower small-site footprint;
- clear path to future Setup.exe/MSI packaging.

Limitations:
- field-test prerequisite is an installed native PostgreSQL x64 service and Python 3.12
  runtime; final commercial setup must bundle/manage approved runtimes;
- Windows 11 x64 and Windows 10 x64 have no matching GitHub-hosted x64 desktop runner
  and remain external qualification;
- hosted CI validates Windows Server 2022 and 2025, not physical reboot, cameras,
  codecs, storage durability or capacity;
- event history/alarms/distributed functions require the enterprise profile.
