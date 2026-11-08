# ADR-0009 — Native WPF desktop client plus a React web client

**Status:** Accepted

## Context

The requirements are explicit: the Windows client "must be a real application,
not simply a browser wrapper presented as a native client". Control-room
operators need multi-monitor walls and many simultaneous tiles; administrators
want zero-install browser access.

## Decision

Two clients with different jobs:
- **Windows (.NET 8 / WPF, MVVM)** — the operator client. Multi-monitor, many
  tiles, keyboard shortcuts, native video rendering via LibVLCSharp.
- **React + TypeScript** — the administration and light-monitoring client.
  Browser live view uses WebRTC/WHEP, never raw RTSP (browsers cannot play
  RTSP, and pretending otherwise is a common VMS lie).

## Consequences

**Costs:** two UI codebases. Operators on macOS or Linux get only the web
client. Hardware-accelerated decode in the WPF client is **designed but not
implemented** in this release (M3-R16).

**Verification status — important:** neither client has been compiled in this
build environment. No .NET SDK and no Node toolchain were available. The source
is delivered; `dotnet build` and `npm run build` are Gate 4 items. Every client
requirement is therefore marked "Partial — source delivered, not built" in the
feature matrix rather than claimed as working.
