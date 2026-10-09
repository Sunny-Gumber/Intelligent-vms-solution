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
at the first gap. In distributed mode the recording index selects the completed
segment that contains the start. Containment is half-open and matches the
recording fence: the segment start is inside, and the segment end is outside,
including one microsecond past the end. The index applies that predicate before
its limit, so a long history cannot push the covering segment off the page.

If several completed segments contain the start after a placement move, playback
uses the one with the latest segment start, then the greatest segment id. The
current placement is not consulted for that choice. A start that falls only in
an older owner's segment is played from that older node. A start in a gap has
no covering segment. Playback then asks the current recorder and continues only
when that recorder's own timeline proves coverage. A gap never silently plays
the current node, and one request never crosses a recording-node boundary.

The server never constructs synthetic continuity across a recording gap or
recording-node/failover boundary.

## Upstream timeouts

Ordinary playback bounds the recorder connection. The connect timeout defaults
to 5 seconds (`RECORDING_PLAYBACK_CONNECT_TIMEOUT_SECONDS`) and the read
timeout defaults to 30 seconds (`RECORDING_PLAYBACK_READ_TIMEOUT_SECONDS`).
The read timeout is the longest silence allowed while waiting for response
headers or the next body chunk. That same limit covers write and pool, so no
phase waits forever. Both values must be finite and greater than zero, and
neither may exceed 300 seconds.

A recorder that accepts and never answers, or that answers and then stalls
before the first playback byte, ends as HTTP 504. A stall after bytes have
started, or a client disconnect, closes the upstream response and client
instead of leaving the connection open. Export does not use these two
settings. It still applies `recording_export_io_timeout_seconds` (default 30
seconds, allowed range 1 through 300) as one timeout for every phase.

The timeline body stays a JSON array. Malformed recording-index rows are
skipped, counted, and reported with `X-VMS-Partial` and `X-VMS-Skipped-Rows`.
A timeline that stops at `recording_query_max_segments` while another segment
may remain also sets `X-VMS-Partial: true`. `X-VMS-Skipped-Rows` stays the
malformed-row count. Export still rejects a range the returned page does not
cover continuously.
See [Search and index page contract](../architecture/SEARCH_INDEX_PAGE_CONTRACT.md).

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
