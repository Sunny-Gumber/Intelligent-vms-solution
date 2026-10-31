# Security Policy

## Reporting a vulnerability

Do **not** open a public issue. Email the repository owner with:

- A description and the affected component
- Reproduction steps
- The impact you believe it has
- Your assessment of severity

Expect acknowledgement within 3 business days and an initial assessment
within 10.

## Scope

**In scope:** authentication and session handling, authorization and scope
enforcement, the credential vault, SSRF and network guarding, path
confinement, injection, the export/evidence chain, privacy controls for
facial recognition and ANPR, and the audit trail.

**Out of scope:** vulnerabilities in third-party cameras (report to the
manufacturer), denial of service by an authenticated administrator,
issues requiring physical access to the server, and anything in the
documented limitations list — those are known and published, not
vulnerabilities.

## Known security posture

`docs/security/security-model.md` states the threat model and the residual
risks honestly. Notably:

- **No penetration test has been performed.** This is a release gate.
- Rate limiting is per-process; with N workers the effective limit is N x the
  configured rate. Login lockout is database-backed and therefore global.
- OIDC is implemented but has never been run against a real identity
  provider.

## Deployment hardening

The product refuses to start in production mode with a default secret key,
SQLite, a wildcard trusted-host list, Argon2 parameters below the OWASP
floor, or `ldap_enabled = true`. These are deliberate and should not be
worked around.

See `docs/deployment/firewall.md`. The single most effective control in a
CCTV deployment is isolating the camera VLAN from the client network, with
the VMS as the only bridge.

## Supported versions

| Version | Security fixes |
| --- | --- |
| 0.1.x-alpha | Best effort. **Not supported for production use.** |
