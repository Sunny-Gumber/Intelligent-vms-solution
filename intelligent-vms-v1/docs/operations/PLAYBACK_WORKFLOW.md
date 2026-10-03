# Reachable Date/Time Timeline Playback

Issue: #299

## Scope

This milestone makes the existing authoritative recording timeline, playback and
MP4 clip-export path reachable from the normal active-camera browser workflow.

Completed catalog scope:

- F04-002 — Playback by date/time
- F04-003 — Timeline
- F04-004 — Play/pause
- F04-011 — Clip selection
- F04-014 — Start/end-time selection

F04-001 is not claimed complete because its exact catalog wording is
"Single-/multi-camera playback" and this milestone intentionally provides only
single-camera playback. F04-012 is not claimed complete because the current
standard export is MP4 only, not MP4 plus AVI.

## Operator workflow

1. Select an authorized active live camera.
2. Choose **Playback** on the active tile.
3. Choose local date and time.
4. Load the authorized day timeline.
5. Inspect actual available recording spans; gaps remain visually empty.
6. Click inside one recorded span or use the chosen date/time when it falls
   inside a span.
7. Play from that recorded time. The native media controls provide seek within
   the bounded response and explicit VMS Play/Pause controls remain available.
8. Choose clip start/end within the selected recorded span.
9. Download through the existing authorized recording export endpoint.
10. Return to live without changing or restarting the live WHEP session.

## Server authority and gap behavior

Every timeline, play and export request identifies only the camera and time
range. The server reauthorizes the camera, resolves recording policy/path and
recording-node placement, and chooses the internal MediaMTX playback client.

The play endpoint now validates real recording coverage before streaming. On a
single/local recorder it can continue only through contiguous spans and stops
at the first gap. In distributed mode a request is bounded to the recording
segment/node containing the selected start. If no completed indexed segment
contains the start, the current recorder is queried and must prove current
coverage before playback begins.

The server never constructs synthetic continuity across a recording gap or
recording-node/failover boundary.

## Time and UI concurrency

Browser date/time inputs represent operator-local time and are converted to
timezone-aware ISO timestamps for the API. The day window is produced from
local midnight to the next local midnight, including timezone offset/DST
semantics.

Timeline requests are generation-fenced. A late response after a new date/time
selection or active-camera change cannot repaint the current playback
workspace. Changing the active camera closes the old playback workspace rather
than allowing stale camera identity to remain visible.

Changing selected playback time resets the prior media source before a new
request. Browser Range seeking remains proxied by the existing playback route.
Closing playback, camera authorization loss and page exit detach the playback
media source. Live sessions are managed separately.

## Clip selection

Clip start/end inputs are constrained in the browser to the currently selected
recorded span and the existing bounded playback window. The export request sends
only camera ID, ISO start and duration. The existing export backend remains
authoritative for configured export duration, finalized coverage, concurrency,
timeout and cross-recording-node rejection.

Current/open distributed footage may be playable before its completion hook is
indexed; clip export still requires the finalized coverage required by the
existing export contract.

## Isolation invariants

Playback does not:

- mutate continuous recording policy or retention;
- alter MAIN/SUB/THIRD live selection;
- alter AI source/policy;
- create a camera ingest or recorder;
- mutate manual recording sessions;
- mutate snapshot state;
- accept a recording path, node URL, RTSP credential or filesystem path from
  the browser.

No database migration or new dependency is required.

## Qualification boundary

Deterministic software QA verifies authorization wiring, bounded/gap-safe server
selection, UI race fencing and reuse of the existing export path. Physical
camera codecs, browser decoder behavior, long-duration seeking, failover user
experience, storage behavior, real workstation capacity and Windows deployment
remain **External Qualification Pending**.
