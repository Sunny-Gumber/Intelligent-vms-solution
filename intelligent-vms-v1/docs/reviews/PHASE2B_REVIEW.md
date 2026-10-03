# Phase 2B reviewer report

Status: PRE-CI REVIEW.

## Security

Positive:
- auth secure-by-default;
- explicit development bypass;
- JWT signature/exp/audience/issuer validation;
- recognized roles only;
- tenant/site scope checks;
- camera/ONVIF mutations protected;
- event producer has service role path;
- cross-scope resources hidden.

Follow-up:
- production UI OIDC flow not yet implemented;
- full audit trail is not yet implemented;
- service-to-service mTLS remains later enterprise hardening.

## Reliability

Positive:
- runtime media paths are no longer assumed durable;
- missing paths are recreated from DB desired state;
- bounded per-run changes reduce reconnect storm risk;
- PostgreSQL advisory transaction lock prevents simultaneous reconcilers;
- unknown paths are not automatically deleted.

Follow-up:
- current query scans first ordered batch; at large regional scale a cursor/partitioned reconciler is required;
- media-node placement still assumes one node per camera record;
- metrics are in-memory/logging, not Prometheus counters yet.

## Database

Positive:
- production startup no longer requires create_all;
- Alembic baseline exists;
- dev bootstrap remains explicit.

Follow-up:
- initial migration must be tested against PostgreSQL in staging;
- future high-volume migrations need online migration strategy.

## Reviewer decision

No new architectural blocker for development merge if CI compile/unit/migration gates pass. Production certification is still deferred until OIDC, PostgreSQL, MediaMTX restart and real-camera integration tests run in an appropriate environment.
