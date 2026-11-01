# ADR-0016: Continuing event authorization and browser media ownership

Status: implemented in the V2 draft branch; runtime qualification pending.

## Event protocol

`/api/v1/events/stream` rejects query parameters. Browser clients send exactly
`{"type":"authenticate","token":"<access token>"}` as their first text frame.
The server acknowledges with `connected` only after checking the token, its
revocation record, current account state, LIVE_VIEW permission and camera scope.
Production requires WSS and a same-origin or explicitly configured CORS origin.
Native clients may omit Origin but must authenticate identically.

Defaults per application worker: 256 connections, 16 per canonical peer IP,
five-second first-frame and send deadlines, 8 KiB transport message limit,
four transport queue entries, no WebSocket compression, and a 256-event queue.
Admission includes unauthenticated connections. Use the `app.main` launcher so
Uvicorn receives the transport and trusted-proxy settings. Direct custom ASGI
launchers must carry these settings explicitly. These counters do not enforce
cluster-wide limits; deployments behind NAT must size peer limits deliberately.

Authorization is reloaded in a fresh database transaction at intervals no longer
than five seconds, including idle connections. Expiry is checked before sends;
queued camera events are checked against the refreshed scope. Database work runs
off the event loop. This is periodic enforcement, not a hard five-second SLA:
database latency, a send already in progress and scheduler delays add latency.
Database exceptions close the socket; exception messages and tokens are not logged.
Camera-less events retain the REST API policy: LIVE_VIEW permission is required,
but there is no camera scope to apply. This is not an administrator-only feed.

Close codes: 4408 requests token refresh; 4401/4403 and 1008 are terminal client
authentication/protocol failures; 4429 is admission exhaustion; 1011 is a server
failure. Disconnect and cancellation unsubscribe and cancel tasks. Slow or full
queues may drop events; the feed is not a durable alarm delivery mechanism.

The browser waits for acknowledgement, monitors heartbeats, retries transient
failures with bounded jittered backoff, and cancels work on disposal/logout.
An authentication generation prevents late requests or refreshes from restoring
an old login or crossing into a newer identity.

## Canonical client addresses

Audit and rate-limit code uses `request.client.host` only. Uvicorn interprets
forwarding headers solely from `VMS_FORWARDED_ALLOW_IPS`; wildcard trust is refused
in production. Configure exact proxy peers/networks and preserve trusted Host and
scheme information at the reverse proxy. An untrusted request header must never
choose an audit identity or rate-limit bucket.

## Live tile ownership

Each tile owns one session attempt, peer, HLS player and its timers/listeners.
Disposal releases its control session and WHEP resource; a late acquisition or
WHEP response releases only the resource it created. Cleanup is idempotent.
Keepalives are serialized, failure tears down playback, and transient failures
have at most three retries. Control requests have a 15-second timeout. Release
requests are best effort; server expiry is the backstop for network failure.

WHEP waits for complete ICE gathering and sends the gathered SDP without trickle
ICE. The Location resource is deleted on teardown. Tokens are headers only.
The tile enters `playing` only on the video element's actual playing event.
The implementation supplies no TURN service; restrictive NAT/firewall traversal
remains a deployment/qualification concern.

Authenticated HLS fallback uses pinned hls.js and MSE; native HLS is not used
because this client cannot attach its required authorization headers there.
Each HLS request gets a bearer header. Initial URLs and WHEP resource URLs must
be credential-free HTTPS (loopback HTTP is allowed). HLS resources must share
the endpoint origin; only validated `_HLS_msn`, `_HLS_part`, `_HLS_skip` query
parameters plus the gateway cookie-check flag and a validated gateway session
UUID are allowed. These gateway-generated session URLs are sensitive and must
not be logged or shared; API play tokens remain forbidden in URLs. The deployment must serve media without cross-origin
redirects and allow the application's Origin/Authorization through CORS.
Browser behavior and gateway CORS still need end-to-end verification.

## Evidence and boundaries

The repository includes backend authorization/transport regressions and Node
client lifecycle tests with controlled peers, media elements and HTTP responses.
They do not prove browser decoding or real-camera interoperability. An initially failing real-gateway regression established that HLS authorization
is session-based. The hook now binds each HLS connection ID and revocation calls
`/v3/hls/sessions/kick/{id}`, never the shared muxer. The gateway
suite uses a real pinned MediaMTX binary and synthetic FFmpeg camera, including
selective HLS session revocation and denial of subsequent playlist/segment reads. Revocation cannot retract already buffered media.

Windows playback, real browsers, physical cameras, signing, penetration tests,
endurance and failover qualification remain separate gates. This change does not
implement enterprise roles/workspaces, arbitrary wall layouts, NVR archive
adapters, distributed recording or active-active failover. PR #177 remains draft.
