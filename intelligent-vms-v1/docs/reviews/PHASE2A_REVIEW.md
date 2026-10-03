# Phase 2A independent review

Reviewer Agent status: CONDITIONAL PASS for merge to development branch; NOT production certification.

## BLOCKER

### Platform API authentication/RBAC
The current Phase-1/2A control API has no complete production authentication/authorization layer. Camera onboarding endpoints therefore must not be exposed to an untrusted network.

Disposition: broader platform blocker scheduled before production/enterprise release. Dev/lab use only behind trusted network until fixed.

## HIGH

### Media-node reconciliation after restart
MediaMTX runtime paths are configured dynamically. A durable reconciler that re-applies desired state after media-node restart is required.

Disposition: Phase 2B/scale controller work.

### Database schema migrations
Prototype uses SQLAlchemy create_all. Production requires Alembic/controlled schema migration.

Disposition: required before production beta.

## MEDIUM

### Media2-only devices
Phase 2A implements Media v1 probing. Media2-only or nonstandard devices can require fallback/vendor adapters.

Disposition: preserve manual RTSP fallback and add fixtures only when failures are reproduced.

### Discovery topology
Multicast discovery inside bridge containers/routed networks is not guaranteed.

Disposition: architecture correctly plans site-local discovery workers; manual IP probe is authoritative.

## SECURITY POSITIVES

- default camera network allowlist reduces SSRF exposure;
- every advertised HTTP/RTSP endpoint is revalidated;
- redirects disabled;
- safe XML parser;
- bounded response size/timeouts;
- public probe removes internal raw stream URI;
- camera credentials are not returned in response models;
- manual RTSP fallback preserved.

## SCALE POSITIVES

- discovery is not treated as global;
- capability snapshot is control-plane data, not media state;
- no central decode/transcode added;
- media node remains replaceable;
- capacity model separates RAM/CPU/network/storage/AI dimensions.

## Reviewer recommendation

Merge Phase 2A only as a development milestone after automated syntax/unit checks pass. Do not mark production-ready until:
1. API auth/RBAC;
2. migrations;
3. durable media reconciler;
4. Docker integration;
5. at least two real ONVIF camera/vendor tests;
6. negative credential/security tests with log inspection.
