# Live-View Session Resource Policy

**Milestone:** #279  
**Parent:** #221  
**Product status:** Release Candidate / External Qualification Pending

## Purpose

Live-media authorization from #277 / PR #278 prevents unauthorized reads, but an authorized client can still replay a valid short-lived grant, open many tabs, or omit WHEP DELETE when a browser crashes. Live viewing is an expensive media-plane operation and must have a server-enforced safety boundary before multi-camera UI work expands concurrency.

## Authoritative lifecycle

The control API authorizes the camera and issues a short-lived RS256 grant. MediaMTX validates the grant and owns the actual reader/WebRTC/HLS lifecycle. The browser attempts explicit WHEP DELETE for fast normal cleanup, but browser cleanup is advisory: crash, network loss, process termination, or device sleep can prevent DELETE.

Grant expiry prevents a new authorization with an expired token. It does **not** terminate a WebRTC session that MediaMTX already established. A still-valid grant is replayable and can establish more than one reader until the media-plane reader ceiling is reached.

MediaMTX restart clears its in-memory reader/session state. Control-api restart does not clear or bypass the reader ceiling because the ceiling is part of each live path configuration on the media node. No duplicate durable control-plane active-session registry is introduced, so there are no VMS-owned stale session leases that can permanently consume quota.

## Safety boundary

The VMS provisions live and optional third-stream paths with MediaMTX `maxReaders` set from:

`LIVE_VIEW_MAX_READERS_PER_PATH`

The default is **16 readers per live path**. This is a conservative configurable **safety guardrail**, not a measured capacity claim, sizing recommendation, or certification. Operators must tune it only from deployment requirements and measured qualification evidence. The setting is validated as non-zero.

The limit is enforced atomically by MediaMTX where readers are added to the path. This avoids check-then-write races between control-api replicas and keeps control-api off the video packet path.

Recording paths deliberately do not receive this live-view ceiling. Continuous main-stream/source-copy recording, retention, recording placement, fencing and completion hooks are unchanged.

## Distributed behavior

Each assigned media node enforces the ceiling for the live path it owns. Reconciliation compares the configured `maxReaders` value and reapplies an existing live/third path when the safety policy is missing or stale, so rollout does not depend on waiting for a future source/placement change. Placement moves provision the path on the new owner with the same configured safety policy. Exact globally coordinated per-principal or per-tenant active-session quotas are **not** claimed by this milestone; those require identity-aware distributed accounting or a media gateway capable of enforcing that scope.

The total live resource surface is therefore bounded per provisioned live path, rather than unbounded per path. This milestone does not claim a globally measured node viewer capacity.

## Failure and cleanup behavior

- Normal browser refresh/navigation: client attempts WHEP DELETE and closes the peer connection.
- Browser crash / missing DELETE / network loss: MediaMTX owns peer detection and eventual cleanup; exact cleanup latency requires integration/soak evidence.
- Token expiry: blocks new use after expiry; does not revoke an established session.
- Token replay: allowed while valid, but additional readers are rejected after the path reaches `maxReaders`.
- MediaMTX restart: active sessions are lost and in-memory reader accounting resets with the process.
- Control-api restart: established media sessions remain media-plane owned; configured MediaMTX reader limits remain authoritative.
- Camera offline/reconnect: live may fail/recover; recording policy is not changed.
- Placement/source change: existing reconciliation remains authoritative; no new session database is involved.
- Duplicate cleanup: WHEP DELETE is best-effort; a missing/already-gone session must not alter recording state.

## Abuse resistance boundary

The media-plane ceiling prevents unlimited readers on one camera path even when a valid grant is replayed or many authorized users target the same camera. It does not by itself provide a strict per-user or per-tenant active-session quota across many cameras, nor a global measured node-capacity limit.

Grant issuance remains authenticated, camera-authorized, short-lived and RSA-signed. Dedicated distributed grant-request rate limiting is not introduced here because an in-process limiter would be bypassable across control-api replicas and a new coordination dependency would expand this focused milestone. Production ingress/API abuse controls and a future identity-aware quota milestone must address cross-camera principal/tenant fairness.

## Observability

MediaMTX 1.21.1 exposes native media-plane metrics including:

- `paths_readers{name,state,readerType}`
- `webrtc_sessions{id,path,state}`
- HLS session/muxer metrics

Operators should aggregate these at the media-node/tenant deployment boundary as appropriate. Do not add JWTs, WHEP secrets, camera credentials, or raw principal identities to metric labels. MediaMTX's raw per-session metrics are infrastructure telemetry and must remain on protected metrics endpoints.

## Key rotation residual

The current live JWT implementation publishes only the current RSA public key and does not yet provide a documented overlapping-key rotation procedure. That is a production security prerequisite, but it is independent of reader-resource enforcement and is intentionally not implemented in #279.

## Qualification boundary

CI can verify that live path provisioning carries a non-zero reader ceiling, recording provisioning does not inherit it, authorization remains path-scoped, and deployment documentation preserves the policy. CI does not prove browser/camera interoperability, peer cleanup timing, TURN/NAT/WAN reliability, simultaneous-viewer capacity, failover visual continuity, or long-duration soak behavior. Those remain NV / External Qualification Pending.
