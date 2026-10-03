# Phase 9 Production Security Hardening

## Scope

This document records the Phase 9 software security baseline for the Intelligent VMS
and the deployment controls that must be supplied by the target platform.

The software baseline can be qualified in CI. Provider-specific identity, PKI, network,
secret-manager, gateway and log-retention controls still require deployment evidence.
A software security PASS is not a Production Qualified claim.

## Production trust mode

The Helm production profile sets:

```yaml
commonConfig:
  AUTH_DISABLED: "false"
  AUTH_REQUIRE_OIDC: "true"
```

When `AUTH_REQUIRE_OIDC=true`, control-api startup fails closed unless all of the
following are true:

- authentication bypass is disabled;
- `AUTH_HS256_SECRET` is empty;
- `AUTH_JWKS_URL` is configured and uses HTTPS;
- `AUTH_ISSUER` is configured and uses HTTPS;
- `AUTH_AUDIENCE` is non-empty.

This keeps local/integration HS256 support available only when the production OIDC
requirement is explicitly disabled.

Production tokens remain subject to the existing JWT expiry, issuer, audience, role,
tenant, site and optional node-scope checks. Out-of-scope resource access continues to
return not-found semantics to avoid cross-tenant enumeration.

## Identity-provider rotation

OIDC/JWKS certificate and signing-key rotation is owned by the selected identity
provider.

Operational requirements:

- rotate signing keys with an overlap period long enough for JWKS cache refresh;
- keep old keys published until tokens signed by them can no longer be valid;
- rotate issuer TLS certificates before expiry;
- monitor JWKS fetch and authentication-failure rates;
- test issuer/audience/key rotation in a pre-production environment;
- do not switch production trust to HS256 as an emergency shortcut.

## Secrets and KMS

The Helm chart never embeds production secret values.

The normal chart consumes an existing Kubernetes Secret through `existingSecret` and,
for regional nodes, `regional.existingSecret`.

The chart can optionally create those Kubernetes Secrets through External Secrets
Operator:

```yaml
externalSecrets:
  central:
    enabled: true
    secretStoreRef:
      name: production-secret-store
      kind: ClusterSecretStore
    dataFrom:
      - extract:
          key: intelligent-vms/production
```

The external provider may be AWS Secrets Manager/KMS, Azure Key Vault, Google Secret
Manager/KMS, Vault or another supported provider. Provider credentials, KMS keys and
secret values remain outside Git and ordinary Helm values.

Required production practice:

- use workload identity or another short-lived provider identity where possible;
- separate central and regional secret scopes;
- restrict secret-store permissions to the exact paths required;
- rotate database, Kafka, callback and service credentials through the provider;
- monitor ExternalSecret reconciliation failures;
- back up/restore provider configuration through the provider's protected process.

`VMS_SECRET_KEY` protects encrypted camera credential fields. Do not silently replace
it. Rotation requires an explicit credential re-encryption migration with recovery
evidence.

## TLS and mTLS boundaries

External browser/API ingress must use TLS. With the default security posture,
`security.requireIngressTls=true`; Helm refuses to render an enabled Ingress that has
no TLS configuration.

Service-to-service mTLS is a platform integration rather than application-issued
certificates. For sensitive internal boundaries, production should use the selected
service mesh, ingress/gateway PKI, managed database TLS or equivalent platform
mechanism for:

- control-api to PostgreSQL;
- event producers/consumers to Kafka/Redpanda;
- event writer/search to ClickHouse;
- control/regional service traffic over routed networks;
- control-api to the identity-provider JWKS endpoint;
- administrative/operator ingress.

mTLS does not replace application service authorization. Node-scoped bearer/service
tokens, recording-hook authentication and regional-spool authentication remain
required.

Certificate rotation requirements:

- automate certificate renewal through the selected PKI/mesh;
- alert before certificate expiry;
- test overlapping certificate rotation without stopping recording ownership;
- retain rollback instructions for CA/trust-bundle changes;
- do not commit private keys to the repository or Helm values.

## Kubernetes NetworkPolicy

`networkPolicy.enabled=true` is the secure chart default.

The base policy selects VMS pods and permits only:

- traffic between pods in the same Helm release;
- DNS to the configured cluster DNS namespace/pod selector.

All other ingress and egress is denied unless explicitly added through
`networkPolicy.extraIngress` or `networkPolicy.extraEgress`.

Production values must explicitly allow only the dependencies that exist in the target
environment, such as:

- trusted ingress-controller/gateway traffic to web/control-api;
- Prometheus scraping to control-api;
- PostgreSQL;
- Kafka/Redpanda;
- ClickHouse;
- OIDC/JWKS;
- required regional MediaMTX/control-plane endpoints;
- explicitly approved camera/ONVIF network ranges for workloads that need them;
- external secret-manager/telemetry endpoints when required.

Avoid broad `0.0.0.0/0` or `::/0` egress rules. If a managed service requires a
broad network destination, record the exception and compensate with TLS/service
identity and provider firewall controls.

## Runtime container baseline

Application Helm pods use:

- `runAsNonRoot: true`;
- runtime-default seccomp;
- `allowPrivilegeEscalation: false`;
- Linux capability drop `ALL`;
- service-account token automount disabled.

Python application images use dedicated UID/GID `10001`. The web image uses numeric
non-root UID `101` and listens on unprivileged port `8080`.

`readOnlyRootFilesystem` remains disabled because some current runtime/base-image
paths still need controlled writable locations. This is a documented residual
hardening item; do not claim read-only-rootfs enforcement until every image has been
qualified with explicit writable tmp/state mounts.

## Security audit trail

The control API emits a bounded structured audit record for:

- every POST, PUT, PATCH and DELETE request;
- every HTTP 401 or 403 response, including read requests.

Audit fields:

- request ID;
- HTTP method;
- matched route template rather than raw resource path;
- response status;
- SHA-256-derived subject identifier;
- tenant ID;
- normalized roles;
- node ID where applicable.

The middleware does not log:

- bearer tokens;
- request bodies;
- query strings;
- raw URL paths containing identifiers;
- plaintext subject identity;
- exception messages.

Production must forward `vms.security.audit` logs to a centralized append-only or
immutability-controlled security log sink. Define retention, access, export and incident
review policy in the selected SIEM/log platform. Local container logs alone are not a
production audit-retention solution.

## Rate limiting and abuse controls

The application already bounds high-risk operations through query windows, response
limits, worker concurrency, payload limits and timeouts. A horizontally scaled VMS
must not pretend that per-process in-memory counters are a global rate limiter.

Production ingress/API gateway controls must enforce and test:

- request-rate limits per source/principal/tenant as appropriate;
- concurrent-request limits;
- request body limits;
- connection/header timeouts;
- burst policy for onboarding/discovery and other expensive operations;
- HTTP 429 behavior and security monitoring.

The concrete syntax is ingress/gateway specific. Configure these controls in the chosen
platform and retain the rendered configuration plus load/abuse-test evidence for the
release gate.

## Evidence and export permissions

The current VMS does not expose a general evidence-package/export API. Recording
playback remains subject to authenticated tenant/site scope.

A future evidence/export feature must not inherit permission merely because playback is
allowed. It must add:

- a dedicated role/scope or explicit policy decision;
- tenant/site/camera authorization on every export source;
- an immutable audit record for request, completion and download;
- short-lived signed download access;
- no camera credentials or internal service tokens in export metadata/URLs;
- content hash/signature evidence where required;
- retention/legal-hold policy;
- bounded job size/rate/concurrency.

Until that contract exists, evidence export remains unimplemented rather than
implicitly permitted.

## Dependency and container scanning

`.github/workflows/intelligent-vms-security.yml` is a blocking security workflow.

It pins:

- `pip-audit==2.10.1` for all production Python requirement files;
- `aquasec/trivy:0.74.0` for all built VMS images.

The dependency audit fails on known Python vulnerabilities reported by pip-audit.
Trivy fails on fixable HIGH/CRITICAL image findings; unfixed findings remain visible but
do not create an impossible gate solely because no vendor fix exists.

Do not suppress a finding merely to make CI green. Upgrade/replace the affected
dependency/base image, or record an explicit release security decision if remediation
is impossible.

## Internal callback and service authentication

Existing controls remain mandatory:

- node operations require matching node-scoped or approved global service identity;
- recording completion hooks require the configured recording-hook token;
- regional spool endpoints require their dedicated token or node bearer identity;
- callback/service tokens are not accepted in query strings;
- SSRF/network-policy validation remains applied to ONVIF/RTSP and infrastructure
  endpoints.

Transport security is additive to, not a replacement for, these application controls.

## Release security evidence

A Phase 9 software security PASS requires:

- production OIDC posture tests passing;
- tenant/site/node authorization regression tests passing;
- security audit privacy tests passing;
- Helm TLS fail-closed test passing;
- NetworkPolicy and ExternalSecret render tests passing;
- non-root image builds passing;
- dependency audit passing;
- container vulnerability gate passing;
- normal full CI/chaos/migration/Helm/image gates passing;
- Security Agent and independent Reviewer sign-off.

The following remain deployment/external evidence, not CI-complete product claims:

- actual identity-provider configuration and signing-key rotation;
- production CA/mTLS configuration and certificate rotation;
- external secret-manager/KMS access policy;
- ingress/API-gateway rate limiting;
- centralized immutable audit-log retention;
- provider firewall/security-group policy;
- real penetration/abuse testing;
- external certification where required.
