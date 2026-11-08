# Architecture overview

## Process topology

```
                      ┌──────────────────────┐
   Browsers  ────────►│   Web client (React) │
                      └───────────┬──────────┘
   Operators ────────►┌───────────▼──────────┐
                      │ Windows client (WPF) │
                      └───────────┬──────────┘
                                  │ HTTPS + WebSocket
                      ┌───────────▼──────────────────────────┐
                      │   control-api  (Python / FastAPI)    │
                      │                                      │
                      │  identity · devices · scheduling     │
                      │  events · storage · administration   │
                      └──┬────────────┬──────────────┬───────┘
                         │            │              │
              SQL        │            │ control      │ desired state
                         ▼            ▼              ▼
                 ┌───────────┐  ┌──────────┐  ┌──────────────────┐
                 │PostgreSQL │  │ MediaMTX │  │recording-service │
                 └───────────┘  └────┬─────┘  └────────┬─────────┘
                                     │  pull           │ pull (FFmpeg)
                                     ▼                 ▼
                              ┌──────────────────────────────┐
                              │          Cameras             │
                              └──────────────────────────────┘
                                                       │
                                                       ▼
                                             ┌──────────────────┐
                                             │ Storage volumes  │
                                             └──────────────────┘
```

The critical property: **MediaMTX and the recording service pull from cameras
independently.** If the gateway dies, live view fails with a clear 503 and
recording is untouched. If the control API restarts, in-flight recordings keep
writing and startup recovery reconciles the index.

## Request lifecycle

```
HTTP request
  → correlation id bound (returned as X-Correlation-Id)
  → authentication   (JWT with revocation check, or API key)
  → rate limit       (token bucket per principal)
  → authorization    (capability AND scope, fail-closed)
  → handler          (sync for DB work, async for I/O)
  → audit record     (for every state change)
  → Problem document on failure (stable `code`, never raw text)
```

## Module boundaries inside control-api

```
app/
  core/         crypto, netguard, paths, backoff, circuit, rbac, audit, deps
  db/           models, session, portable column types
  devices/      adapters (ONVIF, RTSP), discovery, probe, monitor, credentials
  media/        gateway client, session broker, stream policy, URL builder
  recording/    index, planner, gaps, retention, recovery, health, triggers
  playback/     resolver
  exports/      jobs, ffmpeg, preflight, evidence, cleanup
  ptz/          arbiter, service
  events_engine/ ingest, rules, alarms, dedupe, escalation
  analytics/    ingest, zones, counting, linking, privacy, anpr
  storage/      volumes, allocator, monitor, diagnostics, raid
  api/v1/       routers (thin; all logic lives in the modules above)
```

Import direction is one-way: `api` → domain modules → `core` → `db`. Routers
contain no business logic, which is what keeps the domain testable without
HTTP.

## Data flow: a frame from camera to evidence

1. **Recorder** pulls RTSP with FFmpeg's `segment` muxer into a spool
   directory, writing a CSV line when each segment closes.
2. **Allocator** picks a storage volume (with failover) and builds a dated,
   path-confined destination.
3. **Index** opens a row in state `writing` *before* the file moves.
4. The file is moved with an **atomic rename**; only then does the row become
   `finalized`. A reader never sees a partial file at a final path.
5. **Playback** resolves a time range to real files, verifying each exists, and
   reports the ACTUAL covered range plus any gaps.
6. **Export** stream-copies the range, hashes it with SHA-256, and records a
   chain of custody. Missing source footage fails the export rather than
   silently producing a shorter clip.

## Failure handling summary

| Failure | Behaviour |
| --- | --- |
| Camera drops mid-recording | Bounded-backoff restart; the gap is recorded and reported |
| Camera stops sending frames but holds the socket | Stall watchdog kills and restarts FFmpeg |
| Server killed mid-segment | Startup recovery finalizes or marks corrupt; never leaves `writing` |
| Storage volume full | Emergency rotation + critical event; failover to another volume |
| No writable volume | `recording.failed` event — never a silent stop |
| Media gateway down | Live view 503; recording and playback unaffected |
| Database restarts | `pool_pre_ping` reconnects; startup waits with bounded retries |
| Client disappears mid-PTZ | Lock expires; watchdog stops the camera; device timeout as backstop |
| Two operators acknowledge one alarm | Version check; the loser gets 409, not a silent overwrite |
| Camera deleted while recording | Tasks stopped, sessions closed, PTZ released — **recordings preserved** |
