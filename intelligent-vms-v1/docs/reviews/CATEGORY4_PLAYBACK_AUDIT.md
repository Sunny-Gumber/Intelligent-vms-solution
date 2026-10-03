# Category 4 Playback and Export Audit — 2026-10-02

Starting main: `f5995091ddf7957e881d1e44819c3a3117f13c72`.

## Existing capability found

Before issue #299 the control API already provided:

- authorized bounded camera timeline;
- distributed recording-index lookup plus current-open-segment fallback;
- recording-node resolution for historical playback;
- MediaMTX playback/remux proxying with Range forwarding;
- authorized bounded MP4 export with continuous-coverage checks;
- cross-recording-node export rejection;
- browser playback functions and timeline rendering.

The principal product gap was reachability: `openPlayback()` had no normal
call site in the multi-camera live workspace, and the browser had no
date/time-oriented operator workflow.

The server play route also relied on MediaMTX behavior for local gap starts
rather than explicitly proving recording coverage itself.

## Exact catalog decision

| Feature | Exact wording | Decision |
|---|---|---|
| F04-001 | Single-/multi-camera playback | Not completed. Single-camera workflow is delivered; multi-camera playback is intentionally out of scope. |
| F04-002 | Playback by date/time | Selected and software-QA evidenced. |
| F04-003 | Timeline | Selected and software-QA evidenced. |
| F04-004 | Play/pause | Selected and software-QA evidenced through VMS controls plus the bounded HTML media element. |
| F04-011 | Clip selection | Selected and software-QA evidenced using a selected recorded span. |
| F04-014 | Start/end-time selection | Selected and software-QA evidenced for the selected clip interval. |
| F04-012 | Native/standard export (MP4, AVI) | Not completed. Existing authorized MP4 export is reused; AVI is not added. |

Other Category-4 rows remain outside this focused milestone.

## Security and correctness audit

- Timeline/play/export routes reauthorize the camera on each request.
- Tenant/site isolation continues through `authorized_camera`.
- Browser supplies no recording path, MediaMTX URL, node ID, filesystem path or
  camera credential.
- Timeline windows remain timezone-aware and bounded by
  `recording_query_max_window_hours` and segment count.
- Playback duration remains bounded by the existing API maximum.
- Playback validates real coverage and stops at the first gap.
- Distributed historical playback stays on the node owning the selected
  segment; one request does not cross a failover/node boundary.
- Export continues to require continuous finalized coverage on one recording
  node.
- Playback source cleanup is independent from live WHEP cleanup.
- Recording, retention, live-role, AI, snapshot and manual-recording state are
  not mutated.

## Edge decisions

No recording/date with no recording: explicit empty timeline.

Recording gap: visually empty and server refuses a start with no real coverage.

Multiple spans: all valid bounded spans render independently.

Rapid date/time/camera changes: generation-fenced.

Camera deletion or loss of authorization: current camera authorization is
required; historical media is not exposed through a deleted/out-of-scope camera
identity.

Retained historical recording: playable while the camera/policy identity still
exists and the responsible historical recording node is resolvable. The route
does not require the policy to remain enabled.

Current open distributed segment: may play after current-node coverage is
confirmed; export still follows finalized-index requirements.

## Windows portability

The milestone adds browser/API time and media control only. It adds no
filesystem path, Linux shell, signal, temp-file, Docker-socket or case-sensitive
path dependency. Existing product-level Windows installation blockers remain
unchanged and Windows packaging is not started here.

## Qualification boundary

Product status remains **Release Candidate / External Qualification Pending**.
This milestone is not evidence for real camera/browser codec compatibility,
measured capacity, Windows support, production failover UX or long-duration
operator soak.
