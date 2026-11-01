# Security Model

## Threat model

Treated as **untrusted input**: camera responses, ONVIF `XAddrs`,
`GetStreamUri` results, operator-supplied hostnames and URLs, webhook targets,
uploaded licence files, every API request body.

Treated as **trusted**: the configured media gateway endpoint on loopback, the
database connection, the local filesystem.

Primary adversaries considered:
1. An attacker who can add or modify a camera entry (insider or compromised
   operator account) and wants the server to connect somewhere it should not.
2. A malicious or compromised camera on the surveillance VLAN.
3. A network attacker attempting credential theft or replay.
4. An operator exceeding their authority.

## Controls

### Authentication
- **Argon2id** password hashing; cost parameters configurable with an enforced
  OWASP floor in production (`memory ≥ 19456 KiB`, `time_cost ≥ 2`).
- **Access tokens**: short-lived JWTs carrying a `jti`. Revocation is real —
  the `jti` is checked against a denylist on every request, so a deactivated
  user loses access immediately, not at token expiry.
- **Refresh tokens**: opaque random strings; only a keyed hash is stored.
  Rotation is enforced and **reuse of a rotated token revokes the entire token
  family** (stolen-token detection).
- **MFA**: RFC 6238 TOTP. The accepted counter is persisted, so a code cannot
  be replayed — including the code used to complete enrolment.
- **Lockout**: windowed, time-bounded, audited.
- Unknown usernames perform an equivalent Argon2 verification against a fixed
  decoy hash so response timing does not reveal which accounts exist.

### Authorization
- 26 discrete permissions, evaluated **server-side on every protected route**.
- Scope resolution for a camera: explicit deny wins → explicit grant allows →
  otherwise the camera's site (or an ancestor) must be in the user's scope.
- A non-admin with no site scope sees **nothing**. Failing closed is tested.
- API keys can never exceed their owner's permissions; attempting to create
  one is rejected.

### Camera credentials
- AES-256-GCM, with the camera id as **additional authenticated data**, so a
  ciphertext copied to another camera row fails to decrypt instead of silently
  succeeding.
- The master key lives at `<data_root>/keys/vms-master.key` (0600), never in
  the database, never in a backup unless `--include-keys` is passed.
- Key rotation is supported: ciphertext carries a key id, so old secrets stay
  readable while new writes use the current key.
- Camera passwords and RTSP source URLs are **absent from every DTO**. A test
  asserts no password and no `rtsp://` appears in camera API responses.

### SSRF and discovery abuse
`app/core/netguard.py` applies to every camera-supplied address:
- Scheme and port allow-lists.
- Deny-list covering loopback, link-local (`169.254.0.0/16` — cloud metadata),
  and unspecified ranges.
- Optional strict CIDR allow-list for the camera VLAN (recommended in
  production).
- **DNS rebinding mitigation**: `resolve_pinned()` resolves once, validates the
  result, and returns the IP the caller must connect to.
- Discovery ranges are capped (`discovery_max_hosts`, default 1024). A request
  to scan a `/8` is refused. **This product will not become an internal port
  scanner.**
- Device-advertised `XAddrs` are rewritten to the already-validated address,
  so a malicious camera cannot redirect the server elsewhere.

### Filesystem
Every path derived from input goes through `resolve_within`, which resolves
symlinks and verifies containment. Export, snapshot, retention and diagnostic
paths are all confined.

### Transport
TLS is terminated at a reverse proxy (nginx/IIS); configuration is given in
`docs/deployment/`. Security headers (`X-Content-Type-Options`,
`X-Frame-Options`, `Referrer-Policy`, `Cache-Control: no-store`) are set by
middleware on every response.

### Audit
Append-only. Every security-relevant action records actor, object, outcome,
client IP and the correlation id that ties it to request logs. Privacy-sensitive
searches (facial recognition, ANPR) go to a **separate** log that the ordinary
retention job does not purge.

### Logging
A redaction processor strips credentials from URLs and masks any field named
like a secret, so camera passwords cannot leak into logs or support bundles.

## Residual risks

| Risk | Status |
| --- | --- |
| Rate limiting is per-process | With N workers the effective limit is N x configured. Login lockout is global; request limits are not. |
| No penetration test | Not performed. A release gate. |
| Recorder split-brain | A paused worker resuming after its staleness window could briefly double-write. Mitigated by heartbeat checks, not eliminated. |
| Master key loss | Unrecoverable camera credentials by design. Operational control only — back it up separately. |
| OIDC untested | Implemented against the spec, never run against a real IdP. |
