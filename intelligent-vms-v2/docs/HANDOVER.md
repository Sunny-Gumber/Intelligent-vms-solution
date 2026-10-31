# Intelligent VMS — Handover Report

**Version:** 0.1.0-alpha  
**Date:** 2026-10-20  
**Repository owner:** Sunny-Gumber  
**Production ready:** **No.** See [Section I](#i-limitations) and the release gates.

---

## A. Product summary

Intelligent VMS is a greenfield enterprise Video Management System. It manages
IP cameras, records them continuously or on a schedule, serves playback and
evidentiary export, controls PTZ, processes events and alarms, manages storage,
and integrates analytics — on Windows Server, Ubuntu Server or Docker, with no
cloud dependency and no mandatory internet connection.

**What it actually does today, demonstrated by automated tests:**

- Onboards cameras by IP or ONVIF discovery, stores their credentials encrypted
  with AES-256-GCM, and refuses to talk to loopback, link-local or
  cloud-metadata addresses.
- Records real video with FFmpeg using stream copy (no transcoding), segments
  it, finalizes each segment atomically, and indexes it so the index and the
  disk never disagree.
- Recovers from a crash without ever leaving a segment in a `writing` state.
- Serves playback manifests, timeline density and gap reports.
- Exports evidence clips, hashes them with SHA-256 and records an append-only
  chain of custody — and refuses to export at all rather than silently produce
  a shorter clip when source footage is missing.
- Enforces authorization server-side on every surface, with per-camera and
  per-site scoping that fails closed.
- Arbitrates PTZ control between operators and runs a dead-man watchdog that
  stops a camera whose operator disconnected.
- Deduplicates events, drives an alarm state machine with optimistic
  concurrency so two operators cannot overwrite each other, and escalates
  unacknowledged alarms.
- Applies retention and emergency storage rotation, leaving tombstones so
  deletions remain auditable.

**What it does not do yet** is stated plainly in Section I. Nothing in this
report claims hardware validation that did not happen.

---

## B. Architecture

### Processes

Three OS processes, not a microservice sprawl. The boundaries exist where
failure isolation genuinely matters (ADR-0001, ADR-0013):

| Process | Responsibility | Why separate |
| --- | --- | --- |
| **control-api** | Identity, devices, events, storage, playback/export orchestration, administration, WebSocket | The system of record |
| **recording-service** | Supervised FFmpeg recorders, one per camera stream | A recorder crash must never take down the API, and API latency must never stall recording |
| **analytics-service** | Frame sampling and inference | AI is the most resource-hungry and least mature component; it must never be able to interrupt recording (M9-R23) |

Supporting components: **PostgreSQL** (system of record), **MediaMTX** (media
gateway), **FFmpeg** (invoked as a separate process, never linked — ADR-0006).

### Request and media paths

```
Browser / WPF client
      │ HTTPS (REST + WebSocket)
      ▼
┌──────────────┐    SQL     ┌────────────┐
│ control-api  │◄──────────►│ PostgreSQL │
└──────┬───────┘            └────────────┘
       │ HTTP control plane (path + token)
       ▼
┌──────────────┐    RTSP (with credentials)   ┌────────┐
│  MediaMTX    │◄─────────────────────────────│ Camera │
└──────┬───────┘                              └────┬───┘
       │ WebRTC / HLS (token-authenticated)        │ RTSP
       ▼                                           ▼
   Client video                            ┌───────────────┐
                                           │ recorder +    │
                                           │ analytics     │
                                           └───────┬───────┘
                                                   ▼
                                            Storage volumes
```

**The critical property:** the camera's RTSP URL and password never leave the
server. Clients receive a gateway path plus a short-lived play token. This is
asserted by `test_camera_crud_and_credential_protection`.

### Key design decisions

Fourteen ADRs in `docs/decisions/`. The consequential ones:

| ADR | Decision | Rationale |
| --- | --- | --- |
| 0005 | Synchronous SQLAlchemy in a threadpool | Control-plane request rates are low; mixing async ORM with blocking subprocess calls was judged the larger risk |
| 0006 | Delegate media transport to MediaMTX | Writing an RTSP/WebRTC stack is a multi-year project with a large attack surface |
| 0007 | Hand-written ONVIF SOAP client | A WSDL stack retrieves schema imports at runtime on some platforms; an offline appliance cannot accept a hidden network dependency |
| 0008 | AES-256-GCM with AAD bound to camera id | A ciphertext copied to another camera row fails authentication instead of silently decrypting |
| 0012 | Analytics always record their source | Receiving ONVIF events from a camera must never be presentable as server-side AI |
| 0014 | Export refuses on incomplete source | Evidence that silently omits footage is worse than no evidence |

---

## C. Feature implementation status

Full detail: **`docs/requirements/feature-matrix.md`** (generated from
`docs/requirements/requirements.yaml`; CI fails if they diverge).

| Status | Count | Share |
| --- | ---: | ---: |
| Implemented | 201 | 66.8% |
| Partial | 87 | 28.9% |
| Designed only | 12 | 4.0% |
| Not implemented | 1 | 0.3% |
| **Total tracked requirements** | **301** | |

| Verification | Count |
| --- | ---: |
| Automated test | 220 |
| Manual / documented | 14 |
| **Not independently verified** | **67** |

**"Implemented" means** the code exists, is integrated, is reachable from a
caller and its configuration is documented. **It never means validated against
physical camera hardware** — no ONVIF or RTSP camera was available.

### Per-module summary

| Module | Impl | Partial | Designed | Not impl |
| --- | ---: | ---: | ---: | ---: |
| M1 Platform and System Architecture | 10 | 7 | 0 | 0 |
| M2 Camera Discovery and Onboarding | 17 | 5 | 0 | 0 |
| M3 Live Video Monitoring | 9 | 8 | 1 | 0 |
| M4 Video Recording Management | 17 | 1 | 0 | 0 |
| M5 Playback Management | 9 | 3 | 1 | 0 |
| M6 Video Export and Evidence Management | 15 | 0 | 0 | 0 |
| M7 PTZ Control | 9 | 4 | 0 | 0 |
| M8 Events, Alarms and Rule Engine | 16 | 4 | 0 | 0 |
| M9 AI Video Analytics | 12 | 7 | 5 | 1 |
| M10 Storage Management | 11 | 5 | 1 | 0 |
| M11 User, Role and Security Management | 14 | 1 | 1 | 0 |
| M12 Multi-Site Enterprise Management | 7 | 3 | 1 | 0 |
| M13 Windows Desktop Application | 0 | 13 | 0 | 0 |
| M14 Web Application | 0 | 10 | 0 | 0 |
| M15 Dashboards and Reporting | 9 | 0 | 0 | 0 |
| M16 Network and Bandwidth Management | 8 | 2 | 0 | 0 |
| M17 Installation, Configuration and Operations | 5 | 8 | 0 | 0 |
| M18 Enterprise Reliability | 14 | 1 | 2 | 0 |
| M19 Third-Party Integrations | 5 | 5 | 0 | 0 |
| M20 Commercial Product Readiness | 14 | 0 | 0 | 0 |

**Why M13/M14 are entirely "Partial":** complete client source is delivered,
but the build environment had no .NET SDK and no npm registry access, so
neither client was compiled. Claiming otherwise would be dishonest.

---

## D. Repository contents

```
intelligent-vms/
├── packages/shared-contracts/   Single source of truth for enums and DTOs
├── services/
│   ├── control-api/             FastAPI control plane (~100 modules)
│   │   └── app/
│   │       ├── core/            security, RBAC, crypto, netguard, paths,
│   │       │                    backoff, circuit breaker, audit, MFA
│   │       ├── db/              ORM models (38 tables), session management
│   │       ├── devices/         ONVIF + RTSP adapters, discovery, probing
│   │       ├── media/           session broker, stream policy, URL safety
│   │       ├── recording/       index, scheduling, gaps, retention, recovery
│   │       ├── playback/        manifest and media resolution
│   │       ├── exports/         job pipeline, preflight, evidence, cleanup
│   │       ├── ptz/             arbitration and command execution
│   │       ├── events_engine/   ingestion, rules, alarms, escalation
│   │       ├── analytics/       ingestion, zones, counting, ANPR, privacy
│   │       ├── storage/         volumes, allocator, RAID, diagnostics
│   │       └── api/v1/          14 routers, 88 endpoints
│   ├── recording-service/       FFmpeg segment recorder and supervisor
│   └── analytics-service/       Frame sampler, tracker, ONNX detector
├── apps/
│   ├── web-client/              React 18 + TypeScript + Vite
│   └── windows-client/          .NET 8 WPF (MVVM, LibVLCSharp)
├── database/                    Alembic configuration and migration 0001
├── deploy/{docker,ubuntu,windows}/
├── installers/{windows,ubuntu}/ WiX v4 MSI and .deb packaging
├── tools/                       SBOM, diagnostics, backup, migration, matrix
├── scripts/                     db backup, upgrade, OpenAPI export
├── tests/                       157 automated tests
├── contracts/openapi.json       88 paths, 75 schemas (generated)
└── docs/                        Architecture, 14 ADRs, deployment, security,
                                 operations, user guides, verification
```

---

## E. Installation

### Docker (fastest evaluation)

```bash
cd deploy/docker
cp .env.example .env
#️ Set VMS_SECRET_KEY to a unique value of at least 32 characters.
docker compose up -d
docker compose exec control-api alembic -c /app/database/alembic.ini upgrade head
docker compose logs control-api | grep first-run
```

### Ubuntu Server 22.04 / 24.04

```bash
sudo apt install -y python3-venv ffmpeg postgresql postgresql-client
sudo ./deploy/ubuntu/install.sh
sudo nano /etc/intelligent-vms/vms.toml      # secret_key + database_url
cd /opt/intelligent-vms/database
sudo -u intelligent-vms /opt/intelligent-vms/runtime/bin/alembic -c alembic.ini upgrade head
sudo systemctl enable --now intelligent-vms-api intelligent-vms-recorder
sudo cat /var/lib/intelligent-vms/first-run-credentials.txt   # then delete it
```

### Windows Server 2022 / 2025

```powershell
.\deploy\windows\install.ps1 -DataRoot 'C:\ProgramData\IntelligentVMS'
notepad C:\ProgramData\IntelligentVMS\config\vms.toml
Start-Service IntelligentVmsApi, IntelligentVmsRecorder
```

Full guides: `docs/deployment/`. Ports: `docs/deployment/firewall.md`.

> **Neither installer has been executed.** No Windows or Ubuntu target was
> available. Treat first installation as a bring-up exercise.

---

## F. Dependencies

Generated SBOM: `release/sbom/intelligent-vms-sbom.cdx.json` (CycloneDX 1.5,
35 components). Obligations: `docs/operations/third-party-licenses.md`.

| Component | Licence | Distribution obligation |
| --- | --- | --- |
| FFmpeg | LGPL-2.1+ **or** GPL-2.0+ **depending on build flags** | Invoked as a separate process, never linked (ADR-0006). **A GPL build imposes GPL obligations on your whole distribution.** Confirm H.264/H.265 patent-pool obligations separately. |
| MediaMTX | MIT | Attribution |
| PostgreSQL | PostgreSQL Licence | Attribution |
| FastAPI / Starlette | MIT / BSD-3 | Attribution |
| SQLAlchemy, Alembic | MIT | Attribution |
| cryptography | Apache-2.0 / BSD | Attribution |
| React | MIT | Attribution |
| LibVLCSharp | LGPL-2.1+ | Dynamic linking; allow user replacement of the library |

**Action before commercial distribution:** replace the placeholder `LICENSE`,
confirm the FFmpeg build you ship, and take legal advice on codec patents.

---

## G. Hardware requirements

All figures below are **model output, not measurements.** The capacity model
(`app/capacity/model.py`) computes them from codec, resolution, frame rate and
retention. `POST /api/v1/system/capacity` returns them with an explicit
disclaimer attached.

| Deployment | Cameras | Ingest (est.) | Storage @30d (est.) | vCPU (est.) | RAM (est.) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Small | 16 | ~22 Mbps | ~7 TB | 6 | 8 GB |
| Medium | 32 | ~45 Mbps | ~14 TB | 8 | 12 GB |
| Large | 64 | ~90 Mbps | ~29 TB | 10 | 16 GB |
| Enterprise | 128 | ~180 Mbps | ~58 TB | 14 | 24 GB |
| Multi-site | 256 | ~360 Mbps | ~116 TB | 22 | 40 GB |

Assumptions: 1080p, 15 fps, H.265, medium scene complexity, continuous
recording, 25% of cameras running server-side analytics.

**These must be validated on your hardware before any commercial commitment.**
Storage sizing is the one figure that is reliably accurate, because it is
arithmetic on bitrate; CPU and RAM depend heavily on actual camera behaviour.

---

## H. Security

| Control | Implementation | Verified by |
| --- | --- | --- |
| Password storage | Argon2id, t=3, m=64 MiB, p=2 | `test_password_hash_and_verify` |
| Session tokens | 15-min JWT with `jti`; **revocation is immediate** via denylist | `test_logout_revokes_access_token` |
| Refresh tokens | Opaque, hashed at rest, rotated; **reuse revokes the whole family** | `test_refresh_rotation_and_reuse_detection` |
| Brute force | Windowed lockout, database-backed (global) | `test_account_lockout_after_repeated_failures` |
| MFA | RFC 6238 TOTP with counter-replay protection | `test_mfa_enrolment_and_login` |
| Authorization | Server-side RBAC, per-site and per-camera, **fails closed** | `test_viewer_without_site_scope_sees_no_cameras` |
| Camera credentials | AES-256-GCM, AAD bound to camera id, master key outside the DB | `test_credential_vault_rejects_wrong_aad` |
| Credential exposure | No password or RTSP URL ever reaches a client | `test_camera_crud_and_credential_protection` |
| SSRF | Deny-CIDR, port allow-list, **DNS pinning** | `test_netguard_dns_rebinding_pin` |
| Scanner abuse | Discovery range hard-capped and audited | `test_discovery_range_is_capped` |
| Path traversal | All user-derived paths confined | `test_resolve_within_blocks_traversal` |
| Evidence integrity | SHA-256 + append-only custody chain | `test_export_produces_a_real_clip_with_verifiable_evidence` |
| Log hygiene | Automatic credential redaction, including inside RTSP URLs | `test_diagnostic_bundle_contains_no_secrets` |
| Support bundles | Secrets provably excluded | same test |
| Privacy (FR/ANPR) | Feature gate + licence + permission + mandatory justification + separate audit log + shorter retention | `test_privacy_retention_strips_biometrics_but_keeps_the_event` |

Threat model: `docs/security/threat-model.md`. Reporting: `SECURITY.md`.

---

## I. Limitations

The honest list. Full detail in `docs/LIMITATIONS.md`.

### Blocking for production

1. **No camera hardware validation.** Every ONVIF and RTSP code path is
   unit-tested against fixtures and exercised with synthetic media, but no
   physical camera was ever contacted. This is the single largest risk.
2. **Clients are not compiled.** No .NET SDK, no npm registry.
3. **Installers never executed.** No Windows or Ubuntu target.
4. **No soak test.** The longest continuous recording run was ~8 seconds.
5. **No performance benchmark.** Every capacity figure is model output.

### Functional gaps (deliberate, and the API says so)

| Gap | Behaviour |
| --- | --- |
| Pre-event recording | `VMS_PRE_EVENT_SECONDS > 0` returns `501`; needs a ring buffer |
| Server-side AI models | Infrastructure complete, **no model ships**; `available=false` with a reason |
| Face matching engine | Privacy controls implemented; matching **not** implemented |
| Server-side ANPR OCR | Plate ingestion/search implemented; **no OCR** |
| Object left behind / removed | Not implemented |
| High availability, N+1, distributed recording | Designed only; endpoints return `501` |
| LDAP / Active Directory | Interface only; production start refuses it |
| OIDC | Implemented, **never tested against a real IdP** |
| Rate limiting | Per-process; multi-worker needs a shared backend |
| Hardware RAID | Software RAID only; controllers report `unknown` |
| Frame stepping, hardware decode | Designed, not implemented |

### Honest uncertainty

- Low-rate IoU tracking can split identities on fast objects, inflating counts.
- SMTP and webhook delivery are implemented but never sent to a live endpoint.
- The segment-list watcher is polled at 1 Hz; a sub-second-granularity
  requirement would need a different mechanism.

---

## J. Git handover

The new implementation is **isolated** from
`Sunny-Gumber/Intelligent-vms-solution`. Nothing was pushed, merged, forced or
deleted anywhere.

### Option 1 — a new repository (recommended)

```bash
unzip intelligent-vms-0.1.0-alpha.zip && cd intelligent-vms
git init && git add . && git commit -m "feat: Intelligent VMS 0.1.0-alpha greenfield implementation"
git remote add origin git@github.com:Sunny-Gumber/intelligent-vms.git
git push -u origin main
```

### Option 2 — an isolated branch in the existing repository

```bash
cd Intelligent-vms-solution
git checkout --orphan next-generation      # no shared history with main
git rm -rf . && cp -r /path/to/intelligent-vms/. .
git add . && git commit -m "feat: next-generation greenfield implementation"
git push -u origin next-generation         # never force, never touch main
```

`--orphan` is deliberate: it guarantees the new tree cannot be mistaken for an
evolution of the existing product or accidentally fast-forwarded onto it.

### Before the first push

```bash
git check-ignore -v var/ data/ recordings/ '*.key' license.json   # all ignored
grep -rn "dev-insecure-change-me" --include='*.toml' .            # example config only
python tools/generate_feature_matrix.py --check                   # matrix current
python -m pytest -q                                               # 157 passing
```

The `.gitignore` already excludes recordings, exports, databases, logs, `.env`,
`*.key`, `vms-master.key`, `license.json` and `first-run-credentials.txt`.

### CI

`.github/workflows/ci.yml` runs lint, the full test suite, migration
verification, the feature-matrix freshness gate and SBOM generation.
`release.yml` builds artifacts on a tag.

---

## K. Verification evidence

| Artifact | Location |
| --- | --- |
| Test run output | `docs/verification/test-run.txt` (157 passed) |
| Feature matrix | `docs/requirements/feature-matrix.md` |
| Traceability source | `docs/requirements/requirements.yaml` (301 requirements) |
| API contract | `contracts/openapi.json` (88 paths) |
| SBOM | `release/sbom/intelligent-vms-sbom.cdx.json` |
| External qualification plan | `docs/verification/external-qualification-plan.md` |

### What the tests genuinely prove

The recording and export tests are **not** mocked. They invoke real FFmpeg,
write real MP4 files to a real storage volume, read them back with `ffprobe`,
and independently recompute the SHA-256 of an exported clip to compare against
the stored evidence record. The camera is simulated by replaying a locally
encoded H.264 file in real time — the only part that is not real is the
network transport from a physical device.

Every test also runs against a schema produced by the **shipped Alembic
migration**, not `metadata.create_all`, so the migration itself is exercised
on every run.

### Release gates still outstanding

1. ONVIF/RTSP compatibility across at least 10 camera models from 3 vendors
2. 72-hour continuous recording soak at target camera count
3. Performance benchmark on representative hardware
4. Independent security assessment / penetration test
5. Successful installer execution on clean Windows and Ubuntu targets
6. Disaster-recovery exercise with a verified restore
7. Client builds and UI acceptance testing
8. Legal review of FFmpeg build flags and codec patent obligations

`app.version.PRODUCTION_READY` stays `False` until these pass. A test asserts
this, so the flag cannot be flipped casually.

---

## L. Recommended next steps

**Week 1 — prove it runs.** Build both clients. Execute the Ubuntu installer on
a clean VM. Connect one real camera and work through onboarding, live view,
recording, playback and export end to end. Expect ONVIF surprises; that is the
point.

**Weeks 2-4 — prove it lasts.** Expand to 8-16 mixed-vendor cameras. Run the
72-hour soak. Measure actual CPU, RAM and bandwidth and compare against the
capacity model, then correct the model's constants with real values.

**Month 2 — close the gaps that matter to your buyers.** Likely order:
pre-event recording (operators ask for it immediately), a bundled detection
model, then LDAP/AD if you sell into enterprises.

**Before any sale.** Independent security assessment, legal review of codec
licensing, and the DR exercise.

---

*Prepared as the handover for Intelligent VMS 0.1.0-alpha. Every status in this
document is traceable to `docs/requirements/requirements.yaml` and to the test
evidence in `docs/verification/`.*
