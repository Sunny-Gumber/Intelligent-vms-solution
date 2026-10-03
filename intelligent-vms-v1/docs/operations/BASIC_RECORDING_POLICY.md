# Basic Continuous Recording and Retention

## Scope

This milestone exposes the existing per-camera continuous-recording policy to the
field-test browser client. It covers F03-001 Continuous and F03-013 Retention
settings at the deterministic software-QA boundary.

It does not add another recorder or ingest path.

## Operator workflow

1. Assign a camera to the live grid and make that tile active.
2. The browser reads the authorized camera recording policy.
3. The operator may enable continuous recording, disable it, or set retention
   from 1 through 3650 whole days.
4. Every write re-reads the current server policy immediately before PUT and
   preserves the current part duration, segment duration and maximum part size.
5. A missing policy (HTTP 404) is treated as not configured. Authorization,
   server and media errors remain explicit failures and are not converted to an
   empty policy.
6. UI results are generation-fenced to the active camera so a late response
   cannot repaint controls for a different selected tile.

The controls describe configured policy, not measured recorder health. Existing
recording-gap and operational metrics remain the health source of truth.

## Recording invariants

- Continuous recording remains MAIN/source-copy authority.
- Active live MAIN/SUB/THIRD selection cannot change recording source.
- AI source selection cannot change recording source.
- Manual recording remains durable interval intent over continuous MAIN
  recording and still reuses the bounded recording-backed export path.
- No browser MediaRecorder, FFmpeg recorder, second RTSP ingest, client-supplied
  filesystem path or public recording URL is introduced.
- Retention continues to map to MediaMTX recordDeleteAfter.
- Existing segmentation limits remain server policy, not UI defaults after an
  existing policy has been created.

## Manual-recording safety

Disabling continuous recording while an unexpired manual-recording session is
ACTIVE is rejected with HTTP 409. Manual Start and recording-policy mutation
serialize on the same recording-policy row lock. This prevents a concurrent
Start/Disable race from silently cutting the authoritative media beneath a
durable manual interval.

An expired manual interval does not block disable merely because lazy cleanup
has not yet rewritten its state.

## Failure behavior

- invalid retention is rejected in the browser and again by the API schema;
- unsupported event/scheduled recording remains rejected by the server;
- a stale active-camera response is ignored;
- a failed policy write does not mutate live-view role, AI policy, manual
  history or recording-export state;
- recorder application failures roll back the policy transaction according to
  the existing recording router contract.

## Windows portability

This UI/API milestone is platform-neutral. It does not claim native Windows
server support. Current recording/storage defaults still include POSIX-oriented
paths and the current operational deployment path remains Linux/container
oriented; those are tracked as a later platform milestone.

## Qualification boundary

Software QA can prove policy authorization wiring, input bounds, MAIN/source
separation, field preservation, concurrency fencing and deterministic failure
contracts. Real camera codecs, MediaMTX behavior across devices, retention
deletion timing on target filesystems, storage exhaustion behavior, browser
interoperability, long-duration stability and measured camera/storage capacity
remain External Qualification Pending.
