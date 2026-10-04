# Windows Desktop Playback Foundation QA

Issue #12 / PR #13.

## Automated software evidence

Deterministic Windows Client tests cover:
- overlapping/duplicate/malformed span normalization and real gaps;
- selected-day UTC bounds;
- 23-hour DST-forward and 25-hour DST-back days;
- Ready → Starting/Playing → Paused → Playing → Seeking → Playing → Stopping/Ready behavior;
- explicit gap seek without silent time jump;
- rapid seek generation fencing;
- session-expiry preflight;
- only supported 1× rate;
- clip start/end within one continuous recording span;
- reuse of authorized export provider;
- context reset for camera/date/profile transitions;
- playback URL same-origin/TLS/token-query rejection.

The Windows Client workflow additionally validates analyzers, dependency vulnerability audit, playback security source contracts, packaging and executable smoke launch.

## External QA still required

Real qualification must cover representative H.264/H.265 recordings, MediaMTX/browser codec behavior, long-duration play/seek, retention during playback, corrupted media, real export interoperability, private/proxy environments, Windows 10/11, 4K/hardware decoding and storage/network behavior.

No synchronized multi-camera playback evidence is claimed.


## Exact-head review additions

- WebView2 playback state events are now session/generation scoped; old handlers are detached before stop so delayed pause/end/error events cannot revive stale playback state.
- Playback timeline and export deterministic tests assert one bounded 401 refresh + one retry with the renewed bearer token, while 403 remains an authorization failure without refresh.
