# Category 2 — Live Video Monitoring Evidence Audit

**Audit date:** 2026-10-01  
**Starting main:** `dc95569387f04b187ad3fba61fd048ec8c8faf60`  
**Parent:** #221  
**First implementation milestone:** #277  
**Qualification:** Release Candidate / External Qualification Pending

This audit traces all 21 deterministic Category-2 Feature IDs. UI presence is not treated as proof. Real camera, browser, NAT/TURN, WAN, codec and viewer-capacity claims remain NV until external evidence exists.

## Actual live-media architecture

The control plane owns camera identity, tenant/site authorization, placement and MediaMTX path orchestration. Camera RTSP is pulled by MediaMTX. The preferred substream (or main when no substream exists) feeds the normal live path; recording remains a separate main-stream `-record` path; an optional third profile has its own path. In distributed mode, the camera is placed on a regional media node and browser-facing WebRTC/HLS bases come from the assigned node. Live media is therefore media-server proxied and may be distributed-node backed; it is not normally browser-to-camera.

Before #277, camera API access was scoped but MediaMTX live reads were anonymous. A stable path leaked or guessed outside the API could therefore bypass tenant/site authorization. #277 is the P0 prerequisite: short-lived exact-path grants plus MediaMTX external authorization, while browser media remains direct to the media node and recording is untouched.

## Row audit

| Feature ID | Feature | Classification | Existing evidence / path | Gap / qualification |
|---|---|---|---|---|
| F02-001 | Single-camera view | PARTIAL → #277 software QA | Camera API, MediaMTX WebRTC/HLS, development web wall | P0 media-read authorization was missing; #277 adds path-scoped grants. Real browser/camera evidence remains NV. |
| F02-002 | 2×2 / 3×3 / 4×4 / custom layouts | PARTIAL | Responsive CSS grid in `web/index.html` | No selectable fixed/custom/saved layout model or tests. |
| F02-003 | Full-screen display | PARTIAL | Existing MediaMTX iframe allowed fullscreen | No VMS-owned tested fullscreen workflow; #277 replaces anonymous iframe transport. |
| F02-004 | Digital zoom | MISSING | None | Needs client-side digital PTZ/zoom without camera PTZ mutation. |
| F02-005 | Live snapshot | MISSING | None | Needs bounded browser/client snapshot and evidence handling. |
| F02-006 | Manual recording | MISSING | Continuous recording policy exists | Existing Record button changes 24×7 server recording policy; it is not an operator manual clip recording feature. |
| F02-007 | Start/stop live view | PARTIAL | Source-on-demand MediaMTX; #277 WHEP session lifecycle | Explicit operator start/stop UX and deterministic browser lifecycle tests remain incomplete. |
| F02-008 | Drag-and-drop cameras | MISSING | None | Requires layout interaction/state. |
| F02-009 | Camera sequence/tour | MISSING | None | Requires bounded sequence/timer/session lifecycle. |
| F02-010 | Previous/next camera | MISSING | None | Requires selected-view navigation model. |
| F02-011 | Aspect-ratio control | MISSING | Browser video defaults only | No explicit fit/fill/native aspect control. |
| F02-012 | Stream-quality selection | PARTIAL | Main/sub/third source roles exist; third URLs added in Category 1 | Live defaults to sub/main; no authorized client quality selector/adaptive policy. |
| F02-013 | Audio listening | PARTIAL / DEVICE-DEPENDENT | WebRTC media path can carry audio; viewer starts muted | No named camera/audio-codec evidence or dedicated tested control. |
| F02-014 | Two-way audio | DEVICE-DEPENDENT / NV | None | No ONVIF backchannel/vendor talk path; requires device evidence. |
| F02-015 | Microphone/speaker control | DEVICE-DEPENDENT / NV | Basic browser media controls only | No VMS talkback/device control implementation or hardware evidence. |
| F02-016 | Instant playback | PARTIAL | Authorized recording timeline/play API | No live-to-instant-playback UX/contract with defined look-back behavior. |
| F02-017 | Live pause/resume | PARTIAL | Browser player controls | No tested VMS pause/resume semantics or resource policy. |
| F02-018 | Camera/recording/motion/alarm status indicators | PARTIAL | Camera health + recording badge; events/alarms panels | Motion/alarm state is not composed into per-camera live status. |
| F02-019 | Date/time and camera-name display | PARTIAL | Camera name in card; event/playback timestamps | No explicit live date/time overlay contract; camera OSD is device-dependent and separate. |
| F02-020 | Local snapshot/recording storage | MISSING | Server recording exists | No client-local snapshot/manual recording store. |
| F02-021 | Snapshot and clip download | PARTIAL | Authorized MP4 playback stream exists | No live snapshot download; no explicit bounded clip-export workflow in Category 2. |

## Primary classification summary

- Implemented + fully evidenced at audit start: **0**
- Partial: **11**
- Missing: **8**
- Device-dependent / NV primary classification: **2**
- Blocked by an unavailable external prerequisite: **0**

Several partial rows also require device or external qualification. All Category-2 rows remain NV for final real-world verification.

## Security and failure findings

### P0 — direct media authorization
The camera API enforced tenant/site scope, but browser-reachable MediaMTX reads did not. Stable stream keys are identifiers, not authorization credentials. #277 addresses this before UX expansion.

### P1 — browser/session lifecycle
The development UI previously created an iframe per camera. Explicit viewer resource limits, per-user/session quotas and measured viewer capacity do not exist. #277 adds explicit WHEP session teardown in the development client, but quota/capacity work remains separate.

### P1 — transport/deployment evidence
WebRTC/HLS are configured, but production NAT/TURN, TLS termination, browser compatibility and WAN behavior require deployment evidence. No Internet/NAT traversal claim is made.

### P2 — stream selection
Recording correctly remains main-stream/source-copy independent. Live view prefers substream, falling back to main; third stream exists. User-controlled quality selection and adaptive selection are not implemented.

### P2/P3 — operator live UX
Layouts, zoom, snapshot, manual clip recording, tours, navigation, aspect controls and composed status are incomplete or absent.

### P4 — audio/talkback
Listening is only a partial transport capability. Two-way audio and microphone/speaker controls require protocol/device implementation and named hardware evidence.

## Failure analysis

Camera/RTSP loss affects MediaMTX live path health but must not alter recording policy. Media-node restart is handled by desired-state reconciliation. Distributed ownership uses existing placement generation/lease/fencing and stale-path cleanup. Source/profile/credential refresh uses the existing source-mutation/reconciliation path. #277 does not modify any recording source, recording assignment or AI path.

Viewer sessions are on-demand MediaMTX resources. #277 grants are short-lived and path-bound; expiry prevents new authorization with an old grant but does not forcibly terminate an already established WebRTC session. Active-session revocation/quota enforcement is a later live-session-control gap and must not be claimed here.

## Prioritized gaps

1. **P0:** enforce tenant/site-derived authorization at MediaMTX live read boundary — #277.
2. **P1 completed in #279 / PR #281 (software boundary):** bounded per-live-path MediaMTX reader safety ceiling, stale-policy reconciliation and lifecycle/observability documentation. Identity-aware cross-camera principal/tenant quotas and measured node capacity remain unclaimed.
3. **P2 next:** selectable fixed/custom grid layouts plus selected-camera navigation/drag-drop.
4. **P2:** explicit main/sub/third stream-quality selection without changing recording.
5. **P2:** snapshot and manual/local clip capture/download.
6. **P3:** digital zoom, aspect control, fullscreen and live pause UX.
7. **P3:** composed camera/recording/motion/alarm status and live timestamp/name presentation.
8. **P4:** audio listening qualification and two-way audio/device controls.
9. **P5:** adaptive/low-bandwidth selection, NAT/TURN deployment and capacity optimization (with measured evidence).

## External qualification boundary

Real camera codecs/audio, WebRTC/HLS interoperability, TURN/NAT traversal, TLS termination, WAN behavior, simultaneous-viewer capacity, failover visual continuity and long-duration soak are not proven by this audit or CI. They remain External Qualification Pending / NV.


## #279 bounded live-view resource milestone

PR #281 merged at `2b1587e17edf218c0c57d13b88fc7b82194edb1c` from accepted head `718c8abaada5793f0f1a56b0a0028de994580c0d`. Live and third MediaMTX paths now receive a configurable non-zero reader ceiling, and reconciliation repairs missing/stale policy on existing legacy or distributed media-node paths. Recording path configuration remains unchanged. The ceiling is a safety guardrail, not measured capacity. Token replay can still create additional readers while a grant is valid, but not beyond the path ceiling. Established-session revocation, exact peer cleanup timing, per-principal/per-tenant cross-camera quotas, real browser/device behavior and viewer capacity remain NV / External Qualification Pending. Safe live signing-key rotation is tracked separately in #280.

## #283 bounded multi-camera live-view grid foundation

The pre-implementation re-audit found that the development client already had secure single-camera WHEP mechanics but automatically instantiated every authorized camera in an auto-fit card list. It had no explicit fixed-layout model, tile assignment, active tile, duplicate prevention, stale-operation fencing or layout-shrink lifecycle.

#283 therefore implements the smallest dependency-correct workspace foundation: fixed 1x1/2x2/3x3/4x4 layouts, authorized camera assignment, accidental duplicate prevention, active tile, independent per-tile WHEP lifecycle, explicit stop/remove and manual retry, generation-fenced asynchronous completion, and cleanup on replacement/layout shrink/page exit. It reuses #278 authorization, #281 maxReaders and #282 signing/JWKS behavior without adding another media/auth subsystem.

Evidence classification after this software milestone:
- F02-001 single-camera view: **IMPLEMENTED + SOFTWARE-EVIDENCED**, final real-world verification remains NV.
- F02-002 fixed 1x1/2x2/3x3/4x4 layouts: **IMPLEMENTED + SOFTWARE-EVIDENCED** for fixed layouts; custom/saved layouts remain missing/out of scope and final real-world verification remains NV.
- F02-007 explicit start/stop live view: **IMPLEMENTED + SOFTWARE-EVIDENCED** at tile lifecycle level; browser-crash cleanup timing remains External Qualification Pending.
- F02-008 drag-and-drop cameras: **PARTIAL**. Accessible selector-based assignment exists; drag/drop itself remains unimplemented.
- F02-010 previous/next camera: **MISSING**. Active-tile/navigation foundation exists, but previous/next controls remain a later UX item.
- F02-012 quality selection: **PARTIAL**, unchanged; grid uses the existing default live path and does not alter recording.
- F02-018 status indicators: **PARTIAL**. Tile connection/health state is present; composed recording/motion/alarm state remains incomplete.

A 4x4 layout is not a sixteen-stream capacity claim. Real browser/GPU decode capacity, cameras/codecs, TURN/NAT/WAN, reader cleanup timing, failover continuity and soak remain **NV / External Qualification Pending**.

## #285 active-tile MAIN / SUB / THIRD live stream-role selection

PR #286 merged from accepted head `6295249da4684b165f6665ee8e0f282ab1ef1fd2`. F02-012 explicit stream-role selection is now **IMPLEMENTED + SOFTWARE-EVIDENCED** while final device/browser qualification remains NV. MAIN is always available; SUB and THIRD are exposed only from authoritative configured camera paths. Default live behavior remains SUB-if-present, otherwise MAIN. Explicit MAIN receives its own bounded derived live path only when SUB exists. Each role request is mapped server-side after tenant/site camera authorization and receives one exact-path #278 grant; #281 maxReaders applies to every viewer path. #284 generation fencing and cleanup protect rapid role/camera/layout changes. Recording stays on its independent MAIN-source record path and AI stream-role policy is not mutated. Review found and fixed distributed deletion cleanup for the derived MAIN path. Physical camera stream support, codecs, performance, TURN/NAT/WAN and soak remain External Qualification Pending / NV.

## #288 secure active-tile live snapshot

PR #289 merged from accepted head `74a1e7ed61a97946e153997accf5dbd96e2b23ea`. The four-row audit separated snapshot from manual clip/export. F02-005 is now **IMPLEMENTED + SOFTWARE-EVIDENCED** with final browser/device verification NV. F02-020 is **PARTIAL** because local PNG snapshot download exists but local manual-recording storage does not. F02-021 remains **PARTIAL** because snapshot download exists and authorized recording playback already exists, but explicit bounded clip export does not. F02-006 remains **MISSING/TARGET** pending a separate recording-backed manual clip/export milestone.

Snapshot captures the already-authorized decoded frame from the active tile's exact selected MAIN/SUB/THIRD role. It adds no new reader, server media target, FFmpeg/subprocess, temp file or persistent evidence store. Camera/role/generation/session are revalidated after asynchronous PNG encoding; stale completion cannot be mislabeled. Continuous recording and AI are unchanged. Browser MediaRecorder is explicitly rejected as the default future clip architecture; the existing authorized bounded MAIN recording/playback path is the preferred dependency for clip export.

## #290 bounded recording-range clip export

PR #291 merged from accepted head `eed94803a91811b0f24528bf128cc2471193636b`. The audit did not equate F02-006 "Manual recording" with clip export. F02-006 remains **MISSING/TARGET** pending a separate durable Start/Stop intent workflow. F02-020 local snapshot/recording storage is now **IMPLEMENTED + SOFTWARE-EVIDENCED** for browser-local PNG snapshot and bounded MP4 recording clip downloads; final verification remains NV. F02-021 snapshot and clip download is now **IMPLEMENTED + SOFTWARE-EVIDENCED** at the software boundary; final codec/browser/scale qualification remains NV.

Clip export uses authoritative continuous MAIN/source-copy recording via server-resolved record_stream_key and MediaMTX playback. It requires admin/operator camera authorization, timezone-aware bounded intervals and continuous recording coverage. Gaps fail explicitly. Distributed ranges must be finalized/indexed and remain on one recording node; cross-node/failover ranges are rejected rather than silently truncated or stitched. Default safety bounds are 900 seconds, four concurrent exports per control-api process and a 30-second export I/O timeout; none is a measured capacity claim and the concurrency ceiling is not cluster-wide.

No browser MediaRecorder, second ingest/recorder, FFmpeg/shell, temp export file, public download URL, arbitrary media URL/path or client storage key was added. Recording/live/AI/snapshot configuration remains independent. Frame-exact boundaries, real codec/audio MP4 remux compatibility, cross-node stitching and production export scale remain External Qualification Pending / NV.

## #292 durable recording-backed Start/Stop manual recording

PR #293 merged from accepted head `b4e24632260be80c3200196c82e24ceada2d8aa0`. F02-006 is now **IMPLEMENTED + SOFTWARE-EVIDENCED / QA** while final verification remains NV. Start persists a server-UTC durable interval intent only when continuous authoritative recording is active; Stop atomically finalizes the interval and never stops the recorder. Manual download reuses #291 and therefore always derives from authoritative MAIN recording regardless of displayed MAIN/SUB/THIRD role.

Shared DB state plus partial unique constraint provide one ACTIVE session per tenant/site/camera/operator across replicas. The immutable max_stop_at captured at Start bounds abandoned sessions; API observation deterministically materializes expiry. Creator ownership is enforced for ordinary operators, with authorized admin override. Refresh/recent APIs recover active/history state and retry the same finalized interval. Camera deletion finalizes ACTIVE sessions and preserves historical metadata with a null camera reference; export then fails 410.

No second RTSP ingest/recorder, browser MediaRecorder, FFmpeg recorder, temp media store or new download path was introduced. Recording gaps and recording-node/failover boundaries retain #291 explicit failure semantics. Continuous recording, live WHEP/roles/maxReaders, AI and snapshot remain independent.

Real browser/device behavior, codec/audio MP4 remux, frame-exact boundaries, production concurrency/long-duration/WAN behavior, cross-node stitching and legal admissibility remain **NV / External Qualification Pending**.
