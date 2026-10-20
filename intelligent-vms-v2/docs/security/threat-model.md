# Threat model

## Assets, ranked

1. **Live and recorded video** — the reason the system exists.
2. **Camera credentials** — grant direct access to the customer's devices,
   often reused across a site.
3. **The audit and privacy logs** — what makes an investigation defensible.
4. **The master encryption key** — unlocks every stored camera credential.
5. **Operator accounts** — especially administrators.

## Adversaries

| Adversary | Capability | Primary mitigation |
| --- | --- | --- |
| External attacker on the network | Can reach the API; cannot authenticate | Argon2id, lockout, rate limiting, TLS at the edge |
| **A compromised camera** | Answers ONVIF/RTSP; controls every response | **Treated as untrusted input throughout** |
| Malicious insider (operator) | Valid low-privilege credentials | Fail-closed scope, per-camera permissions, audit trail |
| Stolen database backup | Full read of the database | Credentials encrypted with a key held outside it |
| Curious operator | Legitimate access, no legitimate purpose | Privacy access log with a mandatory justification |

## The compromised-camera path, in detail

This is the attack most VMS products handle badly, so it gets explicit
treatment.

A camera supplies data the server acts on: ONVIF `XAddrs`, `GetStreamUri`
responses, device info strings, event payloads. A compromised camera can return
anything.

| Attack | Control | Where |
| --- | --- | --- |
| XAddr points at `169.254.169.254` (cloud metadata) | Deny-CIDR list rejects it | `core/netguard.py` |
| XAddr points at `127.0.0.1:9997` (gateway control API) | Loopback denied | `core/netguard.py` |
| Hostname resolves safe on check, loopback on connect (DNS rebinding) | `resolve_pinned()` resolves ONCE; callers connect to the validated IP | `core/netguard.py` |
| XAddr redirects to a different internal host | Advertised addresses are rewritten to the already-validated IP | `devices/onvif_adapter._rewrite_host` |
| Camera supplies a URL on an administrative port | Port allow-list | `core/netguard.py` |
| Oversized or hostile XML | Size-bounded reads; no DTD processing; defensive parsing | `devices/onvif_soap.py` |
| Device info injected into logs/filenames | Paths built from IDs, not names; path confinement | `core/paths.py` |

Discovery gets the same treatment: `expand_discovery_range()` refuses any
range larger than the configured cap, so camera discovery cannot be turned into
a general network scanner. Every discovery run is audited.

## Credential protection

Camera passwords must be replayed to the device, so they are encrypted rather
than hashed (ADR-0008):

- AES-256-GCM; the 32-byte master key lives outside the database at mode 0600.
- Ciphertext is bound to its owner with AAD (`camera:<id>`), so a blob copied
  between rows fails authentication instead of silently decrypting.
- The key id is embedded, so rotation does not require a flag day.
- Logging has a redaction processor; a test asserts that neither the password
  nor an `rtsp://` URL appears in an API response.

**Accepted risk, stated plainly:** anyone with root on the server and access to
the key file can decrypt stored credentials. Sourcing the key from an HSM or
KMS is the hardening step not yet taken.

## Authorization

Two independent checks on every protected route: **capability** (does the
principal hold the permission) and **scope** (is this object inside their
site/camera scope). A non-administrator with no scope sees nothing — the
default is deny, verified by `test_viewer_without_site_scope_sees_no_cameras`.

An API key can never exceed its owner's permissions; the creation endpoint
rejects the attempt, and the runtime intersects the two sets regardless.

## Privacy-regulated capabilities

Facial recognition and ANPR are disabled by default and gated on five
independent controls: configuration flag, licence entitlement, a dedicated
permission, a mandatory written justification, and an entry in a separate
privacy access log that ordinary retention never purges. Biometric and plate
attributes have their own shorter retention, applied independently of video.

## Residual risks

- TLS termination is a reverse-proxy responsibility; this product does not
  manage certificates.
- Rate limiting is per-process (see `docs/LIMITATIONS.md`).
- No independent penetration test has been performed — Gate 5 of the
  qualification plan.
- Supply chain: dependencies are pinned by lockfile and inventoried in the
  SBOM, but no reproducible-build attestation is produced.
