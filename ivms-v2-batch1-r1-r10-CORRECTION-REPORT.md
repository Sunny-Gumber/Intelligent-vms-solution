# Intelligent VMS V2 — Batch 1 correction report (R1–R10)

**Supplied baseline:** `83e965d9c0445b842d84b384fc89441cb54502bf`. I rebuilt it from
`IVMS_V2_Claude_Batch1_Source_83e965d.txt`, and all **62/62 files matched their
declared SHA-256**.
**What it covers:** the selected subset only. I did not have GitHub access. Nothing
was pushed, merged or deployed.
**What it replaces:** the provisional Batch 1 package (`ivms-v2-batch1.zip`,
`51f20486…`). Withdraw all of its `reference/` files. They are superseded.

Status words: PASS / FAIL / NOT RUN / BLOCKED / PARTIAL.
Evidence classes: **[U]** = unit/integration test against the real DB, broker, RBAC
and route, with the gateway replaced by a labelled test double. **[S]** = static
config inspection. **[PG]** = real PostgreSQL. **[GW]** = real pinned MediaMTX.
**[CAM]** = real camera.

---

## 1. Environment and commands executed

- Python 3.12 venv. I installed the project's declared dependencies from PyPI
  (`fastapi 0.159.1`, `SQLAlchemy 2.1.6`, `alembic 1.20.1`, `pydantic 2.14.1`,
  `pydantic-settings 2.16.1`), plus `ruff 0.17.1`.
- GitHub, raw.githubusercontent, mediamtx.org and Docker Hub returned **HTTP 403**.
  So I could not get the MediaMTX binary, the image digest or the source code.
- `pgserver` (PyPI) gave me a real **PostgreSQL 16.2** server.

| Command | Result |
|---|---|
| `pytest tests/unit/test_media_gateway_admission.py tests/unit/test_deploy_config.py tests/unit/test_security_defaults.py tests/gateway -v -rA` | **PASS — 88 passed, 6 skipped** (`docs/verification/batch1-test-run.txt`) |
| Mutation checks: 9 security controls deliberately broken, one at a time, in throwaway copies | **PASS — 9/9 caught** (`docs/verification/batch1-mutation-checks.txt`) |
| `alembic upgrade head` → model/migration drift compare → `downgrade 0001_initial` → `upgrade head` on SQLite | **PASS**, drift NONE |
| Same cycle on PostgreSQL 16.2 | **PASS**, drift NONE (`docs/verification/batch1-postgres-migration.txt`) |
| `ruff check services packages tools tests` (the CI command) | **PASS (parity)**: 4 findings, the same 4 `I001` the untouched baseline subset produces under ruff 0.17.1. My changes add zero findings and zero suppressions |
| App with only the internal router → `openapi()["paths"]` | `[]` — the hook adds nothing to the contract |
| `git apply --check` + apply on the pristine baseline, then `diff -r` against the delivered tree | **PASS**, byte-identical |
| `tests/gateway/test_mediamtx_gateway.py` (6 tests) | **NOT RUN — BLOCKED** (no MediaMTX binary) |
| Full existing backend suite (161 tests) | **NOT RUN — BLOCKED**: the subset leaves out modules that `container.py` imports (`app.devices`, `app.events_engine`, …) |
| Web typecheck/build, Windows build | NOT RUN (out of Batch 1 scope; no frontend or Windows files changed) |

---

## 2. Findings

### R1 — Real application interfaces · **PASS [U]**

- **Original:** The reference read `app.state.session_broker/access_policy/connection_registry` and called `verify_play_token(token)`.
- **Source evidence:** `main.py` stores `app.state.container`. `container.sessions` is the `SessionBroker`. The real signature is `verify_play_token(session, path, token) -> LiveSession | None` (`session_broker.py`).
- **Root cause:** I designed the reference without the source.
- **Changed:** Added `app/api/internal/media_auth.py`, `app/media/media_auth.py`, `app/media/connection_registry.py`. Modified `main.py` and `session_broker.py`.
- **Decision:** The route reads `request.app.state.container` and opens its DB session through `app.db.session.session_scope`, the same module behind `deps.get_db`. It does not import `deps`, because that pulls in the whole container graph and would have kept the tests from running. It calls the real `verify_play_token(db, path, token)`, then reloads `User` and `Camera` and applies `build_principal` / `may_access_camera` / `site_ancestors`, the same rules `assert_camera_access` uses. The registry hangs off the broker (`broker.connections`), so `Container` needed no change.
- **Tests:** All tests in `test_media_gateway_admission.py` open sessions through the real `SessionBroker.open` against the Alembic-migrated DB.
- **Limitation:** I only verified the route through a minimal app plus the real router. `create_app` cannot import in this subset.

### R2 — Callback port · **PASS [S]**, runtime NOT RUN

- **Original:** The callback went to `control-api:8000`.
- **Evidence:** `Dockerfile.control-api` sets `VMS_API_PORT=8080` and `EXPOSE 8080`.
- **Decision:** The hook is `POST /internal/media/auth` on the existing 8080 listener, reached as `http://control-api-internal:8080/...`. `control-api-internal` is an alias that exists only on the internal network. Compose now always adds `control-api-internal,localhost` to `VMS_TRUSTED_HOSTS`. Otherwise `TrustedHostMiddleware` would reject the gateway's calls in production, and also the existing `localhost` healthcheck.
- **Test:** `test_hook_callback_matches_listener_route_and_trust` cross-checks the URL, alias, Dockerfile port, allowed-caller IP and route registration.

### R3 — Container admin connectivity · **PASS [S]**, runtime NOT RUN

- **Original:** The reference set `apiAddress: 127.0.0.1:9997`, which the control-API container cannot reach.
- **Decision:** `mediamtx.yml` keeps `apiAddress: :9997`. Compose adds a network `media-admin` (`internal: true`, `172.31.250.0/24`) and gives MediaMTX the fixed address `172.31.250.10`. `MTX_APIADDRESS=172.31.250.10:9997` binds the API to that interface only. `VMS_MEDIA_GATEWAY_URL=http://mediamtx-admin:9997`. Ports 9997 and 9998 stay unpublished, and a test enforces that.

### R4 — Management authorization · **PASS [U]**, runtime NOT RUN

- **Original:** With `authHTTPExclude: []`, the hook denied every action except `read`/`publish`, so the control plane's own API calls would have failed.
- **Decision:** `action=api` is admitted only with `VMS_MEDIA_GATEWAY_USER/PASSWORD`, compared in constant time. The existing `MediaGatewayClient` already sends these as Basic auth. Viewer tokens are refused for `api`. `publish`, `playback`, `metrics` and `pprof` are always denied. `ensure_path` now refuses legacy `read_user`/`read_pass`. MediaMTX v1.21.2 changed how legacy credentials behave.
- **Tests:** service credentials admitted; empty, wrong or other-user credentials denied; play token cannot administer the gateway; other actions denied. Path creation, inspection and targeted kicks against the real gateway are written in `tests/gateway/` but are **NOT RUN**.
- **Kick request construction:** tested against the real `MediaGatewayClient`, with an httpx `MockTransport` standing in for the network. Checked: method, URL `/v3/webrtcsessions/kick/{id}`, Basic header, 404 treated as already gone, 503 retryable, HLS and path-traversal ids rejected.

### R5 — Caller validation defaults · **PASS [U]**

- **Original:** An empty allowlist meant "trust everyone". There was no startup validation. Publishing was allowed by payload IP.
- **Decision:** The caller must present Basic `mediamtx:<VMS_MEDIA_AUTH_HOOK_SECRET>` **and** come from a TCP peer inside `VMS_MEDIA_AUTH_ALLOWED_CALLERS`. Neither the payload `ip` nor `X-Forwarded-For` counts. `Settings.media_auth_problems()` rejects: a missing or short secret (< 32 characters), an empty caller list, a `/0` or invalid CIDR, and missing gateway service credentials. On a problem, the hook denies everything. Production startup refuses to start (`lifecycle.startup`); other environments log an error. The publish bypass is removed. The shipped defaults are tested to be untrusted.
- **Related defect, fixed:** uvicorn ran with `forwarded_allow_ips="*"`. That let any client rewrite its source address. It is now `VMS_FORWARDED_ALLOW_IPS`, defaulting to `127.0.0.1`.
- **Tests:** no Basic auth, wrong secret, outside network with the right secret, spoofed `X-Forwarded-For` plus spoofed payload IP, six invalid-configuration cases.
- **Unverified (F9 in the ADR):** whether MediaMTX sends the `user:secret@` userinfo from `MTX_AUTHHTTPADDRESS` as Basic auth. If it does not, the result fails closed: no live video. The gateway harness detects this.

### R6 — Token model and URL policy · **PASS [U]**; ADR corrected

- **Original:** The ADR claimed signed JWT play tokens with an audience claim, a 60 s lifetime and at most one minute of exposure. None of that matched the code.
- **Evidence:** Play tokens come from `new_opaque_token(24)` and only a keyed hash is stored. `live_session_ttl_s=300`, extended by keepalive. `play_token_ttl_s=60` is never used.
- **Decision:** No token rewrite. Authority stays separated structurally (opaque hash lookup vs HS256 JWT). Accepted transports: `Authorization: Bearer` (payload `token`) or Basic `session_id:token` (`user`/`password`). The Basic username must equal the session id. **Query-string credentials are denied even when valid.** The ADR now says plainly that TTL does not end established connections.
- **Tests:** a REST JWT is rejected by the hook; a play token is rejected by `decode_access_token`; four query-credential forms are denied; harmless query parameters are allowed.
- **Compatibility:** `VideoTile.tsx` already sends `Authorization: Bearer`, so the web client is compatible. I have not seen the Windows client. If its LibVLC/HLS path puts the token in the URL, it will now be refused (see §5).

### R7 — Active revocation · **PARTIAL** (control plane PASS [U]; gateway effect NOT RUN [GW])

- **Original:** Only a Protocol and a test stub existed. Empty connection IDs were admitted. The "two-viewer" test just appended to a list.
- **Decision:**
  - Bindings are stored in the DB: new `media_connections` table, migration `0002_media_connections`. Bindings are unique per `(protocol, gateway_conn_id)`.
  - Bounded per session: `media_connection_max_per_session`, default 4. Older bindings are superseded and kicked.
  - WebRTC/RTSP reads **without a connection id are denied**.
  - Revoked ids cannot be re-admitted, and an id cannot be moved to another session.
  - A supervised `media-revocation` loop (every 5 s, critical) closes bindings for closed, expired or missing sessions. It also handles users who fail the live-view check, closing that session with reason `permission_revoked`.
  - Kicks go to **one gateway session id**, never a path.
  - Failed kicks are retried a bounded number of times, then abandoned with a loud log.
  - `SessionBroker.close()` kicks immediately. Synchronous paths (logout, deactivation, deletion, permission edits) are picked up on the next cycle.
  - Survives restart, because state is in the DB.
- **Tests [U]:** close kicks only viewer 1; logout from sync code; permission change; deactivation; expiry; restart (fresh registry instance); bounded retries; supersede bound; re-auth of a revoked id denied; rebinding denied; purge.
- **Defect found by my own test, and fixed:** the permission re-check could read stale scope rows from SQLAlchemy's identity map, because sessions use `expire_on_commit=False`. I now use `populate_existing=True` in both the hook and the loop. A dedicated test covers it, and mutation 1 proves the test catches the regression.
- **Not proven:** that a kicked viewer's video actually stops while the other keeps flowing. That is `test_revoking_one_viewer_leaves_the_other_playing` in `tests/gateway`, **NOT RUN**.
- **HLS:** HLS is not bound, because its muxer is shared by everyone on the path. **HLS revocation is not provided** unless the gateway re-authenticates every HLS request, which is unverified. This is documented as a release blocker for any "complete revocation" claim.
- **WebRTC:** WebRTC revocation needs a browser check. The harness drives RTSP only.

### R8 — Version pinning · **PARTIAL**

- **Original:** Compose used `bluenviron/mediamtx:latest`.
- **Done:** I pinned the tag `bluenviron/mediamtx:1.21.2`. Per the official releases page, that is the latest release. `mediamtx.yml` is written for that version's documented keys.
- **Not done:** I could not retrieve the image digest (Docker Hub returned 403), and I did not invent one. `tools/resolve_mediamtx_digest.sh` resolves it. A test rejects floating tags and accepts `X.Y.Z` or `X.Y.Z@sha256:<64 hex>`.
- **Not run:** `--validate-conf` against the pinned binary (it is the first test in the gateway harness).

### R9 — Error handling and secret hygiene · **PASS [U]**

- **Original:** Dependency reads sat outside the try block, so a missing dependency returned HTTP 500. `logger.exception` could leak secrets from exception text.
- **Decision:** Everything is inside one try block, and only 204 or 401 with an empty body ever comes back. Payloads are parsed strictly: an 8 KiB body cap, every field must be a string, 1 KiB per field, 256 characters per token. Logs carry a reason code and, for exceptions, only the exception **type**. They never include exception text, tokens, passwords or the query string. Denied requests roll back, so no partial binding is saved.
- **Tests:** five malformed bodies; missing container; broker exception carrying a synthetic token (checked across both the stdlib log capture and stdout); denials with the secret in the token, the query, and the hook password; a denied request leaves no binding.

### R10 — Evidence and ADR · **PASS**

- The tests live in the project (`tests/unit/`, `tests/gateway/`). They use the existing conftest fixtures and need no special `PYTHONPATH`. A new `gateway` marker is registered in `pytest.ini`.
- I rewrote the ADR at `docs/decisions/ADR-0012-media-gateway-authorization.md`. Its facts table cites official, version-specific sources and marks each fact as documented or **runtime-verify**.
- The ADR corrects the JWT revocation claim: admission expiry and active revocation are separate for both methods.
- It treats the legacy-key hazard as reported only for v1.7/1.8.
- It treats HLS segment authorization and payload field population per protocol as unverified.

---

## 3. Other defects found and changed

| ID | Defect | Change |
|---|---|---|
| D1 | `VMS_TRUSTED_HOSTS=vms.example.com`, exactly as in the shipped `.env.example`, **crashed settings load**. The same applies to every CSV list env var: pydantic-settings JSON-decodes them before the CSV validator runs. Reproduced. | `NoDecode` on list settings. The validator accepts CSV or JSON. Floor raised to `pydantic-settings>=2.7` |
| D2 | `forwarded_allow_ips="*"` | Configurable; defaults to `127.0.0.1` |
| D3 | Compose never passed `VMS_TRUSTED_HOSTS` to the control API | Now passed, with the internal names appended |

**Found but not fixed (out of Batch 1):** `deps.client_ip()` reads `X-Forwarded-For` straight from the request. So audit IPs and rate-limit keys are client-controlled. I recorded this in `LIMITATIONS.md`.

---

## 4. Security-sensitive behaviour changes

1. Live video now needs a valid, open, path-matching session **and** current permission. With a misconfigured hook, all live video is denied.
2. The MediaMTX Control API now requires service credentials.
3. Query-string media credentials are refused.
4. Existing WebRTC/RTSP viewers are disconnected within about one revocation interval after logout, deactivation, permission loss, expiry or session close.
5. Production refuses to start without valid media trust configuration.
6. X-Forwarded-For is trusted only from configured proxies.

---

## 5. Compatibility and migration

- **Migration `0002_media_connections`:** additive only. Downgrade drops the table, which only loses the bindings for viewers already connected. `REQUIRED_SCHEMA_REVISION` is now `0002_media_connections`. Back up first, as for any upgrade.
- **New required secrets in `.env`:** `VMS_MEDIA_GATEWAY_PASSWORD` and `VMS_MEDIA_AUTH_HOOK_SECRET`. The hook secret must be URL-safe, because it is embedded in the hook URL. Compose fails fast if either is missing.
- **Windows client (not seen):** must send `session_id:play_token` as HTTP Basic, for example LibVLC `:http-user` / `:http-password`. Query tokens will be refused.
- **Web client:** compatible as-is. The HLS fallback (Batch 3) must use header credentials.
- **Risks I cannot check from the subset:** unseen tests that assert the schema revision string, the set of supervised task names (I added `media-revocation`), or the `ensure_path` keyword usage.
- After applying: `chmod +x intelligent-vms-v2/tools/resolve_mediamtx_digest.sh`. Unified diffs do not carry file modes.

---

## 6. Next actions — ChatGPT (repository owner)

1. `git apply batch1-r1-r10.patch` on `83e965d`, then `chmod +x` the tool. Push to `feature/intelligent-vms-v2` and report the actual CI result: lint, full suite, OpenAPI (expected unchanged), PostgreSQL migration, web, Windows, SBOM.
2. Resolve and pin the digest: `tools/resolve_mediamtx_digest.sh 1.21.2`.
3. Run the real-gateway harness. Get the official `mediamtx_v1.21.2_linux_amd64.tar.gz`, check it against the release checksums, then:
   `VMS_IT_MEDIAMTX_BIN=/path/mediamtx python -m pytest -m gateway tests/gateway -v -rA`.
   Return the raw output. Optionally add this as a CI job.
4. `mediamtx --validate-conf=deploy/docker/mediamtx.yml` on the pinned binary.
5. Compose smoke test, from a second host:
   - unauthenticated `POST /<path>/whep` → 401;
   - `curl http://<host>:9997` → refused;
   - valid browser session plays.
6. Confirm whether HLS re-authenticates per request (MediaMTX v1.21.2 source: HLS server auth path). The answer decides the HLS revocation status.
7. Update `requirements.yaml` and the feature matrix. I did not have them, so I did not change them.

## 7. Next actions — Sunny (hardware / Windows)

1. Browser WHEP check: two operators on one camera, revoke one, confirm the other keeps playing.
2. One real ONVIF camera through the gateway, with and without a valid session.
3. Windows client against the new credential contract (Basic header). This depends on Batch 6 source.

## 8. Claims not made

Not production-ready. Not verified secure. Finding A is not demonstrated on a real
gateway. HLS revocation is not provided. E.4 is not complete. The existing 161-test
suite has not been re-run here.
