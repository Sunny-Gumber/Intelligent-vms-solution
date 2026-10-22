# ADR-0011 — Local accounts first; OIDC implemented; LDAP deferred

**Status:** Accepted

## Context

Enterprise customers expect LDAP/AD and SSO. Both are substantial integrations
with real failure modes, and a VMS must never become unreachable because an
identity provider is down.

## Decision

1. **Local accounts are always available** (Argon2id, TOTP MFA, lockout,
   server-side revocation). They cannot be disabled, so a misconfigured IdP can
   never lock an administrator out of the surveillance system.
2. **OIDC is implemented** — authorization code + PKCE, state/nonce, JWKS
   signature validation, issuer/audience/expiry checks.
3. **LDAP is deferred.** `app/identity/ldap.py` defines the interface and every
   method raises `501 not_implemented`. Production startup refuses
   `VMS_LDAP_ENABLED=true`.

## Why deferred rather than half-built

A half-working LDAP integration that silently falls back to local accounts, or
that fails open on a bind error, is worse than none. The interface is fixed so
the implementation can be dropped in with a known shape.

## Consequences

**Costs:** customers requiring AD integration cannot use this release for that.
OIDC is implemented but **has never been tested against a real identity
provider** — Entra ID and Keycloak validation are Gate 4 items. Group-to-role
mapping is designed but not wired.
