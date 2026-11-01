# ADR-0010 — Ed25519-signed offline licences that never block operation

**Status:** Accepted

## Context

The requirements state that licensing "must not prevent authorized offline
installations from operating". Surveillance systems run air-gapped, and a
licence check that stops recording is a safety problem, not just an annoyance.

## Decision

- A licence is a JSON document signed with **Ed25519**. The vendor holds the
  private key; the product ships only the public key.
- Validation is **pure local cryptography**: no activation server, no
  phone-home, no periodic online check. There is no network call anywhere in
  the validation path.
- An absent, invalid or expired licence degrades to a **working limited mode**
  (8 cameras, core features). It never stops recording, playback or export of
  existing cameras.
- Enforcement points are few and explicit: camera count on onboarding,
  federation registration, server-side analytics, facial recognition, ANPR,
  OIDC.

## Consequences

**Good:** air-gapped sites work. A licence problem cannot destroy evidence or
blind an operating site.

**Costs:** an offline licence can be copied to multiple servers. Hardware
binding is supported in the format but not enforced, because binding to
hardware IDs causes support pain when a customer legitimately replaces a server.
This is a deliberate commercial trade-off: the licence is a contract artefact,
not a DRM mechanism.
