# ADR-0001 — Modular monolith control plane, separate media and recording processes

**Status:** Accepted

## Context

The requirements call for modular services "where operational separation is
justified" while explicitly warning against unnecessary microservice
complexity. A VMS has one genuinely hard separation requirement: **a crash in
analytics or live streaming must never stop recording.**

## Decision

Three process boundaries, not fifteen:

1. **control-api** — identity, devices, scheduling, events, storage,
   administration. Internally modular (`app/devices`, `app/recording`,
   `app/events_engine`, …) with strict import direction, but one process.
2. **recording-service** — supervises one FFmpeg process per recorded stream.
   Separate because an FFmpeg crash or a storage stall must not take the API
   down, and because recorders scale horizontally per server.
3. **media gateway (MediaMTX)** — a third-party process that handles RTSP and
   WebRTC transport.

Analytics workers are a fourth boundary when enabled.

## Consequences

**Good:** one database transaction boundary for control-plane operations; no
distributed-transaction problem for camera deletion; a developer can run the
whole control plane with one command; recording survives API restarts.

**Costs:** the control plane scales vertically first. Horizontal scaling needs
the federation design (ADR deferred, and honestly marked unimplemented). The
modular boundaries are enforced by convention and review, not by the runtime,
so they can erode without discipline.
