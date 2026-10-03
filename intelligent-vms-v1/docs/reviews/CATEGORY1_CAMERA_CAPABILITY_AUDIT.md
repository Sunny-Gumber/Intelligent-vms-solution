# Category 1 — Device and Camera Support evidence audit

Issue: #263 · Source: `intelligent-vms-v1` only · Audit date: 2026-09-28

The 43 checklist identities are unchanged. This is a software/source/test audit of
the VMS, **not** named camera interoperability or production qualification.
Forty-one rows now have scoped software implementations in `QA` and 2 remain
`TARGET`. All 43 retain `verification_status=NV`; none are `VERIFIED`
or `EXTERNAL_EVIDENCE`. A `QA` row is not a supported presales claim. All category rows remain unqualified for real cameras, measured
scale and deployment-specific network behavior.

Audit source is VMS runtime baseline `f40abe99d9420e6141486af4feb29967c7fcfbb7`
plus the evidence-overlay merge `d511d504cc2665643540704c0a29a8280be86c0e`.
The new focused deterministic tests in this audit verify the cited parser/probe
and discovery boundaries. No vendor/model/firmware was supplied.

## Category 1 row audit

| ID | Exact master capability | State / intended mode | Evidence | Scope or reason still pending |
|---|---|---|---|---|
| F01-001 | IP camera discovery | QA / N | E1 | Tenant/site scope and target filtering are enforced in software; site-local multicast execution remains deployment-specific and device/site evidence is absent. |
| F01-002 | Manual camera addition | QA / N | E8 | Target validation/pinning and bounded media errors are implemented; real-camera interoperability remains unverified. |
| F01-003 | Automatic camera search | QA / N | E1 | Operator-triggered automatic LAN search is scope-bound and target-filtered; site-local deployment evidence is still required. |
| F01-004 | Addition by IP address | QA / N | E8 | Manual create enforces the configured camera-network policy before persistence/provisioning. |
| F01-005 | Addition by domain name | QA / N | E8 | DNS targets must resolve to exactly one allowed address and are pinned to that IP before downstream use. |
| F01-006 | Addition by serial number | QA / C | E12 | Bounded site-local WS-Discovery plus lightweight exact ONVIF SerialNumber matching reuses normal secure onboarding; no vendor-cloud/global lookup is claimed. |
| F01-007 | Addition by QR code | TARGET / R | E12 | A credential-safe versioned decoded-QR onboarding API exists, but the current web UI has no QR scanner; complete end-user QR addition remains pending. |
| F01-008 | Username/password configuration | QA / N | E2 | Initial credentials are encrypted and public error/health responses are redacted; credential rotation remains separate work. |
| F01-009 | ONVIF support | QA / C | E3 | Device/Media software path is scope-bound and sanitizes public metadata/faults; named camera/firmware evidence and write controls remain absent. |
| F01-010 | RTSP stream support | QA / I | E4 | MediaMTX RTSP wiring, site-bound target validation and diagnostic redaction are tested in software; real-device playback remains unverified. |
| F01-011 | Manufacturer-specific drivers | TARGET / R | E12 | A driver registry/contract exists, but no real manufacturer adapter is installed; this row cannot be promoted without an actual vendor-specific implementation. |
| F01-012 | Connection testing | QA / N | E5 | Probe and immediate media health are bounded, scope-aware and redacted in software; real-network/device behavior remains unverified. |
| F01-013 | Online/offline status | QA / N | E6 | Immediate and persisted health paths exist and immediate diagnostics are allowlisted; real-device failure behavior remains unverified. |
| F01-014 | Camera naming | QA / N | E10 | Name updates preserve camera ID and stable live/record stream keys; site/tenant migration remains separate. |
| F01-015 | Location description | QA / N | E10 | Bounded location description metadata can be set/cleared without changing camera identity. |
| F01-016 | Camera grouping | QA / N | E10 | Tenant/site-scoped group CRUD and same-scope assignment are implemented; deleting a group only unassigns cameras. |
| F01-017 | Camera deletion | QA / N | E8 | Single-node delete removes enabled continuous recorder then live path fail-closed before row deletion; retained segment files follow storage retention. |
| F01-018 | Camera replacement | QA / N | E10 | Physical source replacement preserves logical camera/stream identity, validates site target policy and invalidates stale ONVIF capability metadata. |
| F01-019 | Credential modification | QA / N | E10 | Encrypted credential rotation/clearing is implemented with local compensation and distributed source re-apply semantics. |
| F01-020 | Basic firmware info | QA / C | E7 | Firmware parsing is implemented and public ONVIF metadata is sanitized; vendor/model/firmware accuracy is not device-qualified. |
| F01-021 | Main-stream configuration | QA / C | E11 | Main managed profile encoder state/options/write/readback are implemented; device support remains option-dependent and NV. |
| F01-022 | Sub-stream configuration | QA / C | E11 | Sub managed profile encoder state/options/write/readback are implemented; device support remains option-dependent and NV. |
| F01-023 | Third-stream support | QA / I | E12 | A distinct third profile now has a stable MediaMTX path, public WebRTC/HLS URLs, lifecycle/reconcile/delete support and multi-key node fencing. |
| F01-024 | Resolution configuration | QA / C | E11 | Resolution writes are restricted to camera-advertised encoder resolutions and read back after write. |
| F01-025 | Frame-rate configuration | QA / C | E11 | Frame-rate writes are validated against advertised encoder ranges and read back. |
| F01-026 | Bit-rate configuration (CBR/VBR) | QA / C | E11 | Bitrate/rate-mode writes are applied only when the requested field/range is advertised by the camera. |
| F01-027 | H.264 / H.265 / MJPEG support | QA / C | E12 | Managed roles can select existing advertised H264/H265/JPEG(MJPEG) ONVIF profiles and safely re-provision media; named-device triple-codec availability remains NV. |
| F01-028 | GOP configuration | QA / C | E11 | GOV/GOP length is validated against advertised encoder options and read back. |
| F01-029 | Video-quality configuration | QA / C | E11 | Encoder quality is validated against the advertised quality range and read back. |
| F01-030 | PAL/NTSC support | QA / C | E11 | PAL/NTSC is set only when exactly one advertised VideoSourceMode description explicitly identifies the requested standard; ambiguous devices fail unsupported. |
| F01-031 | Image rotation/mirror/flip | QA / C | E12 | Rotation/mirror remain option-dependent; vertical flip is represented only when the camera advertises mirror plus ON/180-degree rotation primitives, otherwise it fails closed. |
| F01-032 | Brightness/contrast/saturation/sharpness/hue adjustment | QA / C | E11 | Imaging values are option/range validated before write; hue is used only when the camera exposes a recognized writable field. |
| F01-033 | Day/night mode | QA / C | E11 | Standard IrCutFilter ON/OFF/AUTO is validated against Imaging options and read back. |
| F01-034 | IR control | QA / C | E11 | Only camera-advertised reserved tt:IRLamp auxiliary commands are sent; vendor-private IR commands are not inferred. |
| F01-035 | White balance | QA / C | E11 | White-balance mode/gains are validated against advertised Imaging options before write. |
| F01-036 | Backlight compensation | QA / C | E11 | Backlight mode/level is option-dependent, validated and read back. |
| F01-037 | Wide dynamic range | QA / C | E11 | WDR mode/level is option-dependent, validated and read back. |
| F01-038 | Exposure control | QA / C | E11 | Exposure mode/priority/time/gain/iris are written only when advertised and are read back. |
| F01-039 | Anti-flicker | QA / C | E11 | Anti-flicker is written only when a recognized advertised anti-flicker/flicker/power-line field is present. |
| F01-040 | Privacy-mask configuration | QA / C | E11 | Media2 mask options are checked before create/update; create/update/delete all return readback. |
| F01-041 | Camera date/time | QA / C | E11 | Device Management date/time get/set/readback is implemented; manual UTC values require timezone information. |
| F01-042 | Text overlay | QA / C | E11 | Text OSD list/create/update/delete uses Media options/readback and preserves non-Plain OSD semantics. |
| F01-043 | Camera-name overlay | QA / C | E11 | A convenience endpoint creates or updates a Plain OSD using the managed camera name. |

Codes: `N` native, `C` camera-dependent, `I` MediaMTX integration,
`P` partial software path, `R` roadmap. `TARGET / R` includes cases where
a narrower read-only or create-only operation exists; it never credits the
complete master capability.

## Evidence index

### E1

`services/control-api/app/routers/onvif.py` (`/discover`), `app/services/onvif_discovery.py`; `tests/test_category1_evidence.py::test_discovery_deduplicates_responses_and_closes_socket`, `tests/test_onvif_phase2a.py::test_probe_match_parser`.

### E2

`app/routers/cameras.py`, `app/routers/onvif.py` initial credentials and `app/core/security.py`; `tests/test_reconciler_phase2b.py::test_source_rebuild_prefers_substream_and_encodes_credentials`, `tests/test_onvif_phase2a.py::test_uri_sanitization_and_credential_injection`.

### E3

`app/services/onvif_client.py` and `app/routers/onvif.py`; `tests/test_category1_evidence.py::test_onvif_probe_carries_firmware_and_sanitized_stream_metadata`, `tests/test_category1_evidence.py::test_onvif_probe_rejects_public_advertised_media_endpoint`, `tests/test_onvif_phase2a.py`.

### E4

`app/services/mediamtx.py`, `app/services/rtsp.py`, `app/services/recording.py`; `tests/test_recording_phase3.py::test_recording_uses_main_stream_not_live_substream`, `tests/test_reconciler_phase2b.py::test_source_rebuild_prefers_substream_and_encodes_credentials`.

### E5

`app/routers/cameras.py` (`/health`), `app/services/health_monitor.py`, `app/routers/onvif.py` (`/probe`); `tests/test_health_phase4.py`, `tests/test_onvif_phase2a.py`.

### E6

`app/services/health_monitor.py`, `app/routers/health.py`; `tests/test_health_phase4.py::test_health_hysteresis_offline_and_recovery`.

### E7

`app/services/onvif_client.py::parse_device_info`, `app/models/entities.py::CameraCapabilityEntity`; `tests/test_category1_evidence.py::test_onvif_probe_carries_firmware_and_sanitized_stream_metadata`.

### E8

`app/routers/cameras.py`: create/list/get/delete exist. A deterministic mocked route check in this audit reached MediaMTX for a disallowed public test address, returned upstream exception text, and showed single-node DELETE removed only the live path without querying a recording policy. This is evidence of a gap, not support.

### E9

`app/services/onvif_client.py::parse_profiles` and `app/routers/onvif.py::onboard_device`; `tests/test_onvif_phase2a.py::test_profile_parser_and_selection_do_not_assume_order`. These read/select profiles, not change device encoder settings.

### E10

`app/routers/cameras.py`, `app/services/camera_lifecycle.py`,
`app/models/entities.py::CameraGroupEntity`, migration
`0013_camera_lifecycle_groups.py`, and
`tests/test_camera_lifecycle_track_a1.py`. Evidence covers stable logical
identity during rename/replacement, tenant/site group isolation, encrypted
credential mutation, capability invalidation, distributed source refresh without
placement-owner/generation changes, and compensating local rollback.


### E11

`app/services/onvif_configuration.py`, `app/services/onvif_client.py`,
`app/routers/onvif.py`, migration `0014_onvif_third_profile.py`, and
`tests/test_onvif_write_controls_track_a1.py`. Evidence covers service-endpoint
site pinning, profile/source/configuration-token retention, advertised option/range
validation, encoder/imaging/source-mode/date-time/IR/OSD/privacy-mask writes,
post-write readback, secret-safe target errors, and explicit fail-closed behavior
for unsupported operations. No named device/firmware was contacted.

### E12

`app/routers/onvif.py`, `app/services/onvif_onboarding.py`,
`app/services/onvif_client.py::identify_xaddr`,
`app/services/camera_lifecycle.py`, `app/services/reconciler.py`,
`app/services/fencing.py`, node-agent fencing,
`app/services/camera_drivers.py`, migration
`0015_camera_third_stream.py`, and
`tests/test_category1_finish_track_a1.py`. Evidence covers bounded exact
site-local serial onboarding, credential-free QR payload validation, a real
third MediaMTX path with distributed reconciliation/fencing, managed selection of
advertised H264/H265/JPEG(MJPEG) profiles, and standards-based composite vertical
flip. No vendor-specific driver or browser QR scanner is implemented by this
evidence.

## Security and tenant-boundary status

The original audit reproduced cross-scope ONVIF targeting and diagnostic
disclosure gaps before PR #268. Those findings are retained as historical audit
context in Git history, not as current behavior.

Current software paths require authorized tenant/site scope, exact site network
policy and pinned ONVIF service targets. Public target/media/SOAP failures are
bounded and deterministic negative tests use synthetic credential markers to
verify they are not disclosed. No real camera/network qualification is implied.

## Camera lifecycle implementation status

The first safety remediation was completed in #266 / PR #268. The follow-on
logical lifecycle cluster is implemented in #269 / PR #270.

The safe-onboarding/deletion/security cluster (#266 / PR #268) and logical
camera lifecycle cluster (#269 / PR #270) now cover the software foundations
described above. The next Category 1 implementation cluster is device-side write
configuration:

1. ONVIF Media encoder writes for main/sub stream resolution, frame rate, bitrate
   mode/rate, GOP and quality where the device advertises supported ranges.
2. ONVIF Imaging writes for rotation/mirror/flip, brightness/contrast/saturation/
   sharpness, day/night, white balance, backlight/WDR, exposure and anti-flicker.
3. Privacy-mask, date/time and OSD/text/camera-name overlay writes through
   capability-discovered device services.
4. Every write path must validate tenant/site target policy, reject unsupported
   capability/range values before network mutation, use bounded secret-safe errors,
   and perform readback verification when the protocol/device supports it.
5. These rows remain device-dependent and `NV` until named vendor/model/firmware
   evidence is collected; software adapters/tests alone do not establish camera
   interoperability.

## Reproduction and acceptance

From `intelligent-vms-v1/`:

```bash
python tools/feature_catalog.py
python tools/feature_catalog.py --check
pytest -q tests/test_feature_catalog.py tests/test_category1_evidence.py
```

Keep `FEATURE_EVIDENCE_OVERLAY.csv` and the regenerated
`FEATURE_CATALOG.csv` together. The release candidate label remains
`RELEASE_CANDIDATE_EXTERNAL_QUALIFICATION_PENDING`; this audit does not
clear Phase 8 target-hardware evidence, Phase 10 real-device testing, or the
security exception in #261.


## Post-remediation re-audit — PR #268

The focused #266 remediation was re-audited after implementation. The software
rows F01-001/002/003/004/005/008/009/012/013/017/020 are promoted to `QA`
only; F01-010 remains `QA`. All keep `verification_status=NV`.

Security/correctness outcomes:
- manual RTSP targets are checked against exact tenant/site camera CIDRs before
  persistence and DNS names are pinned to one site-approved address;
- media provisioning and health failures use bounded public errors;
- immediate health exposes only readiness/tracks plus bounded boolean state;
- ONVIF probe/discovery require authorized tenant/site scope, explicit site CIDRs,
  and discovery additionally requires that the handling process is declared local
  to that site's multicast domain;
- discovered XAddrs pass the camera-network policy;
- public ONVIF service URLs drop credentials/query/fragment data and raw SOAP
  fault text is not exposed;
- single-node camera deletion removes an enabled continuous recording path before
  the live path and keeps the database row when cleanup fails.

Remaining qualification boundary: no named vendor/model/firmware, real-camera,
site-network, hardware or scale evidence is added by this remediation. Site-local
multicast execution is a deployment requirement, not proven by CI.


## Lifecycle re-audit — PR #270

Rows F01-014, F01-015, F01-016, F01-018 and F01-019 are promoted to software
`QA` only. All remain `verification_status=NV`.

The replacement continuity contract preserves camera ID, live stream key,
recording stream key, recording policy and camera-ID-linked subsystems. Because
ONVIF firmware/services/profiles belong to the physical device, replacement
deletes the old capability snapshot and requires a fresh probe before
device-specific metadata is reused.

Single-node credential/replacement changes reconfigure live and enabled
continuous-recording paths before database commit and compensate back to the
prior source when external apply or commit fails. Distributed changes do not
move placement ownership or increment the fencing generation; they clear only
the applied-generation marker so the existing reconciler reapplies the same
assignment.

No real camera, vendor/model/firmware, hardware, GIS, or scale evidence is added
by this milestone.


## ONVIF write-control re-audit — PR #272

The #271 milestone adds capability-discovered camera write controls while keeping
all device-dependent claims at `verification_status=NV`.

Twenty additional rows move to software `QA / C`: F01-021, F01-022,
F01-024–026, F01-028–030 and F01-032–043. The implementation validates
advertised options/ranges before mutation where the ONVIF operation exposes
them, revalidates every advertised service endpoint against the exact tenant/site
camera network policy, uses stored encrypted credentials server-side, maps public
failures to bounded errors and performs post-write readback for encoder, imaging,
source modes, date/time, OSD and privacy-mask operations.

Six Category 1 rows remain TARGET: F01-006 serial-number addition, F01-007 QR
addition, F01-011 manufacturer-specific drivers, F01-023 complete third-stream
VMS media support, F01-027 complete H.264/H.265/MJPEG codec-family support and
F01-031 complete rotation/mirror/flip support. The third ONVIF profile can now be
selected/configured, but no dedicated VMS third-stream media path exists.
Rotation/mirror are implemented when advertised, but generic vertical flip is
not claimed. Existing selected codecs can be configured, but cross-codec switching
is deliberately fail-closed pending a device-qualified Media2 route.

No named vendor/model/firmware, real-camera, hardware or measured-scale evidence
is introduced by PR #272.


## Remaining Category 1 generic-route re-audit — PR #276

Issue #275 closes the remaining generic software routes without manufacturing
support claims.

F01-006 moves to `QA / C`: serial-number addition is explicitly site-local.
The route accepts authorized tenant/site scope, bounds WS-Discovery candidates,
uses at most four concurrent lightweight `GetDeviceInformation` calls, applies
per-operation and overall deadlines, requires an exact SerialNumber match and
then reuses the normal secure ONVIF onboarding path.

F01-023 moves to `QA / I`: the optional third ONVIF profile is now a real VMS
media path with a stable key, public WebRTC/HLS endpoints, create/replace/
credential lifecycle, disable/delete behavior, single-node and distributed
reconciliation, handoff cleanup, and backward-compatible multi-key node fencing.

F01-027 moves to `QA / C`: the VMS can select an existing unused advertised
H264, H265 or JPEG(MJPEG) profile for main/sub/third and safely re-provision that
role. It does not force a camera encoder across codec families when the camera
does not expose such a profile.

F01-031 moves to `QA / C`: advertised ONVIF rotation and mirror continue to be
used directly. Vertical flip is represented as mirror plus 180-degree rotation
only when those exact standard primitives are advertised; unsupported devices
fail closed.

Two rows remain TARGET:
- F01-007: the credential-safe decoded QR onboarding contract exists, but the
  current development web UI has no QR scanner.
- F01-011: a manufacturer-driver registry/contract exists, but no concrete
  vendor-specific adapter has been implemented or device-qualified.

Category 1 therefore stands at **41 QA / 2 TARGET / all 43 NV**. No named
vendor/model/firmware, real-camera, hardware or measured-scale evidence is added
by PR #276.
