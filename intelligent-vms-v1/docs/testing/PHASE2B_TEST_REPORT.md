# Phase 2B QA report

Status: automated gates prepared; CI evidence required before Boss merge.

## Authentication/RBAC

Automated:
- tenant/site principal access
- cross-tenant access hidden as 404
- signed local HS256 integration token validation
- role normalization ignores unknown roles

Manual/integration before production:
- OIDC/JWKS provider rotation
- expired token
- wrong issuer/audience
- missing site claim
- viewer mutation attempt
- service role camera-admin attempt
- bearer token absent from logs

## Database

CI must:
1. create a fresh SQLite DB;
2. run `alembic upgrade head`;
3. run `alembic current`;
4. verify expected tables;
5. remove test DB.

PostgreSQL migration is still required in integration/staging before production.

## Reconciliation

Automated:
- rebuild source prefers substream
- encoded camera credentials reconstructed
- present paths are not re-added
- disabled cameras ignored
- per-run change budget enforced

Integration before production:
- restart MediaMTX and verify paths recover
- run two control-api replicas against PostgreSQL and verify advisory lock
- unavailable MediaMTX leaves DB untouched
- reconnect storm under thousands of cameras remains within configured budget

## Current quality gate

The Phase 2B PR may merge to development after compile/unit/migration CI is green. Production remains contingent on real PostgreSQL + MediaMTX restart tests and OIDC provider integration.
