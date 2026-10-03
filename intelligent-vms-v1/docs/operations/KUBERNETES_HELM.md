# Intelligent VMS Kubernetes / Helm Operations

## Scope

The Helm chart under `deploy/helm/intelligent-vms` deploys the VMS application plane. It does not silently create a single-node PostgreSQL/Kafka/ClickHouse/media stack and does not make any 100K capacity claim.

Production stateful dependencies must be supplied explicitly:
- PostgreSQL;
- Kafka/Redpanda;
- ClickHouse;
- regional MediaMTX/recording storage.

## Secrets

Create the central secret before installation. At minimum it normally contains:
- `DATABASE_URL`;
- `VMS_SECRET_KEY`;
- `LIVE_VIEW_TOKEN_PRIVATE_KEY_B64` (base64-encoded PEM RSA private key, 2048+ bits; signs short-lived path-scoped live-media grants);
- identity-provider secrets when applicable;
- `RECORDING_HOOK_TOKEN`;
- `REGIONAL_SPOOL_TOKEN` where used.

The chart references `existingSecret`; it does not commit secret values. `VMS_SECRET_KEY` has no usable application default: credential encryption/decryption fails closed when it is missing. Keep the key stable for data encrypted with it and manage rotation as an explicit migration rather than silently replacing the value.

For a regional release, create `regional.existingSecret` with the node-scoped service token and regional hook/spool tokens.

## Browser CORS

The production chart allows no browser origin by default. Set
`CORS_ALLOWED_ORIGINS` in production values to the exact HTTPS origin or
comma-separated origins that host the trusted VMS web client. Methods and
request headers are also explicit through `CORS_ALLOWED_METHODS` and
`CORS_ALLOWED_HEADERS`; do not replace them with `*` merely to bypass a
browser error. Bearer-token authentication does not require credentialed CORS,
so `allow_credentials` remains disabled.

## Images

Replace every `REPLACE_WITH_IMMUTABLE_TAG` value with an immutable release tag or digest. Do not use an untracked mutable production tag.

## Central release

Example:

    helm upgrade --install vms ./deploy/helm/intelligent-vms \
      --namespace vms --create-namespace \
      -f values-production.yaml

Keep `AUTO_CREATE_SCHEMA=false` in production. Run Alembic as an explicit release/upgrade step before the new application revision becomes ready.

## Regional node release

Use one regional Helm release per logical media/recording node identity. Set:
- `regional.enabled=true`;
- a unique `regional.nodeId`;
- `regional.regionId`;
- `regional.nodeRoles`;
- the node-local/regional MediaMTX API URL;
- the central control API URL;
- a node-scoped service token in the regional secret.

The regional deployment uses Recreate and replica count 1 intentionally so one node identity cannot have two simultaneous node-agent writers.

## Probes

Control API:
- liveness: `/api/v1/system/healthz/live`;
- readiness: `/api/v1/system/healthz/ready`;
- metrics: `/metrics`.

Readiness requires PostgreSQL because PostgreSQL holds durable control truth. Kafka and MediaMTX outages do not make the control API unready; Phase 7 outbox/regional behavior is designed to survive those failures.

## Resources

Resource requests/limits are empty by default. Populate them only from qualified Phase 8 measurements. Do not copy laptop/CI resource numbers into production values.

## Rollout rules

- control-api, placement-controller, event-writer and alarm-worker may use rolling replacement;
- ONVIF event worker defaults to Recreate because multiple identical ownership/shard instances can duplicate device subscription work;
- regional node-agent/spool uses Recreate for node-identity safety;
- enable the control-api PodDisruptionBudget only when running more than one replica.

## External media plane

This central chart intentionally does not route all media through the control API. MediaMTX instances and recording storage remain regional. Register their trusted endpoints and measured capacity in the VMS infrastructure-node registry before enabling distributed placement execution.

## Validation

CI must run:
- `helm lint deploy/helm/intelligent-vms`;
- `helm template` with default central values;
- `helm template` with `regional.enabled=true`.

A rendered manifest is not a production qualification. Phase 8 measured hardware evidence, Phase 9 security/DR gates, and external camera/storage/network qualification are still required.

## Phase 9.2 observability

The control API exposes bounded Prometheus metrics through `/metrics`. The chart
adds a Grafana dashboard ConfigMap by default:

```yaml
observability:
  grafana:
    enabled: true
    datasourceUid: prometheus
```

If the cluster has the Prometheus Operator `PrometheusRule` CRD, enable the
VMS alert bundle explicitly:

```yaml
observability:
  prometheusRule:
    enabled: true
```

Keep hardware-dependent alert thresholds configurable. The chart does not infer
Kafka/PostgreSQL/ClickHouse/MediaMTX/AI/disk exporter metric names; configure
`observability.prometheusRule.externalSignals.*.expr` only after the selected
exporter or managed service metrics are verified.

Recording-gap detection uses durable per-camera segment-completion state. Tune
`OBSERVABILITY_RECORDING_GAP_MIN_SECONDS` and
`OBSERVABILITY_RECORDING_GAP_SEGMENT_MULTIPLIER` to the recording segment
policy rather than setting an arbitrary hardware-derived value.

See `docs/operations/PHASE9_OBSERVABILITY_SLO.md` for the metric catalog,
default alert policy, external-exporter contract, SLO objectives and triage.



## Phase 9.3 backup, DR and rollback

Executable operator scripts live under `deploy/operations` for PostgreSQL,
ClickHouse, regional state and Helm release configuration backup/restore paths.

Before any production upgrade, run the backup set, verify it with
`tools/phase9_dr_evidence.py`, and run `deploy/operations/upgrade-preflight.sh`.
Application rollback is allowed only while the database schema remains compatible
with the previous application revision; otherwise use the tested restore path rather
than assuming an Alembic downgrade is safe.

See `docs/operations/PHASE9_BACKUP_RESTORE_DR.md` for the complete restore order,
region-loss exercise, object-storage boundaries and measured RPO/RTO evidence format.


## Phase 9 production security

The chart's secure defaults now include:

- `AUTH_REQUIRE_OIDC=true`;
- `security.requireIngressTls=true`;
- `networkPolicy.enabled=true`;
- non-root pod/container security contexts;
- optional provider-neutral External Secrets Operator integration.

The default NetworkPolicy allows same-release traffic and cluster DNS only. Production
values must explicitly add ingress-controller/Prometheus ingress and the exact
PostgreSQL, Kafka, ClickHouse, OIDC, MediaMTX/camera and secret-manager egress required
by the deployment.

An enabled Ingress must include TLS unless `security.requireIngressTls` is
intentionally overridden through a reviewed non-production profile.

See `docs/security/PHASE9_PRODUCTION_SECURITY.md` for mTLS/certificate rotation,
secret-manager/KMS, centralized security audit retention, rate limiting/abuse controls
and the remaining deployment evidence required for release.


Category-2 live media authorization also requires every browser-facing regional MediaMTX to use the external-auth callback documented in `docs/security/LIVE_MONITORING_MEDIA_AUTH.md`. Do not expose anonymous WebRTC/HLS reads in production.
