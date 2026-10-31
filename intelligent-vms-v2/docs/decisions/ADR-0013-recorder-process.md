# ADR-0013: Recording runs in a separate OS process

**Status:** Accepted · **Date:** 2026-10-19

## Context

Recording is the function a surveillance system must never lose. It is also
the function most exposed to hostile input: arbitrary RTSP streams from
devices of varying quality, decoded by a large C codebase.

## Decision

Recording runs as its own OS process (`services/recording-service`, a systemd
unit or Windows service) that reads desired state from the `recording_tasks`
table, claims cameras with a heartbeat, and supervises one FFmpeg child per
camera.

## Alternatives rejected

- **Recording inside the API process.** An FFmpeg crash, a file-descriptor
  leak or a zombie child would degrade the API. Worse, restarting the API to
  deploy a UI fix would interrupt recording.
- **One process per camera managed by systemd.** Clean isolation, but at 256
  cameras that is 256 units to template, monitor and upgrade, with no shared
  backoff state.

## Consequences

**Good**
- Recording survives an API restart or upgrade.
- An FFmpeg fault is contained to one camera's supervisor, which restarts it
  with bounded, jittered backoff.
- The claim-and-heartbeat model is the growth path to multiple recorder hosts
  without a schema change. A stale claim (no heartbeat for 120 s) can be
  stolen by another worker.

**Bad / accepted cost**
- Desired state is communicated through the database, so a configuration
  change takes up to one reconcile interval (10 s) to take effect.
- Two processes must share the master key file to decrypt camera credentials,
  so filesystem permissions matter more.
- Split-brain is possible if a worker is paused (e.g. a VM snapshot) past the
  staleness window and then resumes. Mitigated by the heartbeat check before
  each claim, but not eliminated. This is a known residual risk.
