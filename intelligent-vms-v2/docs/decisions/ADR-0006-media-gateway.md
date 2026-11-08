# ADR-0006: Delegate media transport to MediaMTX; isolate FFmpeg as a subprocess

**Status:** Accepted · **Date:** 2026-10-19

## Context

A VMS must deliver live video to browsers (which cannot play RTSP) and to
native clients, and must write 24/7 recordings. The three obvious options are:

1. Implement RTSP/RTP/WebRTC transport ourselves.
2. Link against FFmpeg's libraries (libav*) in-process.
3. Use a proven media gateway process and invoke FFmpeg as a subprocess.

## Decision

**Option 3.** MediaMTX (MIT licensed) pulls from cameras and republishes as
WebRTC (WHEP) and HLS for browsers and RTSP for native clients. The control
API only programs gateway paths and issues short-lived play tokens. The
recorder invokes FFmpeg as a **separate executable process** and never links
its libraries.

## Alternatives rejected

- **Writing our own transport stack.** RTP jitter buffers, WebRTC ICE/DTLS and
  H.265 packetisation are years of work with a long tail of vendor quirks.
  Nothing about a VMS product is differentiated by reimplementing them.
- **Linking libav* in-process.** A malformed stream from one camera could
  crash the API process and stop the entire system. It also creates a direct
  derivative-work relationship with FFmpeg, making LGPL/GPL compliance far
  harder to reason about for a commercial product.

## Consequences

**Good**
- Camera credentials stay between the gateway and the camera. Clients receive
  a gateway URL and a token, never an RTSP URL — asserted by test.
- A gateway crash cannot corrupt the database or stop recording, because the
  recorder pulls from the camera directly.
- Invoking FFmpeg as a separate executable keeps it at arm's length. This is a
  deliberate licensing posture, **not legal advice** — have counsel confirm
  your specific FFmpeg build and codec/patent obligations before shipping
  binaries.

**Bad / accepted cost**
- An extra process to install, configure and monitor.
- Live view depends on the gateway being up. This is handled explicitly: the
  API returns `503 media_gateway_unavailable` with the message "Recording and
  playback are unaffected", rather than hanging. Verified by
  `test_live_session_fails_clearly_without_gateway`.
- Latency is bounded by the gateway's buffering, which we do not control.
- We inherit MediaMTX's control-API surface and must track its versions.
