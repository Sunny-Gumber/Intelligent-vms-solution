# ADR-0015 — Media gateway authorization and active revocation

- **Status:** Proposed. Implemented against baseline `83e965d9`; real-gateway
  verification outstanding (see §11). Supersedes the provisional Batch 1 ADR.
- **Findings:** A (gateway authorization), Batch 1 review items R1–R10.
- **Pinned gateway:** MediaMTX **v1.21.2**, with an immutable multi-architecture
  image digest — see §10.

## 1. Problem

The control plane minted play tokens (`SessionBroker.open`) and could verify
them (`SessionBroker.verify_play_token`), but nothing called the verifier, and
the shipped `mediamtx.yml` left MediaMTX on its default internal user list.
Anyone who knew a path name could read live video straight from the gateway.
Issuing a token that the media plane never checks protects nothing.

## 2. Facts relied on, and their status

| # | Fact | Source | Status |
|---|---|---|---|
| F1 | `authMethod: http` POSTs a JSON payload (`user`, `password`, `token`, `ip`, `action`, `path`, `protocol`, `id`, `query`, `userAgent`) for each authentication request; a 2xx admits, anything else denies | Official docs, *Authentication* — https://mediamtx.org/docs/features/authentication | Documented (current docs). Field population per protocol on v1.21.2: **runtime-verify** |
| F2 | Empty user/password requests are normal for RTSP; the hook must answer 401 so the client sends credentials | Same page | Documented |
| F3 | Actions are `publish`, `read`, `playback`, `api`, `metrics`, `pprof` | Same page | Documented |
| F4 | Up to v1.20.0 `authHTTPExclude` defaulted to excluding `api`, `metrics`, `pprof`; v1.20.1 changed the default to exclude nothing | Release notes v1.20.1 (#6103) — https://github.com/bluenviron/mediamtx/releases ; issue #5730 — https://github.com/bluenviron/mediamtx/issues/5730 | Documented. We set `[]` explicitly regardless |
| F5 | Any parameter can be overridden with `MTX_<PARAMNAME>` environment variables | Official docs, *Configuration* — https://mediamtx.org/docs/features/configuration | Documented |
| F6 | `--validate-conf` exists from v1.21.0 | Release notes v1.21.0 (#5549/#6140) | Documented |
| F7 | v1.21.2 restricts playback-server access when legacy credentials (`readUser`/`readPass`/`publishUser`/`publishPass`) are configured | Release notes v1.21.2 (#6311) | Documented |
| F8 | Leftover pre-1.6 auth keys caused the internal user list to be ignored | Maintainer reply, discussion #3258 — https://github.com/bluenviron/mediamtx/discussions/3258 | Reported for v1.7/1.8 only. Treated as a hazard; **not** asserted for v1.21.2 |
| F9 | Go's `net/http` client sends URL userinfo as HTTP Basic | Go standard library behaviour | MediaMTX's use of it for `authHTTPAddress`: **runtime-verify**. If not sent, the hook denies everything (fail closed) |
| F10 | `Authorization: Bearer` reaches the hook in `token`; Basic reaches it in `user`/`password` | Inferred from F1 | **runtime-verify** per protocol |
| F11 | WebRTC/RTSP sessions are kickable by id at `/v3/{webrtcsessions,rtspsessions,rtspssessions}/kick/{id}`; the auth `id` equals that session id | Existing client code + API naming | **runtime-verify** (the harness asserts it for RTSP) |
| F12 | Whether HLS re-authenticates every playlist/segment request | — | **Unknown.** Determines whether HLS revocation exists at all (§9) |

Everything marked *runtime-verify* is exercised by
`tests/gateway/test_mediamtx_gateway.py`, which has **not run** (§11).

## 3. Decision

**`authMethod: http`, with the control API as the only admission authority,
at `POST /internal/media/auth` on the existing listener (port 8080).**

The hook re-evaluates, at connection time, against the database: the session
(open, unexpired, token hash match, *path match*), the user (active,
unlocked), the camera (exists, enabled) and the user's current RBAC and
site/camera scope. It uses the same `build_principal` / `may_access_camera`
rules as the REST API.

### Alternatives

- **Internal user list.** Would require writing session credentials into the
  gateway config on every open/close. Rejected: configuration churn, and
  per-session scope cannot be expressed.
- **JWT/JWKS.** Viable. The provisional ADR wrongly called it incompatible
  with revocation. Admission-time expiry and revocation of established
  connections are separate problems for *both* methods: neither terminates a
  running WebRTC/RTSP session by itself. JWT would avoid a per-connection
  round trip but needs a signing key, a JWKS endpoint, and either very short
  lifetimes or a server-side denylist to honour logout and permission change
  at admission. HTTP was chosen because the existing opaque, hash-stored play
  token and the live database checks are reused unchanged. Revisit if
  per-connection latency is measured to be a problem.

## 4. Credential model

| Party | Credential | Transport |
|---|---|---|
| Gateway → hook | Basic `mediamtx:<VMS_MEDIA_AUTH_HOOK_SECRET>` **and** TCP peer in `VMS_MEDIA_AUTH_ALLOWED_CALLERS` | Userinfo in `MTX_AUTHHTTPADDRESS` (F9), internal network only |
| Viewer → gateway (WebRTC, HLS) | Play token | `Authorization: Bearer <token>` or `Basic <session_id>:<token>` header |
| Viewer → gateway (RTSP) | Play token | RTSP Basic after the 401 challenge (`rtspAuthMethods: [basic]`) |
| Control API → gateway API | `VMS_MEDIA_GATEWAY_USER/PASSWORD` | Basic; admitted by the hook as `action=api` |

**Play tokens** are the existing opaque values from `new_opaque_token(24)`,
stored only as a keyed hash, valid for `live_session_ttl_s` (default **300 s**)
and extended by keepalive. They are **not** signed JWTs and carry no audience
claim. Media and REST authority are separated structurally: REST accepts only
HS256 JWTs (`decode_access_token`), the hook accepts only tokens whose hash is
in `live_sessions`. Each is tested to reject the other.

**URL policy.** The hook denies any read whose query string carries
credential-like keys (`token`, `pass`, `jwt`, …), even if the token is valid.
Clients must use headers.

**TTL does not end established connections.** Expiry is checked at
admission only. Ending a running viewer requires §6.

The payload `ip` field is never used for trust. The hook reads the TCP peer
address; uvicorn's `forwarded_allow_ips` now defaults to `127.0.0.1`
(configurable) instead of `*`, so `X-Forwarded-For` cannot rewrite it.

## 5. Fail closed

204 or 401 only, empty body. Missing application state, invalid trust
configuration, malformed payloads, database errors and unexpected exceptions
all yield 401. Logs record a reason code and, for exceptions, the exception
*type* — never exception text, tokens, passwords or the query string.
Production startup refuses to continue if `Settings.media_auth_problems()` is
non-empty; other environments start, log an error, and deny all media.

If the control API is down, nobody new can watch. Recording is unaffected:
the recorder pulls cameras directly.

## 6. Active revocation

`media_connections` (migration `0002_media_connections`) records each admitted
WebRTC/RTSP gateway session against its live session. A supervised loop
(`media-revocation`, every `media_revocation_interval_s`, default 5 s, critical)
runs `MediaConnectionRegistry.enforce()`. Bounded scans persist each binding's
last policy-check time so later bindings are not starved by repeatedly checking
the same authorized rows:

1. Marks bindings whose live session is closed, expired or missing, or whose
   user no longer passes the live-view check (that live session is also closed
   with reason `permission_revoked`).
2. Kicks each marked binding **by gateway session id**. Other viewers of the
   same path are untouched.
3. Retries failures up to `media_kick_max_attempts`, then gives up loudly
   (`media.revocation_kick_abandoned`).
4. Purges closed bindings after `media_connection_retention_s`.

Each cycle starts at most `min(media_revocation_batch,
media_revocation_kick_concurrency)` kicks concurrently. Each request has a
`media_revocation_kick_timeout_s` deadline, so one slow gateway call cannot
block the loop indefinitely. A saturated kick queue is processed over later
cycles; timeouts and exhausted retries can leave an existing stream active
until the gateway responds.

`SessionBroker.close()` kicks immediately; synchronous paths (logout,
deactivation, deletion, permission edits) are picked up by the scan. Under a
backlog of `N` open bindings, policy-check latency can approach
`ceil(N / media_revocation_batch)` intervals, plus kick time; the 5 s interval
is not a fixed revocation-latency guarantee. A kick backlog adds cycles at the
configured concurrency, and the gateway runtime must be verified separately.
Because state is in the database, a restart resumes outstanding revocations.

Bounds: at most `media_connection_max_per_session` open bindings per session
(older ones are superseded and kicked); per-cycle work capped by
`media_revocation_batch`. A revoked connection id cannot be re-admitted, and
an id cannot be re-bound to another session.

Pre-existing, unchanged: `close_for_camera` (camera deletion, credential
change, replacement) still kicks *all* readers of the path, which is correct
there.

## 7. Configuration hazards

- `authHTTPExclude: []` is explicit (F4).
- No legacy per-path credential keys and no `authInternalUsers` anywhere (F7,
  F8). `MediaGatewayClient.ensure_path` now refuses `read_user`/`read_pass`.
- `authHTTPAddress` in the file is a fail-closed placeholder; the real value
  (with the secret) is injected by `MTX_AUTHHTTPADDRESS`.
- `tests/unit/test_deploy_config.py` asserts these statically. Static checks
  do not replace `--validate-conf` or an empirical unauthenticated-read test.

## 8. Network and administration boundary (Compose)

- `media-admin`: `internal: true`, subnet `172.31.250.0/24`. MediaMTX has a
  fixed address `172.31.250.10`; the Control API listens only there
  (`MTX_APIADDRESS`). Ports 9997/9998 are never published.
- The control API joins `media-admin` with alias `control-api-internal`; the
  hook URL uses that alias, so hook traffic stays on the internal network and
  arrives from `172.31.250.10`, the only allowed caller.
- `VMS_TRUSTED_HOSTS` always includes `control-api-internal` and `localhost`.
- Excluding the route from OpenAPI is documentation hygiene, not security.

## 9. Protocols and release consequences

| Protocol | Admission | Per-viewer revocation | Release consequence |
|---|---|---|---|
| WebRTC/WHEP | Hook | Kick by session id | Needs a browser check on the pinned build (no headless WHEP client in harness) |
| RTSP | Hook, Basic only | Kick by session id | Token is cleartext on plain RTSP; expose 8554 only on trusted networks or use RTSPS |
| HLS | Hook | **None** — muxer is shared by all viewers of a path | Until F12 is verified, revocation for HLS viewers is **not provided**. If HLS re-auths every request, revocation takes effect at the next request; if not, a revoked HLS viewer keeps watching until they leave. Blocks any claim of complete revocation |
| RTMP, SRT, playback | Disabled | — | — |

## 10. Version pinning

`docker-compose.yml` pins `bluenviron/mediamtx:1.21.2` to the immutable
multi-architecture manifest digest
`sha256:c7540465f868a02652118bea5a0daab12a29ac0f6cb8485abd535939ecddf886`,
resolved from the official registry. This pins the container image but does
not substitute for runtime qualification of the contained binary.

## 11. Evidence

| Class | Status |
|---|---|
| Unit/integration against real DB, broker, RBAC, route (gateway replaced by a labelled test double) | PASS — see `docs/verification/batch1-test-run.txt` |
| Static deployment-config invariants | PASS |
| Real pinned-gateway harness (`tests/gateway/`) | **NOT RUN — BLOCKED** (no MediaMTX binary obtainable offline) |
| Browser WHEP check | NOT RUN |
| Real camera | NOT RUN |
