# Known Limitations

Everything this software does not do, or does not yet prove. Read this before
any commercial commitment, pilot or customer demonstration.

Status language matches `docs/requirements/feature-matrix.md`.

---

## 1. Nothing here has touched real camera hardware

The build environment had **no camera, no NVR, no NAS, no Windows host and no
identity provider**. The following are therefore implemented-but-unverified:

| Area | What is verified | What is not |
| --- | --- | --- |
| ONVIF | SOAP envelope construction, WS-Security password digest, clock-skew compensation, response parsing (unit tested against captured payloads) | Any real device interaction: `GetDeviceInformation`, `GetProfiles`, `GetStreamUri`, PTZ, PullPoint events |
| WS-Discovery | Probe message format and `ProbeMatch` parsing | Multicast behaviour on a real LAN; vendor quirks |
| RTSP pull | URL construction, credential encoding, transport selection | Pulling from an actual camera, including vendor authentication quirks |
| Recording | Real MP4 segments produced from a looped H.264 file, indexed, finalized and verified playable | Long-duration (24/7) stability, H.265 cameras, audio-bearing streams, behaviour under packet loss |
| PTZ | Arbitration, clamping and watchdog logic (unit tested with a fake adapter) | Actual camera movement, vendor velocity-space differences |

**Consequence:** camera compatibility claims cannot be made. The compatibility
matrix in `docs/verification/external-qualification-plan.md` is empty by
design and must be filled in from real testing.

---

## 2. AI analytics: architecture without models

**Implemented:**
- Ingestion of camera-side analytics (ONVIF topics mapped to normalized classes)
- Ingestion from external engines via `POST /api/v1/analytics/detections`
- Zone geometry rules — intrusion, tripwire with direction, loitering with
  dwell, region exit — all unit tested
- Counting and business-intelligence aggregation over stored detections
- Linking detections to recorded footage, and a privacy-gated search path

**Not implemented:**
- **No detection model ships with this product and none is downloaded.** The
  server-side inference worker and detector interface exist; you must supply
  an ONNX model and set `VMS_ANALYTICS_MODEL_DIR`.
- **Facial recognition matching is not implemented.** The privacy controls
  around it are (feature gate, dedicated permission, mandatory justification,
  separate audit log, shorter retention) — the matching engine is not.
- **Server-side ANPR OCR is not implemented.** Plate reads from ANPR-capable
  cameras are ingested, normalized, watchlist-matched and searchable.
- **Object left behind / object removed** is not implemented at all.
- **Pixel-based smart search** is not implemented; search is metadata-based.

Every analytics record carries `source` (`camera_edge`, `server_inference`,
`external`) and the API never blurs the distinction.

---

## 3. Pre-event recording is not implemented

Post-event recording works. Pre-event recording requires a continuously
running ring buffer per camera, which changes the recorder's memory and disk
model.

`VMS_PRE_EVENT_SECONDS` greater than 0 raises
`501 pre_event_not_implemented` rather than silently recording nothing. No
operator should believe they have pre-event footage when they do not.

---

## 4. Enterprise scale-out is designed, not built

| Capability | Status |
| --- | --- |
| Node registration, health and inventory aggregation | Implemented (single-node tested only) |
| Distributed recording across nodes | **Designed only** — API returns `501` |
| Cross-node N+1 recording failover | **Designed only** — API returns `501` |
| High availability / distributed control plane | **Designed only** |
| Rolling upgrades | **Designed only** — needs a multi-server runtime |
| Live video proxying between nodes | **Not implemented** |

---

## 5. Identity integration

- **LDAP / Active Directory: not implemented.** `app/identity/ldap.py` is an
  interface whose methods raise. Production start-up is refused when
  `VMS_LDAP_ENABLED=true`.
- **OIDC: implemented but unproven.** Discovery, PKCE, token exchange and ID
  token validation (JWKS signature, issuer, audience, nonce, expiry) are all
  written. It has **never been run against a real identity provider**.

---

## 6. Client builds pass; runtime qualification remains pending

GitHub Actions run [39363143375](https://github.com/Sunny-Gumber/Intelligent-vms-solution/actions/runs/39363143375)
verified commit `c889a5e8e35d48b4d45a0bb334ff020b915015bf` on 2026-10-20:
web typecheck/build and Windows .NET 8 restore/build passed. The Windows build
reported zero warnings and zero errors. See draft PR #177 for later commit checks.

- **Windows WPF client**: missing startup, model and login wiring was supplied
  during CI repair. The alpha shell exposes login, camera inventory, grid layout
  and live-session assignment. It does not implement the originally sketched
  playback, alarm, notification, multi-monitor or persistent-preference workspaces.
  LibVLC uses the HLS gateway endpoint; decoding, token renewal, reconnect and
  session teardown still require live runtime qualification. Hardware-accelerated
  decoding remains unqualified.
- **Web client**: compile failures in page imports, tile properties, authentication
  guard and API calls were repaired, and an npm lockfile was added. No browser
  end-to-end or real-media qualification has been performed. The playback page
  displays metadata/timeline availability; it is not a verified video player.

Build success establishes compilation only. It is not evidence that UI bindings,
authentication against a live server or camera playback work in an installed system.

---

## 7. Installers and deployment scripts are unexecuted

The Windows PowerShell installer, WinSW service definitions, systemd units,
logrotate configuration and upgrade/rollback scripts are all written but have
**not been run on a target operating system**. The Docker Compose stack is the
only deployment path that was exercised during development.

Native installers now provision and preflight the required loopback media-auth
settings; existing installations must add these settings before upgrading to
this build. Static checks do not qualify a clean native install or live gateway.

---

## 8. Scale and performance figures are modelled, not measured

`POST /api/v1/system/capacity` returns an **analytical estimate** from an
explicit arithmetic model, and the response carries a disclaimer field saying
so.

The 16/32/64/128/256-camera figures in the documentation are **capacity
planning targets, not tested performance**. No load test, soak test or
benchmark of this software has been run. Validate on your target hardware
before committing to a deployment size.

---

## 8a. Live-video admission and revocation (ADR-0015)

Implemented in Batch 1 against baseline `83e965d9`; **not yet verified on a
real gateway**.

| Area | Status |
| --- | --- |
| Gateway calls the control API for every read and Control-API request | Configured (`authMethod: http`, `authHTTPExclude: []`). **Not verified** on MediaMTX v1.21.2 — `tests/gateway/` has not run |
| Hook decision (session, path, user, camera, RBAC/scope, URL-credential policy, caller authentication) | Implemented; tested against the real database, broker and RBAC with the gateway replaced by a test double |
| Revocation of established WebRTC/RTSP viewers | Implemented as a fair, batch-limited supervised scan (5 s interval) with concurrency-limited, timed kicks plus immediate kick on session close. Scan and kick latency grow with backlog; per-viewer kick by gateway session id is **not verified** on the pinned release |
| Revocation of HLS viewers | **Not provided.** HLS has no per-viewer gateway session. Whether MediaMTX re-authenticates each HLS request is unverified; if it does not, a revoked HLS viewer keeps watching until they leave |
| Hook caller authentication via `user:secret@` in `MTX_AUTHHTTPADDRESS` | Relies on the gateway sending URL userinfo as Basic auth. **Unverified.** If it does not, the hook denies everything (fail closed, live video unavailable) |
| Plain RTSP | Play token travels in cleartext; restrict port 8554 to trusted networks or use RTSPS |
| Gateway image | MediaMTX 1.21.2 pinned to an immutable registry digest; binary-level runtime qualification is still required |
| Control API unavailable | No new viewer can connect (by design). Recording is unaffected |
| Windows client (LibVLC/HLS) | Must send credentials as HTTP Basic (`session_id:play_token`); query-string tokens are now refused. Not yet updated or tested |

## 9. Operational caveats to know before a pilot

| Area | Caveat |
| --- | --- |
| **Client IP behind proxies** | `VMS_FORWARDED_ALLOW_IPS` (default `127.0.0.1`) controls which peers may set `X-Forwarded-For` for uvicorn. Separately, `client_ip()` in `app/core/deps.py` still reads `X-Forwarded-For` directly for audit and rate-limit keys, so those values are client-controlled until fixed (tracked, not in Batch 1). |
| **Rate limiting** | In-process token buckets. With N API worker processes the effective limit is N x the configured rate. Login lockout is database-backed and therefore global; request rate limiting is not. A shared backend is required for exact global limits. |
| **RAID monitoring** | Linux `md` and Windows Storage Spaces only. **Hardware RAID controllers (MegaRAID, PERC, SmartArray) are not queried** and report `unknown`, because their vendor CLIs cannot be redistributed. |
| **NAS / SMB / NFS** | Supported as a mounted path that is monitored. Mounting, credentials and reconnection are the operating system's responsibility. Not validated against a real share. |
| **SMTP** | Implemented, never sent to a live mail server. |
| **WebSocket stream** | Token-authenticated and scope-filtered. Not load tested; slow clients have their messages dropped rather than back-pressuring the event pipeline. |
| **Database** | PostgreSQL is the only supported production backend. SQLite is for development and the test suite, and production start-up refuses it. |
| **Master key** | Camera credentials are encrypted with a key at `<data_root>/keys/vms-master.key`. **Lose that file and every stored camera password becomes unrecoverable.** Back it up separately from the database — see `docs/operations/disaster-recovery.md`. |
| **Time** | All storage and transport is UTC. Site-local time is a presentation concern. Recording schedules are evaluated in site-local time so DST is handled correctly. |
| **MFA replay** | A TOTP code used to complete enrolment cannot be reused to sign in; the accepted counter is recorded. Users must wait for the next 30-second window. This is deliberate. |

---

## 10. Not performed at all

- Long-duration soak testing (24h+ continuous recording)
- Load testing at any camera count
- Real-camera compatibility certification
- Penetration testing or an external security audit
- Disaster-recovery exercise
- Failover testing (the capability is not implemented)
- Accessibility (WCAG) assessment of either client
- Localisation beyond English

These are listed as release gates in
`docs/verification/release-gates.md`. `PRODUCTION_READY` in
`app/version.py` stays `False` until they pass.
