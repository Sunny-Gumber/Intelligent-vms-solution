# VMS Boss — Live Cross-Chat State

**Purpose:** canonical short handoff for every ChatGPT/engineer session. Read this after `INTELLIGENT_VMS_START_HERE.md` and before changing code.

**Last synchronized:** 2026-10-03 IST  
**Repository:** Sunny-Gumber/Intelligent-vms-solution  
**Product path:** `intelligent-vms-v1/`

## Live milestone

- Program: **MIGRATION + FIELD-TEST RELEASE TRACK**.
- Destination repository: `Sunny-Gumber/Intelligent-vms-solution`; currently PRIVATE during Phase A.
- Migration issue: destination #1 — Public standalone Intelligent VMS repository cutover.
- Accepted legacy recovery source: `Sunny-Gumber/camvault@6a97653e596c9dab3ab212c7b045fea2cd3fbb15` (legacy issue #301 / PR #302 Ubuntu field-test baseline).
- Exact destination clean-import checkpoint: `fa4bdc13e89e41bd925aa448c628d999a1201da3` on `migration-public-release-baseline`; 323/323 VMS source blobs matched exactly before publication-hygiene changes.
- The old partial destination `migration` branch is not authoritative and was found missing 73 accepted files.
- No active files are allowed under `.github/workflows/` while the destination remains private. Required VMS workflows are staged under `migration-staging/workflows/`.
- Public-release audit is recorded in `docs/reviews/PUBLIC_REPOSITORY_SECURITY_AUDIT.md`; real-place demo labels are sanitized to generic examples. No license is granted; owner license decision remains pending.
- Legacy unfinished Windows work remains separate and unaccepted: camvault issue #303 / PR #304 / branch `release/windows-field-test-server-baseline` at frozen legitimate head `7fc9065a43977329c91988f4dad5e123826f60cc`.
- Do not merge legacy PR #304. After destination baseline public validation/merge, recreate the Windows milestone from destination main and apply that exact legitimate delta.
- Known first Windows continuation fix: provide a deterministic TEST-ONLY `VMS_SECRET_KEY` to the Windows pre-install regression step; production fail-closed behavior must remain unchanged.
- `Sunny-Gumber/camvault` remains the frozen authoritative recovery source until public destination baseline validation and merge complete.
- Product qualification remains **Release Candidate / External Qualification Pending**.
- Do not start the native Windows desktop client until migration + Windows server baseline are accepted.
- Live GitHub state is authoritative over this handoff if newer.

## Cross-chat rules

1. Re-read live GitHub state at session start. This file is a handoff, not authority over newer repository state.
2. Never create a second implementation PR for work already covered by the active milestone.
3. One focused branch/PR per milestone. Never implement directly on `main`.
4. During the working day, prefer investigation, review, design, test planning and a coherent batch over repeated remote writes.
5. A GitHub connector file/code write is a remote push and can trigger CI. Treat it as the daily integration boundary unless a security/correctness emergency requires another push.
6. Before the daily push, re-fetch the branch head. Never overwrite another chat's newer head. Reconcile first.
7. After the daily push, update this file in the same commit/batch where practical with the new exact head, findings, tests and next action.
8. Do not merge merely because GitHub reports `mergeable=true`. Required gates, QA and reviewer evidence still apply.
9. Do not promote device-dependent rows without named hardware/model/firmware evidence.
10. Never claim Production Qualified, 100K certified or full market coverage without the required external evidence.

## Current #276 review focus

Implemented/reviewed areas include third-stream lifecycle/fencing, serial onboarding, credential-safe QR onboarding, managed profile reassignment, codec/orientation boundaries, ONVIF target hardening, and derived third-stream orphan cleanup. Continue independent review for tenant/site authorization, secret/error redaction, recording continuity, migration safety, node-agent multi-key revocation failure behavior and catalog evidence accuracy.

## Session exit checklist

Before ending a chat that materially changes project understanding, record:
- live branch/head/PR;
- what was reviewed or changed;
- exact tests/gates actually executed and their results;
- unresolved defects/blockers;
- QA/reviewer decision;
- next concrete action.

If no remote write is appropriate yet, give the user the findings and leave this repository state unchanged for the daily batch.


## Track A1 Category 1 final acceptance — 2026-10-01

- Issue #275: completed/closed.
- PR #276: merged after independent QA/security review.
- Reviewed head: `bd127673194b024b2c930efb2b7b93602c7ff025`.
- Merge commit: `812052561d7b44fe228db8d6e25293c051081bee`.
- Latest-head gates before merge: repository vms-test PASS; VMS compile/unit/Ruff PASS; 623-row catalog PASS; Phase 7 chaos 14/14 PASS; Phase 8 smoke PASS; Alembic-to-0016 PASS; Compose/Helm/image builds PASS; dependency-audit PASS; fixable HIGH/CRITICAL Trivy enforcement PASS.
- Delivered software routes include bounded site-local serial onboarding, credential-safe QR backend onboarding contract, managed third-stream lifecycle/fencing/cleanup, advertised codec-profile selection, standards-advertised orientation handling, and manufacturer-driver registry contract.
- Device/manufacturer/model/firmware-dependent Category 1 capabilities remain NV / External Qualification Pending.
- Trivy report-only inherited Debian HIGH findings with no currently available fixed package remain tracked under #261; no security gate is waived.
- Product status remains **Release Candidate / External Qualification Pending**.
- Next recommended Track-A milestone: Category 2 — Live Monitoring evidence-first catalog audit under parent #221. Do not implement until a focused milestone is opened from current main.


## Track A2 Category 2 first milestone — 2026-10-01

- Starting main for audit: `dc95569387f04b187ad3fba61fd048ec8c8faf60`.
- Category 2 Live Monitoring: all **21** Feature IDs audited in `docs/reviews/CATEGORY2_LIVE_MONITORING_AUDIT.md`.
- Issue #277 completed; PR #278 merged.
- Reviewed PR head: `0736010327a27210be3e3777bdb16d207d30d633`.
- Merge commit: `9af20f79d729885340481a3b99e688f701f056dd`.
- Delivered P0 software prerequisite: tenant/site-authorized, short-lived RS256 grants with one exact MediaMTX read-path permission; MediaMTX validates locally from cached JWKS with issuer/audience checks. Browser grants stay in Authorization headers, not media URLs/query strings.
- Development WHEP client now uses authenticated ICE/session setup and rejects cross-origin WHEP session Locations before retaining teardown credentials.
- Recording source/path/policy, AI, placement generation/lease/fencing and Category-1 behavior were not changed.
- Exact-head gates passed: CI; Intelligent VMS CI; compile/Ruff; full VMS unit suite; 623-row/48-category catalog; Phase 7 chaos; Phase 8 benchmark smoke; Phase 9 recovery/RC gate; Alembic; Compose; Helm; VMS image builds; dependency audit; fixable HIGH/CRITICAL Trivy enforcement.
- Category-2 real camera/browser/codecs, audio, TURN/NAT, TLS deployment, WAN behavior, viewer concurrency, failover visual continuity and soak remain **NV / External Qualification Pending**.
- Security residual #261 remains: report-only Debian images show inherited HIGH findings without current fixed package versions; no gate is waived.
- Product status remains **Release Candidate / External Qualification Pending**.
- Next recommended Track-A milestone: **F02-007 bounded live-session control/quotas and explicit start/stop semantics**. It is not implemented yet.


## Track A2 Category 2 bounded live-view resource milestone — 2026-10-01

- Starting main: `89789537d6170095ec5536e1d5c97086422d7bb2`.
- Issue #279 completed; PR #281 merged.
- Accepted PR head: `718c8abaada5793f0f1a56b0a0028de994580c0d`.
- Merge commit: `2b1587e17edf218c0c57d13b88fc7b82194edb1c`.
- Feature IDs affected: F02-001, F02-007 and F02-012 as a resource-safety foundation.
- MediaMTX remains authoritative for active reader/WebRTC/HLS lifecycle. Live and optional third paths now carry configurable non-zero `maxReaders`; continuous recording paths deliberately do not.
- Legacy and distributed reconciliation detect missing/stale live reader policy and reapply it to existing live/third paths without waiting for a source/placement change.
- Default `LIVE_VIEW_MAX_READERS_PER_PATH=16` is a safety guardrail only, not measured capacity or a sizing/certification claim.
- Token replay remains possible during grant validity, but one live path cannot create readers beyond the configured media-plane ceiling. Grant expiry still does not revoke an established session.
- Browser crash/network-loss cleanup timing remains MediaMTX-owned and externally qualified. No duplicate durable VMS session registry was introduced.
- Exact per-principal/per-tenant active-session quotas across cameras are not claimed; distributed identity-aware quota/fairness remains future work.
- Safe signing-key rotation/JWKS overlap is separately tracked in #280.
- Exact-head CI PASS: generic CI; Intelligent VMS CI; compile/Ruff/coding policy; full unit suite; 623-row/48-category catalog; Phase 7 chaos; Phase 8 benchmark smoke; Phase 9 recovery/repository gates; Alembic; Compose; Helm; VMS image builds; dependency audit; Trivy fixable HIGH/CRITICAL enforcement.
- Independent adversarial review: PASS after fixing rollout reconciliation for already-existing paths and updating the Category-1 cleanup fixture to remain policy-current.
- Security residual #261 remains open for inherited Debian HIGH findings without available fixed package versions; no Trivy/security gate was weakened.
- Product remains **Release Candidate / External Qualification Pending**.
- Recommended next Category-2 milestone: **selectable fixed/custom live layouts plus selected-camera navigation/drag-drop**, while preserving the new live reader ceiling. Do not implement it as part of #279.

## Track A2 Category 2 signing-key rotation milestone — 2026-10-01

- Starting main: `a838729aa553111c810bb03783dfc274c480009e`.
- Issue #280 completed; PR #282 merged.
- Accepted/reviewed PR head: `c6f897c2d29a448c0cfc40ec46e40ef7a576d594`.
- Merge commit: `38abde20cdb09f1535bfccd5d45ee552ded4fc4d`.
- Independent adversarial review/QA evidence: PR review ID `5376091197`; REVIEW PASS / QA PASS with no blocker.
- Delivered safe RS256/JWKS signing-key rotation: one active private signer plus at most four public-only overlap verification keys, deterministic `kid`, duplicate/invalid/weak/private/excess keyring rejection, and fail-closed token issuance/startup validation.
- Routine rotation requires pre-publishing the next public key, refresh/evidence on every browser-facing MediaMTX node before signer cutover, old-token overlap through TTL plus rollout/clock-skew safety, and explicit retired-key removal/refresh verification.
- MediaMTX v1.21.1 JWKS caching and trusted `POST /v3/auth/jwks/refresh` behavior are documented. Partial regional refresh is explicitly incomplete and can cause node-dependent authorization failures.
- Emergency compromise intentionally removes the compromised public key and invalidates outstanding grants after forced regional refresh; already-established WebRTC sessions are not claimed to be forcibly terminated by JWT rotation.
- Recording source/path/policy/retention, media path provisioning, placement generation/lease/fencing, AI source selection and browser media-session implementation were not changed.
- Exact reviewed head gates PASS: Intelligent VMS compile/unit, vms-test, static/image build, dependency audit and container audit; publish-image skipped as expected.
- Security residual #261 remains open for inherited Debian HIGH findings without available fixed package versions; no security gate was weakened.
- Product remains **Release Candidate / External Qualification Pending**. This milestone does not establish Production Qualified status, universal device compatibility or full Category-2 qualification.
- Category-2 grid/layout work was not started as part of #280.

## Track A2 Category 2 multi-camera live-view grid milestone — 2026-10-01

- Starting main: `90ad8647ca3882836e0c2673a2506bb539696956`.
- Issue #283 completed; PR #284 merged.
- Accepted/reviewed PR head: `71fff275a8afb6b219eed822d0cba1d179286791`.
- Merge commit: `3bdfec6273102ee817d9895f60909525deacd1c4`.
- Independent adversarial review/QA evidence: PR review `5376541868`; REVIEW PASS / QA PASS at deterministic software boundary.
- Delivered fixed 1x1/2x2/3x3/4x4 live layouts, authorized camera-to-tile assignment, active tile, accidental duplicate prevention, independent WHEP tile lifecycle, explicit stop/remove, manual retry, generation-fenced stale async rejection, page/layout cleanup and MediaStream reattachment across DOM reconciliation.
- Review blockers fixed before acceptance: active streams becoming invisible after grid DOM reconciliation; created WHEP sessions not deleted after later negotiation failure; retry double-click suppression; a test escaping defect.
- #278 path-scoped authorization, #281 MediaMTX maxReaders and #282 signing/JWKS rotation are reused unchanged. No second media/auth/quota subsystem was introduced.
- Recording source/path/policy/placement/fencing/completion and AI source selection were not changed. Regression tests verify recording paths remain continuous/non-on-demand and do not inherit live maxReaders.
- Feature evidence: F02-001 remains software QA; F02-002 fixed layouts now software QA with custom/saved layouts still incomplete; F02-007 explicit tile start/stop lifecycle software QA; F02-008 remains partial because drag/drop gesture is not implemented; F02-010 remains missing; F02-012 remains partial; F02-018 remains partial.
- Exact-head gates PASS: generic CI, Intelligent VMS compile/unit, full VMS workflow including 623-row catalog, Phase 7 chaos, Phase 8 smoke, Phase 9 recovery/RC gate, Alembic, Compose, Helm and image builds, dependency audit and container audit.
- Real camera/browser codecs, TURN/NAT/WAN, sixteen-stream workstation/GPU capacity, peer cleanup timing after crashes, failover visual continuity and soak remain NV / External Qualification Pending.
- Security residual #261 remains open; no security gate was weakened.
- Product remains **Release Candidate / External Qualification Pending**.

## Track A2 Category 2 F02-012 live stream-role selection — 2026-10-01

- Starting main: `cc023da65de7d58d8e64b85811a45405adbf6011`.
- Issue #285 completed; PR #286 merged.
- Accepted/reviewed PR head: `6295249da4684b165f6665ee8e0f282ab1ef1fd2`.
- Merge commit: `3230666d9cad9ad6c4a0890665eb2df52ac26bd6`.
- Independent adversarial review/QA evidence: PR review `5377278820`; REVIEW PASS / QA PASS at the deterministic software boundary.
- F02-012 now provides explicit active-tile MAIN/SUB/THIRD selection. MAIN is always available; SUB is offered only when configured; THIRD only when its configured path/key exists. Default remains SUB-if-present, otherwise MAIN.
- The legacy default live path remains unchanged. When SUB exists, explicit MAIN uses a deterministic derived live path; all viewer paths retain #281 maxReaders and #278 exact-path grants. #282 signing/JWKS remains unchanged.
- Role switching reuses #284 per-tile generation fencing, break-before-make cleanup, same-origin WHEP Location checks, tile-local failure and manual retry. Duplicate-camera prevention remains camera-based across roles.
- Review blocker fixed before acceptance: distributed camera deletion initially omitted the new derived MAIN live path; final head removes it from current and stale cleanup nodes with regression coverage.
- Recording remains a separate MAIN-source continuous record path with no live reader ceiling. Live role selection does not mutate recording or AI policy/source configuration.
- Exact-head gates PASS: static/image build, Intelligent VMS compile/unit, full vms-test, dependency audit and container audit; publish image skipped as expected for the PR.
- Physical camera MAIN/SUB/THIRD support, codec/profile combinations, browser/GPU capacity, WAN/TURN/NAT, switch latency, failover visual continuity and soak remain NV / External Qualification Pending.
- Security residual #261 remains open; no security gate was weakened.
- Product remains **Release Candidate / External Qualification Pending**.

## Track A2 Category 2 secure live snapshot milestone — 2026-10-01

- Starting main: `4c858ae368f88dba92947aa9ea38ba98f0afdcd0`.
- Issue #288 completed; PR #289 merged.
- Accepted/reviewed PR head: `74a1e7ed61a97946e153997accf5dbd96e2b23ea`.
- Merge commit: `46f608437ef66727ac6ccfe44b419bb08978560d`.
- Independent adversarial review/QA evidence: PR review `5377466211`; REVIEW PASS / QA PASS at the deterministic software boundary.
- Four-row audit result: F02-005 was missing; F02-006 missing but has an authoritative recording/playback foundation; F02-020 missing; F02-021 partial from authorized MP4 playback.
- Dependency decision: snapshot and clip/export are separate architectures. This milestone implements only secure active-tile snapshot/local download. Manual clip/export remains the next focused recording-backed milestone.
- Snapshot source is the already-authorized decoded frame of the current active tile and exact selected MAIN/SUB/THIRD role. No hidden MAIN upgrade, camera-native/original-sensor claim or silent fallback exists.
- No new backend media endpoint, MediaMTX reader, camera/RTSP credential, FFmpeg/subprocess, server temp file, DB evidence row or persistent snapshot repository was introduced.
- Browser MediaRecorder is rejected for future clip architecture; existing authorized bounded recording playback on record_stream_key is the preferred foundation.
- Review blocker fixed before acceptance: stale snapshot completion originally rerendered the grid unconditionally; final head fences the finalizer on the original camera+role+generation with regression coverage.
- Recording and AI code/configuration are unchanged. Continuous MAIN/source-copy recording remains independent.
- Exact-head gates PASS: static/image build, Intelligent VMS compile/unit, full vms-test, dependency audit and container audit; publish image skipped as expected for the PR.
- F02-005: implemented + software-evidenced, final browser/device qualification NV. F02-020: partial (snapshot local download only). F02-021: partial (snapshot download + existing playback; clip export missing). F02-006: remains missing/target pending recording-backed manual clip/export.
- Product remains **Release Candidate / External Qualification Pending**. Security residual #261 remains separately open.

## Track A2 Category 2 bounded recording-range clip export — 2026-10-01

- Starting main: `81b51af3b1f63dc4c84fcc0b257ed1d6c998e7d4`.
- Issue #290 completed; PR #291 merged.
- Accepted/reviewed PR head: `eed94803a91811b0f24528bf128cc2471193636b`.
- Merge commit: `5c3a500fee327a53a8c282e88ee172142db96aea`.
- Independent review/QA: review `5377973113`; REVIEW PASS / QA PASS at deterministic software boundary.
- Audit decision: F02-006 "Manual recording" is not treated as synonymous with clip export. It remains TARGET/MISSING pending a separate durable Start/Stop manual-intent design. F02-021 clip download and F02-020 local recording-download semantics were dependency-ready and implemented here.
- Export reuses authoritative continuous MAIN/source-copy recording through server-resolved `record_stream_key` and MediaMTX playback. Browser MediaRecorder, second RTSP ingest/recorder, FFmpeg/shell, temp export files and public download URLs are absent.
- Export is admin/operator-only, camera-authorized on each request, timezone-aware, max-duration bounded, requires continuous coverage, rejects gaps, and in distributed mode rejects ranges crossing recording-node/failover boundaries.
- MP4 attachment filenames use sanitized stable camera ID plus UTC start/end. No client-controlled media URL/path/storage key/remux flag exists.
- Resource guardrails: default 900-second duration, 4 concurrent exports per control-api process, configurable 30-second export I/O timeout. These are safety bounds, not measured capacity; concurrency is explicitly not cluster-wide.
- Review fixes before acceptance: stalled export I/O now cannot hold a slot indefinitely; retained historical footage remains exportable after future recording is disabled; test-string defect corrected.
- Export failure does not mutate/stop continuous recording, live WHEP/roles/maxReaders, AI policy/source or #289 snapshot.
- Exact-head gates PASS: compile/unit, full vms-test, static/image build, dependency audit, container audit, 623-row/48-category catalog, Phase 7 chaos 14/14, Phase 8 smoke, Alembic through 0016, Compose and Helm. PR image publish skipped as expected.
- F02-020 and F02-021 are software-QA complete for their stated local snapshot/recording-download and snapshot/clip-download software contracts, while final verification remains NV. F02-006 remains TARGET/MISSING.
- Product remains **Release Candidate / External Qualification Pending**. Security residual #261 remains separately open.

## Track A2 Category 2 durable manual recording — 2026-10-01

- Starting main: `be99bc12dd55065355771b1275fee7d452a0b3f9`.
- Issue #292 completed; PR #293 merged.
- Accepted/reviewed PR head: `b4e24632260be80c3200196c82e24ceada2d8aa0`.
- Merge commit: `7853642d064622dd6e91ca1b8b6aec2796ac7a12`.
- Independent review/QA evidence: PR review `5378965251`; REVIEW PASS / QA PASS at deterministic software boundary.
- F02-006 Manual recording is now implemented as durable server-timed Start/Stop interval intent over authoritative continuous MAIN recording. No second recorder/ingest, browser MediaRecorder, FFmpeg recorder or temp media store was added.
- Alembic 0017 adds shared manual session state with tenant/site/camera/operator ownership, ACTIVE/STOPPED state, immutable max_stop_at and database-enforced one-active-session-per-operator/camera uniqueness. Different operators may independently mark the same camera.
- Start requires current admin/operator camera authorization and active continuous recording. Stop reauthorizes ownership, row-locks and finalizes once. Admin may manage authorized in-scope sessions; ordinary operators own their own sessions.
- Maximum interval uses the #291 export-duration configuration captured immutably at Start. Expired sessions are deterministically finalized on API observation; no browser timer or process-local state is authoritative.
- Refresh/reconnect recovers active sessions from shared DB. Bounded recent history supports retry of the same stopped interval. Manual-session metadata currently follows control-DB administrative retention; no separate automatic history purge is claimed.
- Manual export reuses #291 shared recording-backed export. Gaps and cross-recording-node/failover ranges fail explicitly. Export failure never rewrites Stop history and may be retried.
- Camera deletion finalizes ACTIVE sessions and SET NULL preserves historical metadata; deleted-camera export fails 410 because current camera authorization/source resolution is unavailable.
- Live MAIN/SUB/THIRD, WHEP/maxReaders, AI, snapshot, recording policy/source/retention/placement/fencing/hooks remain independent.
- Exact-head gates PASS: compile/unit/Ruff, full vms-test, static/image build, dependency audit, container audit, 623-row/48-category catalog, Phase 7 chaos 14/14, Phase 9 RC gate, Alembic 0001→0017, Compose and Helm. PR image publish skipped as expected.
- F02-006 engineering state is QA/software-evidenced; final verification remains NV / External Qualification Pending.
- F02-020 and F02-021 remain QA/software-evidenced; architecture is reused, not reopened.
- Product remains **Release Candidate / External Qualification Pending**. Security residual #261 remains separately open.

## Native Windows Desktop Client Foundation — issue #6 / PR #7 — 2026-10-04

- Verified starting main: `e49962afaa8b176ad8f7b67a0293642af51b02d3`, the accepted Windows Server baseline.
- Dedicated branch: `feature/windows-desktop-client-foundation`; no server-baseline PR was reused.
- Existing catalog rows selected: F08-001 Windows desktop client, F08-007 Secure login, F08-008 Remember-login. No duplicate feature IDs were created.
- Technology: .NET 10 LTS + WPF x64. WebView2 is restricted to the replaceable secured WHEP/WebRTC renderer, not the application shell.
- The client reuses existing FastAPI authentication/capability/camera/live-grant APIs and MediaMTX/WHEP. It does not add a database, recorder, camera authority or authentication bypass.
- Remote profiles require HTTPS; HTTP is limited to localhost/loopback field testing. Certificate validation remains platform-default with revocation checking.
- Remember-login uses Windows Credential Manager. Profiles JSON, logs and diagnostics do not intentionally contain bearer tokens/passwords/camera credentials.
- Camera navigation consumes server-authorized camera scope and live-role availability.
- Live-session lifecycle is generation-fenced and performs renderer cleanup on stop/switch/logout/unload/application close.
- Client state uses per-user LocalAppData and is separate from server ProgramData.
- Packaging is a deterministic self-contained win-x64 field-test ZIP plus SHA-256 and isolated per-user install/uninstall scripts; it is not claimed as the final commercial installer.
- Automated evidence includes native compile, deterministic positive/edge/negative tests, production-source security scan, package generation and executable smoke launch. Existing CI, Security, Ubuntu and Windows Server field-test workflows remain regression gates.
- F08-001/F08-007/F08-008 are promoted only to QA/software-evidenced; verification remains NV.
- Windows 10/11 hardware qualification, real WHEP/camera/codec validation, DPI/multi-monitor, GPU/native decoding, high-density grids, OIDC/PKCE interactive UX and final signed installer/update channel remain External Qualification Pending.
- Product status remains **Release Candidate / External Qualification Pending**.
