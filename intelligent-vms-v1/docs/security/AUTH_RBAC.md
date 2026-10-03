# Authentication and RBAC

## Production posture

Authentication is enabled by default. Local development must explicitly set `AUTH_DISABLED=true`.

Production should use an OIDC/OAuth2 identity provider and configure:
- `AUTH_REQUIRE_OIDC=true`
- `AUTH_JWKS_URL`
- `AUTH_ISSUER`
- `AUTH_AUDIENCE`

When `AUTH_REQUIRE_OIDC=true`, startup fails closed if authentication is bypassed,
HS256 is configured, JWKS/issuer are missing, or the trust URLs do not use HTTPS.

`AUTH_HS256_SECRET` exists for controlled local/integration testing only and is
forbidden by the production OIDC posture.

## Token claims

Required:
- `sub`
- `exp`
- audience matching `AUTH_AUDIENCE`
- `tenant_id`
- role/roles

Supported roles:
- admin
- operator
- viewer
- service

Site scope:
```json
{
  "tenant_id": "tenant-01",
  "site_ids": ["site-a", "site-b"],
  "roles": ["operator"]
}
```

`site_ids:["*"]` grants all sites inside the token tenant. `tenant_id:"*"` is reserved for explicitly trusted global administration/service control.

## Authorization policy

- viewer: read cameras/health/capabilities
- operator: viewer rights + camera onboarding/delete/refresh/discovery
- admin: operator rights + future tenant/admin operations
- service: machine event ingestion; not camera administration by default

Out-of-scope object access returns 404 to avoid confirming resource existence across tenants.

## JWKS caching

PyJWT's JWK client caches signing keys. Monitor identity-provider/JWKS errors and do not fetch signing metadata manually on every request.

## UI

The minimal static web wall remains development-only. Production UI must obtain a bearer token through an OIDC flow and never embed camera credentials or long-lived service tokens.

## Security rules

- do not log bearer tokens;
- do not put tokens in query strings;
- use TLS;
- use short-lived access tokens;
- use service identity for machine event producers;
- scope every resource query by tenant/site;
- treat `AUTH_DISABLED=true` as a non-production configuration finding.


## Phase 9 production security

See `PHASE9_PRODUCTION_SECURITY.md` for OIDC key/certificate rotation, structured
mutation/auth-failure audit logging, External Secrets/KMS integration, NetworkPolicy,
TLS/mTLS boundaries, abuse controls and the security release gate.
