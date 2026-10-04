# ADR — Windows Desktop Playback Foundation

**Milestone:** issue #12 / PR #13  
**Status:** Implemented; pending final exact-head acceptance.  
**Product status:** Release Candidate / External Qualification Pending.

## Decision

The native Windows client reuses the existing Intelligent VMS recording truth and playback/export APIs. It does not create a playback database, recorder, filesystem browser, RTSP player, or desktop-only playback authority.

The accepted path is:

authorized camera → `/api/v1/recordings/cameras/{id}/timeline` → actual bounded recording spans → `/play` MP4 media → replaceable `IPlaybackMediaRenderer`.

Clip export reuses the existing authorized `/export` endpoint.

## State and timeline model

`PlaybackCoordinator` owns one camera/day/session and explicit states: Idle, LoadingAvailability, Ready, Starting, Playing, Paused, Seeking, Ended, Stopping, Unavailable and Failed.

Timeline spans are clipped to the selected day, malformed ranges are ignored, overlapping/touching coverage is normalized, and real gaps remain gaps. A seek into a gap stops active playback, keeps the requested position visible and reports **No recording**. It never silently jumps to another segment.

Camera/date/profile changes fence pending work and clear incompatible playback context.

## Time policy

API/storage timestamps remain offset-aware UTC. The selected operator day is converted through an explicit `TimeZoneInfo` into UTC query bounds; display position converts back through that same zone. Invalid and ambiguous local seek times are rejected rather than guessed. Deterministic tests cover 23-hour and 25-hour DST days.

The current UI uses the Windows client local timezone because site timezone metadata is not currently exposed in the camera/server contract. Site-timezone presentation remains future work.

## Renderer

Playback uses a separate `IPlaybackMediaRenderer` from live WHEP rendering.

The foundation renderer is a restricted packaged WebView2 page with an HTML5 video element. The media URL contains only server/camera/start/duration/format. The native WebView2 host injects the current bearer Authorization header only for the active VMS origin and expected playback path. Tokens are not supplied to page JavaScript or URL query strings.

Playback position comes from actual `video.currentTime`, translated from the requested recording start. Renderer and coordinator generation/session fencing reject stale seek/open completion.

The renderer remains replaceable by a future native/GPU decoder.

## Rate and end policy

Only **1×** is advertised in this foundation. Although the abstraction supports rate commands, no fast/slow qualification is claimed until the accepted media path is externally tested.

Media `ended` transitions to Ended. The client does not loop or auto-jump across a recording gap.

## Clip/export policy

Clip start/end must lie within one continuous displayed recording span. The desktop sends the range to the existing server export endpoint. The server remains authoritative for continuous coverage, node-boundary, authorization and export limits.

## Live/playback resource policy

Entering Playback stops hidden live sessions. Leaving Playback stops active playback. Logout, session expiry, server/profile switch and application close tear down playback state/media.

## Qualification boundary

This is deterministic software evidence for a single-camera playback foundation. It does not qualify Windows 10/11, H.264/H.265 interoperability, 4K, hardware decoding, high-speed playback, long-duration stability, corrupted media, storage/NAS/RAID, or synchronized multi-camera playback.
