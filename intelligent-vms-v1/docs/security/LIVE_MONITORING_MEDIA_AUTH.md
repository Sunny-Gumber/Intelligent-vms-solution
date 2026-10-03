# Live Monitoring Media Authorization

Category-2 live video is tenant data. Camera API authorization alone is not sufficient because browsers read WebRTC/HLS directly from regional MediaMTX nodes.

## Trust path

1. The authenticated client requests `POST /api/v1/live/cameras/{camera_id}/access`.
2. The control API loads the camera through the existing tenant/site authorization boundary.
3. The API returns credential-free WebRTC/HLS endpoints plus a short-lived bearer grant bound to exactly one MediaMTX path.
4. The browser sends the grant in the HTTP `Authorization: Bearer` header. It is never appended to the media URL.
5. MediaMTX validates the RS256 grant locally. Its JWT permission contains exactly one `read` action for the authorized path.
6. MediaMTX obtains public verification keys from `GET /internal/v1/media/jwks`; private keys never leave the control API. MediaMTX v1.21.1 refreshes cached JWKS hourly unless its trusted internal `POST /v3/auth/jwks/refresh` endpoint is called.

MediaMTX API, metrics and pprof administrative actions are excluded from this callback because they remain private infrastructure endpoints. Playback is consumed through the tenant-authorized control API and is excluded from this Category-2 callback; the Compose topology no longer publishes the MediaMTX playback port to the host.

## Configuration

Set `LIVE_VIEW_TOKEN_PRIVATE_KEY_B64` to the base64-encoded PEM RSA private key (2048+ bits) that signs new grants. `LIVE_VIEW_TOKEN_VERIFICATION_PUBLIC_KEYS_B64_JSON` is a JSON array of at most four base64-encoded PEM **public** RSA keys used only for bounded rotation overlap. The active signing key is published automatically and must not also appear in the overlap list; duplicate `kid` values fail closed. `kid` remains the deterministic first 32 hex characters of SHA-256 over DER SubjectPublicKeyInfo. `LIVE_VIEW_TOKEN_TTL_SECONDS` defaults to 60 and is bounded to 15–300 seconds.

Production OIDC posture fails startup when the active signer or overlap keyring is malformed, weak, duplicated or unsafe.

Every regional MediaMTX serving browser traffic must use equivalent external-auth configuration:

```yaml
authMethod: jwt
authJWTJWKS: http://control-api:8000/internal/v1/media/jwks
authJWTIssuer: intelligent-vms-control
authJWTAudience: intelligent-vms-live
authJWTExclude:
  - action: api
  - action: metrics
  - action: pprof
  - action: playback
```

The JWKS contains only public key material. MediaMTX caches it and validates viewer grants locally, keeping HLS segment authorization off the control-plane hot path. Do not disable TLS verification for browser-facing media. Production browser-facing WebRTC/HLS should be behind the deployment's approved TLS termination.

## Browser behavior

The development web client uses WHEP with the bearer grant in request headers. It performs authenticated ICE-server discovery, creates a receive-only peer connection, and explicitly tears down known sessions on refresh/page exit. It does not put the grant into query strings, DOM text or media URLs. WHEP session `Location` responses are accepted only when they remain on the authorized media origin, preventing bearer leakage during teardown.

## Failure behavior

- Missing/malformed/weak private signing key: token issuance fails closed; production OIDC startup also fails.
- Malformed/expired/wrong-issuer/wrong-audience token: media read denied by MediaMTX.
- Wrong path: denied by the single exact-path `mediamtx_permissions` claim.
- Publish attempt: denied because the grant contains read permission only.
- Camera outside caller tenant/site: camera remains non-enumerable and no grant is minted.
- Media/control outage: new live sessions fail; recording ownership/policy is unchanged.
- Media-node restart: existing desired-state reconciliation remains responsible for camera paths.
- Viewer disconnect: browser closes the peer connection and attempts WHEP session deletion.

Grant expiry prevents reuse for new authorization. It does not forcibly terminate an already established WebRTC session. Active-session revocation, viewer quotas and measured concurrency are separate Category-2 gaps.

## Recording invariant

This milestone does not change camera main/sub/third source selection, recording path construction, recording policy, recording placement, generation/lease fencing, or AI source selection. Recording remains a separate main-stream/source-copy concern and must continue if a live-view grant or viewer session fails.

## Qualification boundary

CI proves token/path/action validation and configuration intent only. Real camera/browser codec interoperability, HLS behavior, ICE/TURN/NAT traversal, TLS deployment, viewer concurrency, failover visual continuity and soak behavior remain External Qualification Pending / NV.


## Signing-key rotation runbook

MediaMTX v1.21.1 caches JWKS for up to one hour. Its internal `POST /v3/auth/jwks/refresh` invalidates that cache; the next JWT authorization fetches the current JWKS. A control-API rollout alone does not prove every media node trusts the new key.

1. Generate a new RSA private key (2048+ bits) outside the repository; keep the current signer active.
2. Add only the new **public** PEM (base64 encoded) to `LIVE_VIEW_TOKEN_VERIFICATION_PUBLIC_KEYS_B64_JSON`, roll control-api, and verify JWKS publishes old+new distinct `kid` values with no private fields.
3. On every browser-facing MediaMTX node, call trusted internal `POST /v3/auth/jwks/refresh`, trigger an authorization, and evidence that the staged keyset was fetched. Do not switch signer until every node passes.
4. Switch `LIVE_VIEW_TOKEN_PRIVATE_KEY_B64` to the new private key and retain the retiring key's public key in the overlap list. Roll control-api. New grants use the new `kid`; old grants remain verifiable.
5. Refresh every MediaMTX node again and verify one old still-valid grant and one new grant on their authorized paths.
6. Wait at least `LIVE_VIEW_TOKEN_TTL_SECONDS` from the last old-key issuance plus deployment clock-skew/rollout safety. Remove the retiring public key, roll control-api, refresh every node, and verify the retired `kid` is absent/rejected.
7. Destroy/archive the retired private key under the deployment secret-management policy. Never place private material in the verification list.

Rollback before retirement: restore the previous signer while both public keys remain published, refresh every media node, then verify grants. After retirement, re-publish the old public key before any deliberate rollback to that signer.

Emergency compromise: replace the signer, omit the compromised public key, roll control-api, force refresh on every media node, and verify the compromised `kid` is rejected. This intentionally invalidates outstanding grants from that key. Already-established WebRTC sessions are not forcibly terminated by JWT key rotation.

Rotation is complete only with refresh evidence from every regional node. Partial refresh can produce node-dependent authorization failures. Never claim immediate global revocation from a control-api key change alone.

## Recording clip export boundary (#290)

Clip export is not a live-media grant and does not broaden #278 JWT permissions. The authenticated control API reauthorizes the admin/operator against the camera, resolves record_stream_key and recording node server-side, validates a bounded timezone-aware interval and continuous recording coverage, then proxies MP4 from the internal playback service as an attachment. Clients cannot submit RTSP/HTTP/file sources, MediaMTX paths, filesystem paths, storage keys or remux flags. There is no public export URL, export ID, FFmpeg/shell process or temporary server file. Per-process concurrency, duration and I/O timeout are safety guardrails; they are not cluster-wide capacity claims.
