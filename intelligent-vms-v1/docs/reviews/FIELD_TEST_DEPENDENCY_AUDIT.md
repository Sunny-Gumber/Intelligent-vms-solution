# First Field-Test VMS Dependency Audit — 2026-10-02

Starting repository baseline for this audit:
`90c623980b93ed0a9aada542e3c045769a58d00a`.

The live repository is authoritative over this document if later work has
merged.

## Category 2 remaining gaps

Category 2 already software-evidences path-scoped live authorization, bounded
live readers, fixed 1x1/2x2/3x3/4x4 grids, explicit MAIN/SUB/THIRD selection,
snapshot/download, recording-backed clip export and durable manual recording.

Remaining market-program gaps include fullscreen ownership, digital zoom,
camera tours, previous/next navigation, explicit aspect-ratio controls, audio
controls/talkback, instant-playback UX, live pause semantics, fully composed
recording/motion/alarm tile indicators and an explicit live date/time display.
These remain catalog work, but none precedes continuous recording/retention in
the first-field-test dependency order.

## Category 3 and 4 field-test dependency audit

Category 3 has a working continuous MAIN recorder, per-camera retention,
recording reconciliation and recording-gap telemetry. The multi-camera browser
client, however, lost the old recording-policy call site and therefore had no
reachable continuous-recording or retention control. The old dead helper also
hard-coded recorder segmentation values. F03-001/F03-013 are therefore the
next dependency-correct milestone.

Category 4 has working backend timeline, bounded playback, selected-span MP4
export and browser playback functions, but `openPlayback()` currently has no
UI call site after the live-grid refactor. Playback is therefore still a
first-field-test blocker. Dependency order places recording/retention before
playback, so the next recommended milestone after F03-001/F03-013 is a focused
F04-001/F04-002/F04-003 reachable single-camera date/time timeline-playback
workflow using the existing backend rather than a new playback subsystem.

## First field-test dependency status

| Requirement | Software state at audit | Field-test implication |
|---|---|---|
| Login/RBAC and tenant/site isolation | Existing | External deployment validation still required |
| Camera onboarding and health | Existing | Real camera/site qualification pending |
| Multi-camera live view and stream role selection | Existing | Real browser/device/capacity qualification pending |
| Continuous MAIN recording | Backend exists; browser control missing | Selected milestone #297 |
| Retention basics | Backend exists; browser control missing | Selected milestone #297 |
| Playback/timeline | Backend/browser functions exist; UI entrypoint missing | Next core blocker after #297 |
| Snapshot, manual recording, clip download | Existing software-QA paths | External codec/browser qualification pending |
| Reconnect/recovery | Reconciler and manual live retry exist | Real outage/soak qualification pending |
| Logs/diagnostics | Existing operational/diagnostic paths | Installer/service integration still needed |
| Primary Linux installation path | Container/Linux oriented | Field package/install validation still needed |
| Windows server installation | Missing genuine service/installer path | Later platform blocker |
| Windows desktop application | Missing | Later client blocker |

## Windows portability blockers recorded

The audit found genuine deployment blockers rather than reasons to rewrite the
stable Linux architecture:

- default recording templates and node-agent mounts use `/recordings`;
- regional spool/fencing defaults use `/var/lib/...` paths;
- backup/restore operational lifecycle is currently Bash-oriented;
- compose/container deployment is the practical server path today;
- Windows service startup/restart, firewall, upgrade/rollback and recording
  volume semantics are not implemented as a genuine Windows installation;
- no real Windows desktop-client package exists yet.

New code in #297 does not add POSIX-only filesystem, signal, shell, Docker
socket or case-sensitive path assumptions.

## Selection decision

Issue #297 / branch `feat/f03-continuous-recording-retention` is selected for
F03-001 and F03-013 because it closes the earliest missing operator path needed
for a usable field-test recording workflow while reusing the accepted recorder.

Windows packaging does not begin in this milestone. Wrapping the current browser
before restoring recording and playback operator workflows would package known
core field-test gaps rather than remove them.

## Qualification boundary

This audit and milestone do not certify camera compatibility, Windows support,
browser/codec support, retention correctness on target storage, capacity,
failover continuity or production readiness. Product status remains
**Release Candidate / External Qualification Pending**.

## Post-#300 release-track update — 2026-10-02

- #297 / PR #298 closed the continuous-recording and retention browser blocker.
- #299 / PR #300 closed the reachable date/time timeline playback blocker for the focused single-camera workflow.
- The minimum core operator loop is now valuable enough to prioritize deployment qualification over another normal feature-category milestone.
- Live deployment audit selected #301: Ubuntu 22.04/24.04 x86_64 deterministic field-test baseline.
- Existing Compose and Phase-9 operations are reused. Missing release-layer items are fresh-machine bootstrap/preflight, private config generation, explicit deployed migration, restart/persistence contract, browser authentication reachability, minimal camera-create reachability, diagnostics, smoke workflow and Ubuntu matrix validation.
- Windows implementation remains deferred until Ubuntu baseline review/acceptance; the precise categorized inventory is in `docs/reviews/WINDOWS_PORTABILITY_BLOCKERS.md`.
- This transition does not change final verification: physical camera/browser/codec/storage/reboot/capacity evidence remains NV / External Qualification Pending.

- Ubuntu runtime validation also exposed an inherited static MediaMTX v1.21.1 configuration blocker: global path defaults combined the default publisher source with sourceOnDemand=true, which MediaMTX rejects as invalid. #301 removes only that invalid global default; dynamic live camera paths still set sourceOnDemand=true with an RTSP source and recording paths explicitly set sourceOnDemand=false.
