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

A **global administrator** is an identity whose roles include `admin` and whose `tenant_id` is `"*"`. A tenant-scoped administrator has role `admin` and a specific `tenant_id`. Those are not the same authority.

Only a global administrator may mutate global control-plane state:

- infrastructure nodes: register, configure, and drain via `PUT /api/v1/infrastructure/nodes/{node_id}` (`state` `draining` is the drain operation; this tree has no node-delete route)
- node heartbeat and revocation acknowledgement when the caller is an administrator (`POST /api/v1/infrastructure/nodes/{node_id}/heartbeat`, `POST /api/v1/infrastructure/nodes/{node_id}/fences/revocations/{revocation_id}/ack`). A `service` token may still act only on the node named by its `node_id` claim. A tenant-scoped administrator may not.
- placement runs that assign and fail over cameras (`POST /api/v1/infrastructure/placement/run`)
- transactional outbox requeue (`POST /api/v1/system/outbox/{message_id}/requeue`)

A tenant-scoped administrator receives HTTP 403 on those routes, and the handler does not change state. That administrator keeps tenant-scoped powers, such as cameras and other resources in its own tenant. A service token is not a global administrator.

## Authorization policy

- viewer: read cameras/health/capabilities
- operator: viewer rights + camera onboarding/delete/refresh/discovery
- admin with a specific `tenant_id`: operator rights plus tenant-scoped administration inside that tenant
- global admin (`admin` and `tenant_id` `"*"`): tenant-admin rights plus the global mutations listed above
- service: machine event ingestion and, when `node_id` is set, that node's heartbeat and revocation acknowledgement; not camera administration and not node registration by default

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

## Native Windows desktop OIDC metadata

Native desktop organization sign-in is enabled only when production OIDC trust is configured and:
- `AUTH_DESKTOP_OIDC_ENABLED=true`
- `AUTH_OIDC_CLIENT_ID=<public client id>`
- `AUTH_OIDC_SCOPES="openid ..."`

The public client has no client secret. `GET /api/v1/auth/capabilities` returns only safe public authentication metadata. The desktop uses the configured HTTPS issuer/authority for system-browser Authorization Code + PKCE, then presents the resulting access token to the existing VMS JWT validation and tenant/site/RBAC boundary.
