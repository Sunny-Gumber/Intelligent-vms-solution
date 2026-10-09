# Intelligent VMS

Distributed, multi-tenant enterprise VMS designed to scale by separating control, media, recording, event, AI and storage responsibilities instead of routing all video through one central server.

> New chat / new engineer: start at repository-root `INTELLIGENT_VMS_START_HERE.md`, then read `docs/program/PROJECT_HANDOFF.md`.

## Current status

**Release Candidate / External Qualification Pending**

Release Candidate merge:

`c75828efc835f05dabf2d2b7ffdd73e37c0258d6`

This is a software/SRE/security Release Candidate. It is not yet Production Qualified and does not certify 100K media throughput, hardware sizing, camera interoperability or all market-feature rows.

## Completed software milestones

### Phases 1–6
- PostgreSQL control plane;
- MediaMTX RTSP/WebRTC/HLS media layer;
- Kafka-compatible event backbone;
- ClickHouse event store;
- ONVIF discovery/probe/capability onboarding;
- JWT/RBAC tenant/site authorization;
- Alembic migrations and durable reconciliation;
- source-copy continuous recording;
- retention/timeline/playback;
- event search and camera health;
- ONVIF events, alarms and diagnostics;
- AI orchestration/provider contract.

### Phase 7 — distributed regional VMS
- regional node registry and placement;
- assignment-driven media/recording execution;
- trustworthy node telemetry;
- generation/lease fencing;
- stale-owner rejection;
- bounded regional autonomy during WAN/control loss;
- durable regional spool/backfill;
- transactional outbox and replay-tolerant Kafka delivery;
- deterministic 14-scenario software chaos qualification.

### Phase 8 — measured-evidence framework
- benchmark result schema/workload drivers;
- environment/resource capture;
- measured-only hardware matrix;
- reproducibility/thermal QA.

Real target-hardware/environment evidence remains pending under #185.

### Phase 9 — production/SRE/security
- Kubernetes/Helm deployment foundation;
- readiness/liveness/metrics;
- Prometheus/Grafana/alerts/SLO contracts;
- recording-gap monitoring;
- backup/restore/DR tooling;
- upgrade/rollback preflight;
- OIDC production trust requirements;
- audit/network/security hardening;
- pip-audit + Trivy image scanning;
- executable Release Candidate gate.

## Market feature program

The product target includes the complete vendor-neutral market checklist:

- `docs/product/VMS_COMPLETE_FEATURE_MASTER_CHECKLIST.md`
- `docs/product/FEATURE_CATALOG.csv`
- `docs/product/FEATURE_CATALOG_SUMMARY.md`
- `docs/program/MARKET_FEATURE_MASTER_PROGRAM.md`

Current catalog:
- **48 categories**
- **623 deterministic feature rows**

These are product targets. A row must be audited/implemented/verified before it can be claimed supported.

## Architectural rule

Video stays distributed. Central services manage identity, configuration, placement, metadata, search and policy; regional/edge media and recording nodes handle continuous video.

Recording remains independent from AI and central WAN availability within the bounded regional-authority model.

## Local development

Requirements: Docker + Docker Compose.

```bash
cp .env.example .env
docker compose up --build
```

Development defaults may use `AUTH_DISABLED=true`. Never expose that mode to an untrusted network.

From this directory, the Python test gate is:

```bash
pip install -r tests/requirements.txt
VMS_SECRET_KEY=ci-test-key python3 -m pytest -q tests
```

`tests/requirements.txt` includes `services/control-api/requirements.txt`.

## Production evidence discipline

Do not infer camera/server/GPU/storage capacity from formulas or CI smoke tests.

Use the Phase 8 benchmark/evidence tools and attach real measured target-environment results. Missing evidence must stay `UNQUALIFIED`.

## Current external blockers

- #185 — real target hardware/environment benchmark evidence;
- #261 — inherited unfixed HIGH CVEs in current Python base images;
- real camera/vendor/firmware interoperability;
- target storage/network/WAN/HA drills;
- measured RPO/RTO;
- deployed OIDC/PKI/mTLS/KMS/gateway/SIEM controls;
- pilot/operator soak.

## Read next

- `docs/program/PROJECT_HANDOFF.md`
- `docs/program/IMPLEMENTATION_HISTORY.md`
- `docs/program/PRODUCTION_EXECUTION_PLAN.md`
- `docs/program/MARKET_FEATURE_MASTER_PROGRAM.md`
- `docs/program/AI_CODING_RULES.md`
- `docs/release/PHASE9_RELEASE_CANDIDATE.md`

## Next work

When external hardware/site evidence is available, execute Phase 8 real measurements and Phase 10 external qualification.

When external evidence is not available, continue the Market Feature Master Program in dependency order, auditing existing functionality before implementing missing feature rows.
