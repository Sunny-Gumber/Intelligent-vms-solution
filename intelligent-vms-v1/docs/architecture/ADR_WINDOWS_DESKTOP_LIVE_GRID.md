# ADR — Native Windows Multi-Camera Live Grid Foundation

**Milestone:** issue #10 / PR #11
**Status:** Implemented; pending exact-head acceptance.
**Product status:** Release Candidate / External Qualification Pending.

## Decision

The Windows desktop client uses a native WPF layout shell with a reusable LiveGridCoordinator and independent LiveTileView controls. The coordinator owns logical session lifecycle; each tile owns one replaceable ILiveMediaRenderer.

Supported deterministic layouts are 1 view (1x1), 4 views (2x2), 9 views (3x3), and 16 views (4x4). The 16-view implementation is architecture/software evidence only, not a workstation capacity, smoothness, CPU, memory, GPU, codec, or 4K qualification claim.

## Server/media boundary

The desktop remains a client of the accepted VMS architecture:

authorized camera inventory -> existing live-grant API -> short-lived path-scoped grant -> MediaMTX WHEP -> tile renderer.

The grid never requests credential-bearing camera RTSP, stores camera credentials, creates a recorder, changes recording policy, or creates a second media backend. Recording remains server-authoritative and independent of desktop live failure.

## Tile/session model

Each tile has an explicit state: Empty, Loading, Connecting, Live, Offline, Unauthorized, Failed, or Stopping. It tracks only safe client state: tile index, authorized camera ID/name/site, requested/actual stream role, generation, and safe error category.

LiveGridCoordinator centralizes tile registration, assignment, duplicate prevention, layout transitions, role changes, focus mode, per-tile generation/cancellation fencing, cleanup, and safe assignment restore. A single tile failure remains tile-local. Session expiry is a central auth failure and tears down the secured grid.

## Duplicate policy

The foundation deliberately prevents the same camera from being assigned to more than one tile. This avoids accidental duplicate WHEP readers and unnecessary server/media load.

## Stream policy

Role selection consumes only server-advertised available_live_roles.

- multi-tile grid: prefer SUB;
- focused or 1-view: prefer MAIN;
- if preferred role is unavailable: use MAIN when advertised, otherwise the first advertised MAIN/SUB/THIRD role;
- explicit role changes are rejected unless advertised.

No stream capability is fabricated client-side.

## Focus behavior

Focus is an in-window single-tile mode, not an OS-level fullscreen claim. Entering focus stops other visible sessions while preserving safe assignments and prefers MAIN on the focused tile. Returning to the grid restarts assigned visible tiles and prefers SUB where available. Hidden focus-mode sessions are therefore not intentionally left running.

## Concurrency and fencing

The architecture supports the visible 16 logical tiles while bounding simultaneous connection establishment to four by default. This is load shaping, not a capacity claim.

Every assignment, role change, layout stop, and clear fences the tile generation and cancels prior connection work. Late completion cannot install older tile state. Existing LiveSessionController generation fencing remains underneath the coordinator.

There is intentionally no automatic reconnect loop in this milestone. Failure is explicit/operator-driven, avoiding grant/WHEP retry storms.

## Renderer abstraction

ILiveMediaRenderer remains the media boundary and now reports state changes. Current LiveMediaView uses WebView2 as a replaceable media-only WHEP renderer. Local virtual-host navigation remains restricted, new windows are blocked, permissions denied, host objects disabled, bearer grants stay in WHEP Authorization headers, WHEP Location stays same-origin, and stop performs WHEP DELETE/peer cleanup.

The grid/session architecture does not depend on WebView2 APIs, leaving a replacement path for native/hardware-accelerated decoding.

## Persistence and restart

Client-local live-grid.json stores only server profile ID, layout, selected tile, and camera IDs. It never stores grants, WHEP URLs, tokens, Authorization headers, or camera credentials.

Restart sequence is conservative: protected auth restore/validation -> capabilities -> authorized camera inventory -> layout metadata restore. Only still-authorized camera IDs are restored. Media is not auto-started after process restart; authenticated operator action starts live media.

## Performance boundary

One WebView2 renderer per initialized tile has meaningful process/memory/compositor cost. Sixteen controls do not prove sixteen smooth streams. SUB preference is an architecture policy, not measured optimization. Focus MAIN, bounded connection establishment, and renderer abstraction preserve a path to visibility prioritization and native/GPU decoding.

Real H.264/H.265 cameras, 1/4/9/16 simultaneous streams, CPU/RAM/GPU, packet loss, low bandwidth, long soak, DPI, and multi-monitor behavior remain external qualification.

## Qualification boundary

Feature evidence remains QA/software evidence. No Production Qualified, Windows 10/11 qualified, GPU qualified, 4K grid, or 16-stream performance claim is made.
