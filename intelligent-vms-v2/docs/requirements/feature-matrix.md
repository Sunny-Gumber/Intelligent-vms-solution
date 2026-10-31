# Feature Implementation Matrix

> **This file is generated.** Edit `docs/requirements/requirements.yaml` and run `python tools/generate_feature_matrix.py`.

- Product: **Intelligent VMS 0.1.0-alpha**
- Statuses reconciled: **2026-10-20**
- Production ready: **NO**
- Test evidence: `docs/verification/test-run.txt`

## Summary

| Status | Count | Share |
| --- | ---: | ---: |
| Implemented | 201 | 66.8% |
| Partial | 87 | 28.9% |
| Designed only | 12 | 4.0% |
| Not implemented | 1 | 0.3% |
| **Total** | **301** | 100% |

| Verification | Count | Share |
| --- | ---: | ---: |
| Automated test | 220 | 73.1% |
| Manual / documented | 14 | 4.7% |
| Not independently verified | 67 | 22.3% |

### How to read this

- **Implemented** means the code exists, is integrated, is reachable from a caller and its configuration is documented.
- **Implemented never means "validated against physical camera hardware".** No ONVIF or RTSP camera was available during development. Device compatibility is an external qualification activity (`docs/verification/external-qualification-plan.md`).
- **Partial** means a meaningful subset works and the specific gap is stated in the evidence column.
- **Designed only** means a written design exists and no working implementation does. The API returns `501 not_implemented` for these.

## Per-module status

| Module | Implemented | Partial | Designed | Not implemented |
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

## Full matrix

### M1 - Platform and System Architecture

| ID | Requirement | Status | Verification | Component | Evidence / gap |
| --- | --- | --- | --- | --- | --- |
| M1-R01 | Control server | Implemented | Automated test | `services/control-api` | tests/integration/test_api_flows.py (app boots, 88 OpenAPI paths served) |
| M1-R02 | Media streaming service | Partial | Automated test | `app/media/mediamtx_client.py` | control plane implemented; transport delegated to MediaMTX (ADR-0006) |
| M1-R03 | Recording service | Implemented | Automated test | `services/recording-service` | tests/integration/test_recording_pipeline.py::test_recorder_produces_playable_segments_and_matching_index |
| M1-R04 | Playback and export service | Implemented | Automated test | `app/playback, app/exports` | tests/integration/test_recording_pipeline.py::test_export_produces_a_real_clip_with_verifiable_evidence |
| M1-R05 | Camera discovery and device management service | Partial | Automated test | `app/devices` | adapters and WS-Discovery implemented; no hardware validation |
| M1-R06 | ONVIF integration service | Partial | Automated test | `app/devices/onvif_adapter.py` | SOAP digest auth and message parsing unit tested; device calls unverified |
| M1-R07 | Event and alarm processing service | Implemented | Automated test | `app/events_engine` | tests/unit/test_domain_logic.py alarm and dedupe tests |
| M1-R08 | Authentication and authorization service | Implemented | Automated test | `app/core/security.py, app/core/rbac.py` | tests/unit/test_core_primitives.py, tests/integration/test_api_flows.py authz tests |
| M1-R09 | Database and configuration service | Implemented | Automated test | `app/db, database/migrations` | every test runs against the real Alembic migration |
| M1-R10 | Storage management service | Implemented | Automated test | `app/storage` | tests/unit/test_domain_logic.py storage tests |
| M1-R11 | Health monitoring service | Implemented | Automated test | `app/health` | tests/integration/test_api_flows.py health endpoints |
| M1-R12 | AI analytics service / deployable analytics worker | Partial | Automated test | `app/analytics, services/analytics-service` | ingestion, zones and worker interface implemented; NO inference models bundled |
| M1-R13 | Web management application | Partial | **Not independently verified** | `apps/web-client` | TypeScript source delivered; npm build not executed in this environment |
| M1-R14 | Windows desktop monitoring client | Partial | **Not independently verified** | `apps/windows-client` | WPF/MVVM source delivered; no .NET SDK in the build environment |
| M1-R15 | Installer, upgrade and maintenance components | Partial | **Not independently verified** | `installers, deploy` | scripts delivered; not executed on a target OS |
| M1-R16 | Stable internal contracts between components | Implemented | Automated test | `packages/shared-contracts` | vms_contracts imported by every service; 25 enums, 31 DTOs |
| M1-R17 | Startup dependencies, health checks, graceful shutdown, recovery | Implemented | Automated test | `app/lifecycle.py, app/core/supervisor.py` | tests/unit/test_core_primitives.py supervisor and backoff tests |

### M2 - Camera Discovery and Onboarding

| ID | Requirement | Status | Verification | Component | Evidence / gap |
| --- | --- | --- | --- | --- | --- |
| M2-R01 | ONVIF-compatible camera discovery (WS-Discovery) | Partial | **Not independently verified** | `app/devices/discovery.py` | UDP multicast probe and ProbeMatch parsing implemented; requires a LAN device to validate |
| M2-R02 | Manual IP address onboarding | Implemented | Automated test | `app/api/v1/cameras.py` | tests/integration/test_api_flows.py::test_camera_crud_and_credential_protection |
| M2-R03 | RTSP and RTSPS stream configuration | Implemented | Automated test | `app/media/url_builder.py` | tests/unit/test_core_primitives.py URL tests |
| M2-R04 | Camera username/password authentication | Implemented | Automated test | `app/devices/credentials.py` | tests/unit/test_core_primitives.py credential vault tests |
| M2-R05 | Secure credential storage (encrypted at rest) | Implemented | Automated test | `app/core/crypto.py` | AES-256-GCM with per-camera AAD; tamper and wrong-key tests pass |
| M2-R06 | Camera model and manufacturer identification | Partial | **Not independently verified** | `app/devices/onvif_adapter.py` | GetDeviceInformation wired; hardware-dependent |
| M2-R07 | Camera capability discovery | Partial | Automated test | `app/devices/base.py` | capability model unit tested; device probing hardware-dependent |
| M2-R08 | Profile and media stream enumeration | Partial | Automated test | `app/devices/onvif_adapter.py` | classification logic tested; GetProfiles hardware-dependent |
| M2-R09 | Device time synchronization support | Partial | **Not independently verified** | `app/devices/onvif_adapter.py` | SetSystemDateAndTime wired; hardware-dependent |
| M2-R10 | Main, sub and third stream configuration | Implemented | Automated test | `app/db/models.py CameraStream` | tests/integration/test_api_flows.py |
| M2-R11 | Camera connection validation | Implemented | Automated test | `app/devices/probe.py, app/devices/rtsp_adapter.py` | ffprobe-based measurement; validation endpoint tested |
| M2-R12 | Camera reconnect and recovery | Implemented | Automated test | `services/recording-service/recorder/supervisor.py` | bounded backoff restart loop; tests/unit/test_core_primitives.py backoff tests |
| M2-R13 | Camera online/offline detection | Implemented | Automated test | `app/devices/monitor.py` | circuit-breaker monitor; state transition emits exactly one event |
| M2-R14 | Camera import/export with credential protection | Implemented | Automated test | `app/api/v1/cameras.py` | tests/integration/test_api_flows.py::test_csv_export_never_contains_passwords |
| M2-R15 | Bulk camera onboarding | Implemented | Automated test | `app/api/v1/cameras.py` | tests/integration/test_api_flows.py::test_bulk_import_reports_failures |
| M2-R16 | Camera groups and site assignment | Implemented | Automated test | `app/api/v1/users.py sites` | tests/integration/test_api_flows.py site tests |
| M2-R17 | Duplicate camera detection | Implemented | Automated test | `app/devices/dedupe.py` | tests/unit/test_domain_logic.py::test_duplicate_detection_by_serial_and_host |
| M2-R18 | Camera deletion and safe cleanup | Implemented | Automated test | `app/services/camera_lifecycle.py` | tests/integration/test_api_flows.py::test_camera_deletion_preserves_recordings |
| M2-R19 | Camera replacement workflow | Implemented | Automated test | `app/services/camera_lifecycle.py` | replace endpoint implemented; probe failure path tested |
| M2-R20 | ONVIF Profile S/T/G/M capability gating (no blanket assumption) | Implemented | Automated test | `app/devices/base.py` | tests/unit/test_domain_logic.py::test_capabilities_default_to_unproven |
| M2-R21 | Plain RTSP cameras supported without ONVIF | Implemented | Automated test | `app/devices/rtsp_adapter.py` | adapter registry resolves rtsp protocol; ffprobe measurement path |
| M2-R22 | Vendor-neutral abstraction with isolated vendor adapters | Implemented | Automated test | `app/devices/registry.py` | registry test; no core module imports a vendor adapter |

### M3 - Live Video Monitoring

| ID | Requirement | Status | Verification | Component | Evidence / gap |
| --- | --- | --- | --- | --- | --- |
| M3-R01 | Single-camera live view | Partial | Automated test | `app/api/v1/streams.py, apps/*` | server session API verified; client rendering unverified |
| M3-R02 | 2x2 / 3x3 / 4x4 and larger configurable layouts | Partial | **Not independently verified** | `apps/web-client, apps/windows-client` | layout engine source delivered, not built |
| M3-R03 | Custom layouts and saved views (per-user persistence) | Implemented | Automated test | `app/api/v1/system.py layouts` | layout CRUD endpoints tested |
| M3-R04 | Drag-and-drop camera assignment | Partial | **Not independently verified** | `apps/web-client` | source delivered, not built |
| M3-R05 | Fullscreen and multi-monitor operation | Partial | **Not independently verified** | `apps/windows-client` | WPF window manager source delivered, not built |
| M3-R06 | Independent video tile lifecycle (one failure must not freeze the UI) | Partial | Automated test | `app/media/session_broker.py, apps/*` | per-session server isolation verified; client behaviour unverified |
| M3-R07 | Camera name, status and error overlays | Partial | **Not independently verified** | `apps/*` | source delivered, not built |
| M3-R08 | Per-tile stream selection (main/sub/third) with manual override | Implemented | Automated test | `app/media/stream_policy.py` | tests/unit/test_domain_logic.py stream policy tests |
| M3-R09 | Automatic stream selection by tile size and resources | Implemented | Automated test | `app/media/stream_policy.py` | tests/unit/test_domain_logic.py::test_stream_policy_picks_sub_for_small_tiles |
| M3-R10 | Reconnect without disturbing unrelated tiles | Implemented | Automated test | `app/media/session_broker.py` | sessions are independent rows with independent gateway paths |
| M3-R11 | Snapshot capture | Implemented | Automated test | `app/services/snapshots.py` | SHA-256 recorded per snapshot; endpoint tested |
| M3-R12 | Instant/manual recording when authorized | Implemented | Automated test | `app/api/v1/recordings.py` | tests/integration/test_api_flows.py manual recording tests |
| M3-R13 | Quick access to playback from live | Implemented | Automated test | `app/api/v1/playback.py` | playback latest/manifest endpoints tested |
| M3-R14 | PTZ control from the selected tile | Partial | Automated test | `app/api/v1/ptz.py` | server API verified; device execution hardware-dependent |
| M3-R15 | Camera groups and favourites | Implemented | Automated test | `app/api/v1/users.py sites` | site hierarchy tested |
| M3-R16 | Hardware decoding with CPU fallback | **Designed only** | **Not independently verified** | `apps/windows-client` | ADR-0009; D3D11/DXVA path NOT implemented in alpha |
| M3-R17 | Bounded live-session allocation, cleanup and authorization | Implemented | Automated test | `app/media/session_broker.py` | global and per-user caps, TTL expiry, reaper loop |
| M3-R18 | Low-latency live playback | Partial | **Not independently verified** | `deploy/docker/mediamtx.yml` | WebRTC/LL-HLS configured; latency NOT measured |

### M4 - Video Recording Management

| ID | Requirement | Status | Verification | Component | Evidence / gap |
| --- | --- | --- | --- | --- | --- |
| M4-R01 | Continuous recording | Implemented | Automated test | `services/recording-service` | tests/integration/test_recording_pipeline.py (real MP4 files produced) |
| M4-R02 | Scheduled recording (per-camera weekly schedule) | Implemented | Automated test | `app/recording/planner.py` | tests/unit/test_domain_logic.py::test_schedule_is_evaluated_in_site_local_time |
| M4-R03 | Manual recording | Implemented | Automated test | `app/recording/triggers.py` | start/stop endpoints tested |
| M4-R04 | Event-triggered recording | Implemented | Automated test | `app/events_engine/ingest.py` | rule action wired to the trigger service |
| M4-R05 | Alarm-triggered recording | Implemented | Automated test | `app/events_engine/ingest.py` | same rule-engine path |
| M4-R06 | Pre-event and post-event recording | Partial | Automated test | `app/recording/triggers.py` | post-event implemented; PRE-EVENT NOT IMPLEMENTED and rejected at startup (see LIMITATIONS) |
| M4-R07 | Record original stream without unnecessary transcoding | Implemented | Automated test | `recorder/segmenter.py` | ffmpeg -c copy by default; verified by the recorder test |
| M4-R08 | Preserve timestamps | Implemented | Automated test | `app/recording/index.py` | UTC segment boundaries persisted and asserted |
| M4-R09 | Handle stream interruption and recover after reconnection | Implemented | Automated test | `recorder/supervisor.py` | bounded backoff restart; stall watchdog |
| M4-R10 | Recover after server restart (orphan segment reconciliation) | Implemented | Automated test | `app/recording/recovery.py` | tests/integration/test_recording_pipeline.py::test_crash_recovery_never_leaves_a_segment_writing |
| M4-R11 | Maintain recording indexes | Implemented | Automated test | `app/recording/index.py` | index rows asserted against files on disk |
| M4-R12 | Prevent silent recording loss (failures raise events) | Implemented | Automated test | `app/recording/health.py` | state transitions emit recording.failed events |
| M4-R13 | Camera-specific recording profiles | Implemented | Automated test | `app/db/models.py RecordingProfile` | profile CRUD endpoints tested |
| M4-R14 | Detect recording gaps | Implemented | Automated test | `app/recording/gaps.py` | tests/unit/test_domain_logic.py::test_gap_detection_finds_holes |
| M4-R15 | Configurable retention policies and safe deletion | Implemented | Automated test | `app/recording/retention.py` | tests/integration/test_recording_pipeline.py::test_retention_deletes_old_files_but_keeps_a_tombstone |
| M4-R16 | Storage rotation when volume thresholds are reached | Implemented | Automated test | `app/recording/retention.py` | apply_space_rotation deletes oldest-first and raises an event |
| M4-R17 | Recording continuity reporting | Implemented | Automated test | `app/api/v1/recordings.py` | continuity endpoint returns percentage and gap list |
| M4-R18 | Segment finalization that avoids corruption | Implemented | Automated test | `recorder/segmenter.py` | spool write then atomic os.replace; verified playable by ffprobe |

### M5 - Playback Management

| ID | Requirement | Status | Verification | Component | Evidence / gap |
| --- | --- | --- | --- | --- | --- |
| M5-R01 | Single-camera playback | Implemented | Automated test | `app/playback/resolver.py` | manifest and media resolution tested |
| M5-R02 | Synchronized multi-camera playback | Partial | Automated test | `app/playback/resolver.py` | server-side synchronized manifests implemented; client sync rendering unverified |
| M5-R03 | Calendar and time-based search | Implemented | Automated test | `app/api/v1/playback.py` | availability and manifest endpoints tested |
| M5-R04 | Timeline navigation and recording availability visualization | Implemented | Automated test | `app/recording/index.py` | tests/unit/test_domain_logic.py::test_timeline_buckets_reflect_recorded_time |
| M5-R05 | Play, pause, seek | Partial | **Not independently verified** | `apps/*` | server seek offsets implemented; client unverified |
| M5-R06 | Playback speed control | Partial | **Not independently verified** | `apps/*` | client-side rate control in source, not built |
| M5-R07 | Frame stepping | **Designed only** | **Not independently verified** | `apps/windows-client` | requires decoder-level control; NOT implemented in alpha |
| M5-R08 | Event markers on timeline | Implemented | Automated test | `app/api/v1/playback.py` | timeline endpoint returns gaps and event data |
| M5-R09 | Recording gap indication | Implemented | Automated test | `app/recording/gaps.py` | gap detection tested |
| M5-R10 | Playback authorization enforced server-side | Implemented | Automated test | `app/core/deps.py` | tests/integration/test_api_flows.py scope tests |
| M5-R11 | Timezone-aware timestamps | Implemented | Automated test | `app/core/timeutil.py` | tests/unit/test_core_primitives.py::test_ensure_utc_and_clamp |
| M5-R12 | Deterministic handling of unavailable or corrupt footage | Implemented | Automated test | `app/playback/resolver.py` | tests/integration/test_recording_pipeline.py::test_playback_refuses_to_hang_when_media_is_missing |
| M5-R13 | Playback timeout and retry management (never hangs) | Implemented | Automated test | `app/playback/resolver.py` | bounded window clamp; structured error instead of a hang |

### M6 - Video Export and Evidence Management

| ID | Requirement | Status | Verification | Component | Evidence / gap |
| --- | --- | --- | --- | --- | --- |
| M6-R01 | Clip export by camera and time range | Implemented | Automated test | `app/exports/jobs.py` | tests/integration/test_recording_pipeline.py::test_export_produces_a_real_clip_with_verifiable_evidence |
| M6-R02 | Snapshot export | Implemented | Automated test | `app/services/snapshots.py` | snapshot endpoints tested |
| M6-R03 | MP4 export | Implemented | Automated test | `app/exports/ffmpeg_export.py` | exported clip verified playable with the expected duration |
| M6-R04 | Original-format export without transcoding when possible | Implemented | Automated test | `app/exports/ffmpeg_export.py` | stream copy is the default path |
| M6-R05 | Export progress reporting | Implemented | Automated test | `app/exports/jobs.py` | ffmpeg -progress parsed into job.progress |
| M6-R06 | Export cancellation | Implemented | Automated test | `app/exports/jobs.py` | cancel endpoint; partial file deleted |
| M6-R07 | Concurrent export limits | Implemented | Automated test | `app/exports/jobs.py` | semaphore plus queue-depth rejection |
| M6-R08 | Disk-space preflight checks | Implemented | Automated test | `app/exports/preflight.py` | estimate from real source sizes with headroom |
| M6-R09 | Export access control | Implemented | Automated test | `app/api/v1/exports.py` | permission and ownership checks tested |
| M6-R10 | Evidence metadata and SHA-256 hashes | Implemented | Automated test | `app/exports/evidence.py` | hash independently recomputed and compared in the test |
| M6-R11 | Chain-of-custody audit record | Implemented | Automated test | `app/exports/evidence.py` | append-only custody list asserted (requested -> produced) |
| M6-R12 | Download authorization with single-use tokens | Implemented | Automated test | `app/api/v1/exports.py` | token issue and constant-time verification tested |
| M6-R13 | Export expiration and cleanup | Implemented | Automated test | `app/exports/cleanup.py` | expiry loop deletes files and marks jobs EXPIRED |
| M6-R14 | Clear errors for missing/damaged footage; never substitute footage | Implemented | Automated test | `app/exports/jobs.py` | tests/integration/test_recording_pipeline.py::test_export_refuses_rather_than_silently_shortening |
| M6-R15 | Export path confinement (no path traversal) | Implemented | Automated test | `app/core/paths.py` | tests/unit/test_core_primitives.py::test_resolve_within_blocks_traversal |

### M7 - PTZ Control

| ID | Requirement | Status | Verification | Component | Evidence / gap |
| --- | --- | --- | --- | --- | --- |
| M7-R01 | ONVIF PTZ capability discovery | Partial | **Not independently verified** | `app/devices/onvif_adapter.py` | GetConfigurations wired; hardware-dependent |
| M7-R02 | Pan, tilt and zoom | Partial | Automated test | `app/ptz/service.py` | server logic verified; device execution hardware-dependent |
| M7-R03 | Continuous movement | Partial | Automated test | `app/ptz/service.py` | capability-gated; arbitration tested |
| M7-R04 | Relative and absolute movement where supported | Partial | Automated test | `app/ptz/service.py` | capability-gated paths implemented |
| M7-R05 | Preset creation, recall and management | Implemented | Automated test | `app/api/v1/ptz.py` | preset endpoints implemented and capability-gated |
| M7-R06 | PTZ speed adjustment | Implemented | Automated test | `app/ptz/service.py` | speed multiplies the velocity vector |
| M7-R07 | Stop command | Implemented | Automated test | `app/ptz/service.py` | stop releases the control lock |
| M7-R08 | Permission-based PTZ access | Implemented | Automated test | `app/core/rbac.py` | PTZ_CONTROL absent from the viewer role; tested |
| M7-R09 | PTZ ownership / control arbitration with priority | Implemented | Automated test | `app/ptz/arbiter.py` | tests/unit/test_domain_logic.py::test_ptz_lock_blocks_second_operator |
| M7-R10 | Automatic stop on control release | Implemented | Automated test | `app/ptz/service.py` | stop clears moving_until and releases the lock |
| M7-R11 | Automatic stop on client disconnect (watchdog) | Implemented | Automated test | `app/ptz/arbiter.py, app/lifecycle.py` | tests/unit/test_domain_logic.py::test_ptz_watchdog_identifies_runaway_and_dead_clients |
| M7-R12 | Server-side movement time limit (no runaway PTZ) | Implemented | Automated test | `app/ptz/arbiter.py` | tests/unit/test_domain_logic.py::test_ptz_move_duration_is_clamped |
| M7-R13 | PTZ state recovery after service restart | Implemented | Automated test | `app/ptz/arbiter.py` | tests/unit/test_domain_logic.py::test_ptz_startup_clears_all_locks |

### M8 - Events, Alarms and Rule Engine

| ID | Requirement | Status | Verification | Component | Evidence / gap |
| --- | --- | --- | --- | --- | --- |
| M8-R01 | Camera online/offline events | Implemented | Automated test | `app/devices/monitor.py` | one event per state transition |
| M8-R02 | Video loss events | Implemented | Automated test | `app/recording/health.py` | recording failure state emits an event |
| M8-R03 | Tampering events (camera-sourced) | Partial | Automated test | `app/analytics/ingest.py` | ONVIF topic mapping tested; depends on camera support |
| M8-R04 | Motion detection events (camera-sourced) | Partial | Automated test | `app/analytics/ingest.py` | tests/unit/test_domain_logic.py::test_onvif_motion_maps_to_camera_edge_source |
| M8-R05 | ONVIF event subscription (PullPoint) | Partial | **Not independently verified** | `app/devices/onvif_adapter.py` | CreatePullPointSubscription/PullMessages implemented; hardware-dependent |
| M8-R06 | Analytics events | Implemented | Automated test | `app/analytics/ingest.py` | ingestion endpoint tested end to end |
| M8-R07 | Storage warning events | Implemented | Automated test | `app/storage/volumes.py` | tests/unit/test_domain_logic.py::test_volume_state_transitions_on_low_space |
| M8-R08 | Recording failure events | Implemented | Automated test | `app/recording/health.py` | state machine emits recording.failed |
| M8-R09 | Service failure and system health events | Implemented | Automated test | `app/health/registry.py` | health report drives degraded/unhealthy status |
| M8-R10 | User-defined alarm rules | Implemented | Automated test | `app/events_engine/rules.py` | rule CRUD plus condition validation tested |
| M8-R11 | Alarm lifecycle New/Active/Acknowledged/Resolved/Closed | Implemented | Automated test | `app/events_engine/alarms.py` | tests/unit/test_domain_logic.py::test_alarm_state_machine_transitions |
| M8-R12 | Alarm severity, assignment, acknowledgement, history | Implemented | Automated test | `app/api/v1/events.py` | tests/integration/test_api_flows.py alarm tests |
| M8-R13 | Event filtering and alarm search | Implemented | Automated test | `app/api/v1/events.py` | search endpoints with scope filtering |
| M8-R14 | Camera-linked playback from alarm | Implemented | Automated test | `app/analytics/linking.py` | playback_jump returns a bounded time window |
| M8-R15 | Snapshot association with alarm | Implemented | Automated test | `app/services/snapshots.py` | snapshot rows carry event_id |
| M8-R16 | Notification routing (webhook, SMTP) | Partial | Automated test | `app/notifications` | webhook signing tested; SMTP implemented but NOT validated against a live server |
| M8-R17 | Event deduplication and suppression windows | Implemented | Automated test | `app/events_engine/dedupe.py` | tests/unit/test_domain_logic.py::test_event_dedupe_collapses_repeats |
| M8-R18 | Escalation | Implemented | Manual / documented | `app/events_engine/escalation.py` | due_escalations/mark_escalated implemented and wired into the supervisor loop; NO dedicated automated test yet |
| M8-R19 | Concurrency-safe acknowledgement (no stale overwrite) | Implemented | Automated test | `app/events_engine/alarms.py` | tests/integration/test_api_flows.py::test_concurrent_acknowledge_rejects_stale_version |
| M8-R20 | Complete audit trail of alarm actions | Implemented | Automated test | `app/core/audit.py` | audit rows asserted after alarm actions |

### M9 - AI Video Analytics

| ID | Requirement | Status | Verification | Component | Evidence / gap |
| --- | --- | --- | --- | --- | --- |
| M9-R01 | Ingest camera-side AI metadata | Implemented | Automated test | `app/analytics/ingest.py` | source recorded as camera_edge and never relabelled |
| M9-R02 | Process ONVIF analytics events | Partial | Automated test | `app/devices/onvif_adapter.py` | notification parsing unit tested; live subscription hardware-dependent |
| M9-R03 | Store analytics results | Implemented | Automated test | `app/db/models.py AnalyticsEvent` | ingestion endpoint tested |
| M9-R04 | Search analytics metadata | Implemented | Automated test | `app/api/v1/analytics.py` | search endpoint tested with scope filtering |
| M9-R05 | Link analytics events to recorded footage | Implemented | Automated test | `app/analytics/linking.py` | segment linking by time |
| M9-R06 | Per-camera AI capability configuration | Implemented | Automated test | `app/api/v1/analytics.py zones` | zone CRUD plus analytics_config column |
| M9-R07 | Analytics overlays where metadata permits | Partial | **Not independently verified** | `apps/*` | bounding boxes delivered to clients; rendering unverified |
| M9-R08 | Server-side inference worker architecture (ONNX Runtime) | Partial | Automated test | `services/analytics-service` | worker and detector interface implemented; NO models bundled |
| M9-R09 | Human detection | **Designed only** | **Not independently verified** | `services/analytics-service/analytics_worker/detectors` | detector interface ready; requires an operator-supplied ONNX model |
| M9-R10 | Vehicle detection | **Designed only** | **Not independently verified** | `services/analytics-service/analytics_worker/detectors` | as M9-R09 |
| M9-R11 | Intrusion detection / region entry-exit | Implemented | Automated test | `app/analytics/zones.py` | tests/unit/test_domain_logic.py zone tests |
| M9-R12 | Tripwire / line crossing | Implemented | Automated test | `app/analytics/zones.py` | tests/unit/test_domain_logic.py::test_tripwire_fires_once_per_track |
| M9-R13 | Loitering detection | Implemented | Automated test | `app/analytics/zones.py` | tests/unit/test_domain_logic.py::test_loitering_requires_dwell_time |
| M9-R14 | Object left behind / object removal | **Not implemented** | **Not independently verified** | `services/analytics-service` | NOT implemented in alpha; roadmap R2 |
| M9-R15 | Crowd/people/vehicle counting | Partial | Automated test | `app/analytics/counting.py` | aggregation over ingested detections implemented; detector not bundled |
| M9-R16 | Object classification | **Designed only** | **Not independently verified** | `services/analytics-service/analytics_worker/detectors` | requires a model |
| M9-R17 | Facial detection | **Designed only** | **Not independently verified** | `services/analytics-service/analytics_worker/detectors` | requires a model |
| M9-R18 | Facial recognition | **Designed only** | Automated test | `app/analytics/privacy.py` | privacy, authorization and retention controls implemented and tested; MATCHING ENGINE NOT IMPLEMENTED |
| M9-R19 | ANPR / LPR | Partial | Automated test | `app/analytics/anpr.py` | plate ingestion, normalization, watchlist and search implemented; SERVER-SIDE OCR NOT IMPLEMENTED |
| M9-R20 | Vehicle attributes | Partial | Automated test | `app/analytics/ingest.py` | attribute storage and search only, camera-sourced |
| M9-R21 | Intelligent motion detection / smart search | Partial | Automated test | `app/api/v1/analytics.py` | metadata-based smart search implemented; pixel-based search NOT implemented |
| M9-R22 | Business intelligence dashboards | Implemented | Automated test | `app/api/v1/reports.py` | analytics summary and counting endpoints tested |
| M9-R23 | AI failure must not interrupt recording (process isolation) | Implemented | Manual / documented | `services/analytics-service` | separate process and queue; recorder has no dependency on analytics |
| M9-R24 | FR/ANPR authorization, retention, audit and privacy safeguards | Implemented | Automated test | `app/analytics/privacy.py` | tests/integration/test_api_flows.py::test_anpr_requires_enablement_and_justification |
| M9-R25 | CPU-only fallback with optional GPU acceleration | Implemented | Automated test | `services/analytics-service/analytics_worker/runtime.py` | provider selection falls back to CPUExecutionProvider |

### M10 - Storage Management

| ID | Requirement | Status | Verification | Component | Evidence / gap |
| --- | --- | --- | --- | --- | --- |
| M10-R01 | Multiple storage volumes | Implemented | Automated test | `app/storage/volumes.py` | volume CRUD and selection tested |
| M10-R02 | Storage pool configuration | Implemented | Automated test | `app/storage/volumes.py` | priority-ordered pool |
| M10-R03 | Camera-to-storage assignment | Implemented | Automated test | `app/storage/allocator.py` | tests/unit/test_domain_logic.py allocator tests |
| M10-R04 | Disk capacity monitoring and free-space thresholds | Implemented | Automated test | `app/storage/monitor.py` | state transitions tested |
| M10-R05 | Storage health reporting and failure alerts | Implemented | Automated test | `app/storage/volumes.py` | events raised on state change |
| M10-R06 | Retention and automatic old-recording deletion | Implemented | Automated test | `app/recording/retention.py` | retention test deletes and tombstones |
| M10-R07 | Storage path validation | Implemented | Automated test | `app/core/paths.py` | write test at registration; confinement tested |
| M10-R08 | Recording index consistency checks | Implemented | Automated test | `app/recording/recovery.py` | orphan scan and writing-segment recovery tested |
| M10-R09 | Storage migration tooling | Partial | **Not independently verified** | `tools/storage_migrate.py` | tool delivered; not executed against a second volume here |
| M10-R10 | Configuration backup and restore | Partial | **Not independently verified** | `tools/backup_restore.py` | tool delivered; restore not exercised in CI |
| M10-R11 | Database backup and recovery procedure | Partial | **Not independently verified** | `scripts/db_backup.sh, docs/operations` | scripts and procedure documented; not executed against PostgreSQL |
| M10-R12 | Storage diagnostics | Implemented | Automated test | `app/storage/diagnostics.py` | diagnostics endpoint returns latency, free space and missing-file samples |
| M10-R13 | NAS / SMB / NFS storage support | Partial | **Not independently verified** | `app/storage/volumes.py` | mounted-path model supported; NO network-share validation performed |
| M10-R14 | RAID-aware monitoring | Partial | Automated test | `app/storage/raid.py` | mdstat parsing tested; HARDWARE RAID CONTROLLERS NOT COVERED |
| M10-R15 | Storage failover (volume switch on failure) | Implemented | Automated test | `app/storage/allocator.py` | allocator skips unusable volumes and fails loudly when none remain |
| M10-R16 | Recording redundancy / N+1 / N+N recording | **Designed only** | **Not independently verified** | `docs/architecture/ha-design.md` | DESIGNED ONLY; NOT implemented in alpha |
| M10-R17 | Distinguish software redundancy from hardware RAID | Implemented | Manual / documented | `app/storage/raid.py` | documented in code and surfaced in the API response |

### M11 - User, Role and Security Management

| ID | Requirement | Status | Verification | Component | Evidence / gap |
| --- | --- | --- | --- | --- | --- |
| M11-R01 | Administrator / Operator / Viewer built-in roles | Implemented | Automated test | `app/core/rbac.py` | tests/unit/test_domain_logic.py::test_builtin_role_permission_sets_are_distinct |
| M11-R02 | Custom roles | Implemented | Automated test | `app/api/v1/users.py` | role creation with permission validation |
| M11-R03 | Per-camera permissions | Implemented | Automated test | `app/core/rbac.py` | tests/integration/test_api_flows.py::test_camera_scope_grant_allows_access |
| M11-R04 | Per-site permissions | Implemented | Automated test | `app/core/rbac.py` | site scope with ancestor resolution |
| M11-R05 | Per-feature permissions | Implemented | Automated test | `app/core/rbac.py` | 26 discrete permissions enforced server-side |
| M11-R06 | Strong password storage (Argon2id) | Implemented | Automated test | `app/core/security.py` | tests/unit/test_security_defaults.py cost-parameter tests |
| M11-R07 | Session expiration | Implemented | Automated test | `app/core/security.py` | access token TTL enforced on decode |
| M11-R08 | Token revocation (jti denylist + refresh rotation) | Implemented | Automated test | `app/core/security.py` | tests/integration/test_api_flows.py::test_logout_revokes_access_token and refresh reuse detection |
| M11-R09 | Audit logs | Implemented | Automated test | `app/core/audit.py` | append-only trail asserted |
| M11-R10 | User activation / deactivation | Implemented | Automated test | `app/api/v1/users.py` | tests/integration/test_api_flows.py::test_deactivating_user_revokes_access_immediately |
| M11-R11 | Account lockout protection | Implemented | Automated test | `app/core/security.py` | tests/integration/test_api_flows.py::test_account_lockout_after_repeated_failures |
| M11-R12 | Secure password recovery (admin-issued reset) | Implemented | Automated test | `app/api/v1/users.py` | admin reset forces change and revokes sessions |
| M11-R13 | Multi-factor authentication (TOTP) | Implemented | Automated test | `app/core/mfa.py` | tests/integration/test_api_flows.py::test_mfa_enrolment_and_login (including replay rejection) |
| M11-R14 | LDAP / Active Directory integration | **Designed only** | Automated test | `app/identity/ldap.py` | interface only; every method raises not_implemented and production start-up is blocked |
| M11-R15 | OIDC / SSO integration | Partial | **Not independently verified** | `app/identity/oidc.py` | authorization-code flow with PKCE implemented; NOT tested against a real IdP |
| M11-R16 | Authorization enforced server-side on every protected surface | Implemented | Automated test | `app/core/deps.py` | tests/integration/test_api_flows.py authz matrix |

### M12 - Multi-Site Enterprise Management

| ID | Requirement | Status | Verification | Component | Evidence / gap |
| --- | --- | --- | --- | --- | --- |
| M12-R01 | Organization hierarchy (org > site > building > floor) | Implemented | Automated test | `app/db/models.py Site` | materialized path hierarchy tested |
| M12-R02 | Camera groups | Implemented | Automated test | `app/api/v1/cameras.py tags` | tags plus site assignment |
| M12-R03 | Centralized camera inventory | Implemented | Automated test | `app/api/v1/cameras.py` | inventory listing and CSV export |
| M12-R04 | Site-level permissions | Implemented | Automated test | `app/core/rbac.py` | site scope tested |
| M12-R05 | Remote server registration | Partial | Automated test | `app/api/v1/system.py federation` | registration implemented and licence-gated; multi-server runtime NOT validated |
| M12-R06 | Centralized monitoring across servers | Partial | Automated test | `app/federation/aggregator.py` | aggregation client implemented; single-node tested only |
| M12-R07 | Distributed recording / multi-server architecture | **Designed only** | Automated test | `docs/architecture/multi-site.md` | DESIGNED; API returns 501 not_implemented |
| M12-R08 | Site health dashboard | Implemented | Automated test | `app/api/v1/reports.py` | dashboard endpoint tested |
| M12-R09 | Centralized alarm monitoring | Implemented | Automated test | `app/api/v1/events.py` | alarm search across sites |
| M12-R10 | Centralized user management | Implemented | Automated test | `app/api/v1/users.py` | user CRUD tested |
| M12-R11 | Secure site-to-site communication (TLS + signed tokens) | Partial | **Not independently verified** | `app/federation` | token encryption and TLS verification implemented; no cross-site deployment test |

### M13 - Windows Desktop Application

| ID | Requirement | Status | Verification | Component | Evidence / gap |
| --- | --- | --- | --- | --- | --- |
| M13-R01 | Native .NET 8 WPF client (not a browser wrapper) | Partial | **Not independently verified** | `apps/windows-client` | WPF/MVVM source delivered; NO .NET SDK in this build environment |
| M13-R02 | Professional login screen | Partial | **Not independently verified** | `apps/windows-client/Views` | source delivered |
| M13-R03 | Server connection management | Partial | **Not independently verified** | `apps/windows-client/Services/ApiClient.cs` | source delivered |
| M13-R04 | Live monitoring dashboard with camera tree | Partial | **Not independently verified** | `apps/windows-client/Views` | source delivered |
| M13-R05 | Custom layouts and multi-monitor operation | Partial | **Not independently verified** | `apps/windows-client` | source delivered |
| M13-R06 | Playback interface | Partial | **Not independently verified** | `apps/windows-client/Views` | source delivered |
| M13-R07 | Alarm management | Partial | **Not independently verified** | `apps/windows-client/Views` | source delivered |
| M13-R08 | PTZ panel | Partial | **Not independently verified** | `apps/windows-client/Views` | source delivered |
| M13-R09 | Export and snapshot interface | Partial | **Not independently verified** | `apps/windows-client/Views` | source delivered |
| M13-R10 | Dark and light themes | Partial | **Not independently verified** | `apps/windows-client/Themes` | Dark.xaml resources only; light theme and switching not implemented |
| M13-R11 | Keyboard shortcuts and notification centre | Partial | **Not independently verified** | `apps/windows-client` | source delivered |
| M13-R12 | Connection status, error recovery, persistent preferences | Partial | **Not independently verified** | `apps/windows-client/Services` | source delivered |
| M13-R13 | Video rendering pipeline (LibVLCSharp) | Partial | **Not independently verified** | `apps/windows-client/Controls` | integration code written against LibVLCSharp; NOT compiled or run |

### M14 - Web Application

| ID | Requirement | Status | Verification | Component | Evidence / gap |
| --- | --- | --- | --- | --- | --- |
| M14-R01 | Responsive React/TypeScript web client | Partial | **Not independently verified** | `apps/web-client` | source delivered; npm build not executed |
| M14-R02 | System administration UI | Partial | **Not independently verified** | `apps/web-client/src/pages` | source delivered |
| M14-R03 | Camera onboarding UI | Partial | **Not independently verified** | `apps/web-client/src/pages/Cameras.tsx` | source delivered |
| M14-R04 | User and site management UI | Partial | **Not independently verified** | `apps/web-client/src/pages` | source delivered |
| M14-R05 | Recording schedule UI | Partial | **Not independently verified** | `apps/web-client/src/pages` | source delivered |
| M14-R06 | Storage configuration UI | Partial | **Not independently verified** | `apps/web-client/src/pages/Storage.tsx` | source delivered |
| M14-R07 | Alarm management UI | Partial | **Not independently verified** | `apps/web-client/src/pages/Alarms.tsx` | source delivered |
| M14-R08 | Reports and system health UI | Partial | **Not independently verified** | `apps/web-client/src/pages/Dashboard.tsx` | source delivered |
| M14-R09 | Browser live view via WebRTC/HLS (never raw RTSP) | Partial | **Not independently verified** | `apps/web-client/src/components/VideoTile.tsx` | WHEP/HLS player source delivered; requires a MediaMTX runtime |
| M14-R10 | Playback and export UI | Partial | **Not independently verified** | `apps/web-client/src/pages/Playback.tsx` | source delivered |

### M15 - Dashboards and Reporting

| ID | Requirement | Status | Verification | Component | Evidence / gap |
| --- | --- | --- | --- | --- | --- |
| M15-R01 | Camera totals: total/online/offline/recording/failed | Implemented | Automated test | `app/api/v1/reports.py` | tests/integration/test_api_flows.py::test_dashboard_reports_real_counts |
| M15-R02 | Storage usage reporting | Implemented | Automated test | `app/api/v1/reports.py` | dashboard storage block |
| M15-R03 | Server health: CPU, RAM, disk, uptime | Implemented | Automated test | `app/health/metrics.py` | psutil measurements asserted present |
| M15-R04 | Network bandwidth aggregation | Implemented | Automated test | `app/health/metrics.py` | measured NIC rates plus clearly labelled estimates |
| M15-R05 | Active session reporting | Implemented | Automated test | `app/api/v1/reports.py` | session counts in the dashboard |
| M15-R06 | Alarm and analytics statistics | Implemented | Automated test | `app/api/v1/reports.py` | analytics summary endpoint groups by source |
| M15-R07 | Export history and audit activity reports | Implemented | Automated test | `app/api/v1/reports.py` | audit report tested |
| M15-R08 | Downloadable reports (CSV) with configurable periods | Implemented | Automated test | `app/api/v1/reports.py` | CSV export for cameras/alarms/audit/exports |
| M15-R09 | Prometheus metrics endpoint | Implemented | Automated test | `app/health/metrics.py` | exposition generated and content type asserted |

### M16 - Network and Bandwidth Management

| ID | Requirement | Status | Verification | Component | Evidence / gap |
| --- | --- | --- | --- | --- | --- |
| M16-R01 | Main/sub/third stream profiles with bitrate awareness | Implemented | Automated test | `app/media/stream_policy.py` | policy tests |
| M16-R02 | Per-camera bandwidth estimation | Implemented | Automated test | `app/capacity/model.py` | capacity model tests |
| M16-R03 | Aggregate inbound/outbound bandwidth | Implemented | Automated test | `app/health/metrics.py` | bandwidth summary endpoint |
| M16-R04 | Active client bandwidth accounting | Partial | Automated test | `app/media/session_broker.py` | ESTIMATED from stream profiles, not measured per client |
| M16-R05 | Server resource monitoring | Implemented | Automated test | `app/health/metrics.py` | psutil metrics |
| M16-R06 | Connection timeouts and network failure recovery | Implemented | Automated test | `app/core/outbound.py, app/core/backoff.py` | every outbound path is time-bounded; backoff tests |
| M16-R07 | Network-aware stream selection | Implemented | Automated test | `app/media/stream_policy.py` | budget-aware downgrade tested |
| M16-R08 | TLS support / secure remote access architecture | Partial | **Not independently verified** | `deploy, docs/security` | reverse-proxy TLS termination documented; certificates not provisioned here |
| M16-R09 | Camera credentials and source URLs never exposed to clients | Implemented | Automated test | `app/media/session_broker.py` | tests/integration/test_api_flows.py asserts no password or rtsp:// in responses |
| M16-R10 | Network isolation and site boundaries enforced on discovery | Implemented | Automated test | `app/core/netguard.py` | tests/unit/test_core_primitives.py SSRF and range-cap tests |

### M17 - Installation, Configuration and Operations

| ID | Requirement | Status | Verification | Component | Evidence / gap |
| --- | --- | --- | --- | --- | --- |
| M17-R01 | Windows native installer | Partial | **Not independently verified** | `installers/windows` | PowerShell installer delivered; NOT built or executed (no Windows toolchain) |
| M17-R02 | Windows service registration with dependencies and auto-start | Partial | **Not independently verified** | `deploy/windows` | service definitions and install script delivered; not executed |
| M17-R03 | PostgreSQL service configuration | Partial | **Not independently verified** | `deploy` | scripts and compose service delivered; not executed |
| M17-R04 | ProgramData configuration and filesystem permissions | Partial | **Not independently verified** | `deploy/windows/install.ps1` | ACL commands scripted; not executed |
| M17-R05 | Firewall configuration guidance | Implemented | Manual / documented | `docs/deployment/firewall.md` | documented with an exact port list |
| M17-R06 | Upgrade workflow and rollback | Partial | **Not independently verified** | `scripts/upgrade.sh` | scripts delivered; interrupted-upgrade handling documented |
| M17-R07 | Backup and restore | Partial | **Not independently verified** | `tools/backup_restore.py` | tool delivered; not exercised in CI |
| M17-R08 | Diagnostic bundle generation | Implemented | Manual / documented | `tools/diag_bundle.py` | tool delivered and runnable; redacts secrets |
| M17-R09 | Safe uninstall (data preserved by default) | Partial | **Not independently verified** | `deploy/windows/uninstall.ps1` | script delivered; not executed |
| M17-R10 | Ubuntu systemd units and native deployment | Partial | **Not independently verified** | `deploy/ubuntu` | unit files and install script delivered; not executed on a target OS |
| M17-R11 | Database migrations applied during install/upgrade | Implemented | Automated test | `database/migrations` | every test run applies the real migration |
| M17-R12 | Log rotation | Implemented | Automated test | `app/logging_setup.py, deploy/ubuntu` | RotatingFileHandler plus logrotate config |
| M17-R13 | Docker Compose for development, not the only method | Implemented | Manual / documented | `deploy/docker` | compose file AND native installers both provided |

### M18 - Enterprise Reliability

| ID | Requirement | Status | Verification | Component | Evidence / gap |
| --- | --- | --- | --- | --- | --- |
| M18-R01 | Graceful startup and shutdown | Implemented | Automated test | `app/lifecycle.py` | ordered startup; bounded shutdown |
| M18-R02 | Automatic service recovery (supervised workers) | Implemented | Automated test | `app/core/supervisor.py` | supervisor restart tests |
| M18-R03 | Bounded retries with exponential backoff and jitter | Implemented | Automated test | `app/core/backoff.py` | tests/unit/test_core_primitives.py backoff tests |
| M18-R04 | Circuit breakers | Implemented | Automated test | `app/core/circuit.py` | tests/unit/test_core_primitives.py::test_circuit_breaker_opens_and_recovers |
| M18-R05 | Configurable resource limits | Implemented | Automated test | `app/config.py` | session, export, queue and analytics caps |
| M18-R06 | Worker health monitoring | Implemented | Automated test | `app/health/registry.py` | worker heartbeat rows and supervisor status probe |
| M18-R07 | Database connection recovery | Implemented | Automated test | `app/db/session.py` | pool_pre_ping plus bounded reconnect loop |
| M18-R08 | Media-session cleanup (TTL + orphan reaper) | Implemented | Automated test | `app/media/session_broker.py` | reaper closes expired sessions and releases gateway paths |
| M18-R09 | Recording recovery and corruption detection | Implemented | Automated test | `app/recording/recovery.py` | crash recovery test |
| M18-R10 | Safe concurrency and transactional state transitions | Implemented | Automated test | `app/events_engine/alarms.py, app/db/session.py` | optimistic concurrency test |
| M18-R11 | Consistent camera deletion across subsystems | Implemented | Automated test | `app/services/camera_lifecycle.py` | ordered teardown test |
| M18-R12 | Predictable timeout handling | Implemented | Automated test | `app/core/outbound.py` | no unbounded outbound call exists |
| M18-R13 | Low-disk handling | Implemented | Automated test | `app/storage/monitor.py, app/recording/retention.py` | threshold transitions plus emergency rotation |
| M18-R14 | Structured diagnostic logging with correlation IDs | Implemented | Automated test | `app/logging_setup.py` | tests/integration/test_api_flows.py::test_correlation_id_is_returned and redaction tests |
| M18-R15 | High availability / N+1 failover / multi-server redundancy | **Designed only** | **Not independently verified** | `docs/architecture/ha-design.md` | DESIGN ONLY; NOT implemented in alpha |
| M18-R16 | Disaster recovery procedure | Partial | **Not independently verified** | `docs/operations/disaster-recovery.md` | procedure documented; DR exercise NOT performed |
| M18-R17 | Rolling upgrades | **Designed only** | **Not independently verified** | `docs/operations/upgrade.md` | requires a multi-server runtime; NOT implemented |

### M19 - Third-Party Integrations

| ID | Requirement | Status | Verification | Component | Evidence / gap |
| --- | --- | --- | --- | --- | --- |
| M19-R01 | Versioned REST API (/api/v1) with OpenAPI contract | Implemented | Automated test | `app/api, contracts/openapi.json` | 88 paths, 75 schemas; contract generated from the running app |
| M19-R02 | WebSocket event stream | Implemented | Manual / documented | `app/api/v1/ws.py` | token-authenticated and scope-filtered; not load tested |
| M19-R03 | ONVIF client integration | Partial | Automated test | `app/devices/onvif_adapter.py` | protocol handling unit tested; hardware-dependent |
| M19-R04 | RTSP / RTSPS support | Partial | Automated test | `app/media, app/devices` | URL handling and transport selection verified; live pull unverified |
| M19-R05 | Outbound webhooks with HMAC signing and retry | Implemented | Automated test | `app/notifications/webhook.py` | tests/unit/test_domain_logic.py::test_webhook_signature_is_replay_resistant |
| M19-R06 | External analytics engine integration (inbound API) | Implemented | Automated test | `app/api/v1/analytics.py` | detection ingestion endpoint tested |
| M19-R07 | Alarm / access control / BMS integration architecture | Partial | **Not independently verified** | `docs/api` | generic event and webhook contract documented; NO vendor connectors |
| M19-R08 | External identity systems | Partial | Automated test | `app/identity` | OIDC implemented, LDAP designed only |
| M19-R09 | External notification systems (SMTP) | Partial | **Not independently verified** | `app/notifications/email.py` | implemented; NOT validated against a live SMTP server |
| M19-R10 | API rate limiting and service accounts / API keys | Implemented | Automated test | `app/core/ratelimit.py, app/api/v1/users.py` | token bucket tests; API key escalation rejected |

### M20 - Commercial Product Readiness

| ID | Requirement | Status | Verification | Component | Evidence / gap |
| --- | --- | --- | --- | --- | --- |
| M20-R01 | Product configuration management | Implemented | Automated test | `app/config.py, config/` | layered config with production safety validation |
| M20-R02 | Software versioning and build metadata | Implemented | Automated test | `app/version.py` | version endpoint asserts production_ready=false |
| M20-R03 | Upgrade compatibility policy | Implemented | Manual / documented | `docs/operations/upgrade.md` | API and schema compatibility rules documented |
| M20-R04 | Feature entitlement architecture | Implemented | Automated test | `app/licensing/entitlements.py` | quota and feature-flag enforcement tested |
| M20-R05 | Offline licensing (signed licence file, no phone-home) | Implemented | Automated test | `app/licensing/license_file.py` | Ed25519 signature and tamper detection tested; no network call in the validation path |
| M20-R06 | Commercial edition architecture (Lite/Pro/Enterprise) | Implemented | Automated test | `app/licensing/editions.py` | edition specs with camera and feature limits |
| M20-R07 | Customer diagnostics and support bundles | Implemented | Manual / documented | `tools/diag_bundle.py` | bundle generator with secret redaction |
| M20-R08 | Release notes | Implemented | Manual / documented | `CHANGELOG.md` | delivered |
| M20-R09 | Administrator and operator documentation | Implemented | Manual / documented | `docs/user-guides` | delivered |
| M20-R10 | Installation manuals | Implemented | Manual / documented | `docs/deployment` | delivered |
| M20-R11 | API documentation | Implemented | Automated test | `contracts/openapi.json, docs/api` | generated from the running application |
| M20-R12 | Software bill of materials | Implemented | Manual / documented | `tools/generate_sbom.py` | CycloneDX-style SBOM generated from dependency manifests |
| M20-R13 | Third-party license notices | Implemented | Manual / documented | `NOTICE, docs/operations/third-party-licenses.md` | delivered |
| M20-R14 | Licensing must not block authorized offline installations | Implemented | Automated test | `app/licensing` | unlicensed mode keeps recording working within a camera limit |

