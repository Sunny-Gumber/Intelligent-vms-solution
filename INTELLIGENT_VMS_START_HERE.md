# Intelligent VMS — START HERE

**Purpose:** permanent project handoff for a new ChatGPT/chat/engineer.

If you are continuing the Intelligent VMS project, read this file first. This standalone repository is `Sunny-Gumber/Intelligent-vms-solution`; the product remains under `intelligent-vms-v1/` during migration to minimize architectural risk. The legacy multi-project `Sunny-Gumber/camvault` repository is a frozen recovery/provenance source until the standalone cutover gates pass.

## 1. Current status

As of 2026-09-28, the Intelligent VMS software baseline is:

**RELEASE CANDIDATE / EXTERNAL QUALIFICATION PENDING**

Accepted legacy source baseline for this standalone migration:

`6a97653e596c9dab3ab212c7b045fea2cd3fbb15`

This destination becomes authoritative only after migration issue #1 completes the required public CI/security/Ubuntu validation and the baseline migration PR is merged.

This means the software/SRE/security release-candidate gates are implemented and accepted. It does **not** mean:

- Production Qualified;
- 100K media throughput certified;
- a server/GPU/storage sizing claim is certified;
- all 623 market-feature rows are implemented;
- all cameras/vendors/firmware are interoperable;
- all external certifications/compliance claims are proven.

## 2. New-chat bootstrap

Before changing code, read these files in order:

1. `INTELLIGENT_VMS_START_HERE.md`
2. `intelligent-vms-v1/docs/product/PRODUCT_REQUIREMENTS.md` — permanent product/platform requirements
3. `intelligent-vms-v1/docs/program/MIGRATION_PROVENANCE.md` — standalone/recovery-repository authority and exact legacy evidence
4. `intelligent-vms-v1/docs/program/PROJECT_HANDOFF.md`
5. `intelligent-vms-v1/docs/program/VMS_BOSS_STATE.md` — live cross-chat execution state
6. `intelligent-vms-v1/docs/program/DAILY_BATCH_PROTOCOL.md` — one-push-per-day coordination rules
7. `intelligent-vms-v1/docs/program/IMPLEMENTATION_HISTORY.md`
8. `intelligent-vms-v1/docs/program/PRODUCTION_EXECUTION_PLAN.md`
9. `intelligent-vms-v1/docs/program/MARKET_FEATURE_MASTER_PROGRAM.md`
10. `intelligent-vms-v1/docs/program/AI_CODING_RULES.md`
11. `intelligent-vms-v1/docs/release/PHASE9_RELEASE_CANDIDATE.md`
12. `intelligent-vms-v1/docs/product/FEATURE_CATALOG_SUMMARY.md`

Then inspect **current GitHub main, open issues, open PRs and CI** before claiming status. Repository state is more authoritative than an old chat.

A useful new-chat instruction is:

> Read `INTELLIGENT_VMS_START_HERE.md` and all mandatory linked program files. Inspect current main/open issues/open PRs/CI. Continue the next unblocked Intelligent VMS milestone without recreating completed work. Follow the coding rules, QA/Reviewer/Boss gates, measured-evidence policy and market-feature program.

## 3. Product goal

Build a distributed, multi-tenant, enterprise VMS that can scale toward very large deployments without routing all video through a single central server.

Target architecture:

```text
Global / central control plane
        |
        +-- identity / RBAC / configuration / audit
        +-- metadata / event search / reporting
        +-- placement / orchestration
        |
        +--> Regional / site execution
                |
                +-- Media nodes
                +-- Recording nodes
                +-- AI nodes/providers
                +-- Regional spool / autonomy
                +-- Cameras / devices
```

Continuous video stays regional/distributed. Central services coordinate rather than becoming a 100K-channel media bottleneck.

## 4. Non-negotiable engineering invariants

- Control plane and media plane remain separated.
- Recording is source-copy by default.
- Main stream is used for recording; sub-stream can be used for UI/AI where appropriate.
- AI failure must not stop recording.
- Tenant/site authorization applies to user-visible/mutable resources.
- Camera/service credentials never appear in logs, URLs, committed files or public APIs.
- Distributed ownership uses generation + lease + fencing.
- Provision new owner before destructive stale-owner cleanup.
- Regional autonomy is bounded; no split-brain failover while old offline authority may still be valid.
- Database schema changes use Alembic.
- Hot paths stay bounded; no unbounded 100K scans/retry storms/N+1 loops.
- Hardware/100K/GPU/storage claims require measured evidence.
- A blocker fix gets a regression test.
- Production Qualified requires real external evidence.

## 5. What is already completed

### Phases 1–6
- control/media/event/storage foundation;
- ONVIF discovery/probe/capability onboarding;
- auth/RBAC and tenant/site scope;
- durable media reconciliation;
- continuous source-copy recording;
- retention/timeline/playback;
- event search and camera health;
- ONVIF events, alarms and diagnostics;
- AI orchestration/provider contract.

### Phase 7 — Distributed Regional VMS
Complete at deterministic software-qualification level:
- regional node registry and placement;
- assignment-driven regional media/recording execution;
- node telemetry;
- generation/lease fencing;
- stale-owner rejection;
- bounded WAN/control-plane autonomy;
- regional durable spool/backfill;
- transactional PostgreSQL event outbox;
- replay-tolerant Kafka delivery;
- deterministic 14-scenario chaos gate.

### Phase 8 — Benchmark framework
Software framework complete:
- versioned benchmark evidence schema;
- event/control, reconnect, RTSP workload, recording/storage drivers;
- environment/resource capture;
- measured-only hardware matrix;
- repeatability/reproducibility gate;
- thermal/frequency evidence handling.

**Still pending:** actual target-hardware/environment benchmark evidence. Parent issue #185 remains open intentionally.

### Phase 9 — Production/SRE/security
Software baseline complete:
- Kubernetes/Helm production topology;
- health/readiness/metrics;
- Prometheus/Grafana/alerts/SLO contracts;
- recording-gap monitoring;
- backup/restore and DR exercise tooling;
- upgrade/rollback preflight;
- OIDC production trust rules;
- audit logging and security posture tests;
- network policy / non-root security context;
- External Secrets integration option;
- TLS fail-closed ingress;
- dependency/container vulnerability scanning;
- executable Release Candidate gate.

## 6. Market feature master program

The market checklist is part of product scope:

- exact checklist: `intelligent-vms-v1/docs/product/VMS_COMPLETE_FEATURE_MASTER_CHECKLIST.md`
- generated catalog: `intelligent-vms-v1/docs/product/FEATURE_CATALOG.csv`
- summary: `intelligent-vms-v1/docs/product/FEATURE_CATALOG_SUMMARY.md`
- execution rules: `intelligent-vms-v1/docs/program/MARKET_FEATURE_MASTER_PROGRAM.md`

Current catalog:
- **48 categories**
- **623 deterministic feature rows**

All rows are accepted as product targets, but most must still be audited/implemented/verified. "All features complete" must never be claimed until the market-feature completion rules pass.

Support may be:
- native;
- camera-dependent;
- integration/plugin;
- analytics-server dependent;
- licence/module gated;
- cloud-only;
- on-prem-only.

Every feature needs an intentional delivery path, engineering state, evidence and documented limitation.

## 7. Current open blockers / external work

### Phase 8 real evidence — issue #185
Real target hardware/environment measurements are required before hardware sizing or 100K claims.

### Security residual — issue #261
Inherited unfixed HIGH CVEs currently reported in Python base images. The fixable HIGH/CRITICAL gate is green, but the inherited findings remain a tracked Release Candidate exception and are **not** a Production Qualified waiver.

### Phase 10 external qualification
Still required:
- real-camera interoperability matrix;
- target server/NIC/storage/GPU benchmarks;
- real storage/network/WAN/backfill tests;
- real HA/failure drills;
- measured backup/restore RPO/RTO;
- N-1 -> N upgrade/rollback with recording continuity;
- deployed OIDC/PKI/mTLS/KMS/gateway/SIEM controls;
- penetration/abuse evidence;
- pilot/operator soak.

## 8. Current product labels

Use these exact meanings:

- **Development Accepted** — engineering milestone passed.
- **Release Candidate** — software/SRE/security gates passed.
- **Production Qualified** — real external production evidence passed.
- **Full Market Feature Complete** — every feature-catalog row meets the market-feature completion rules.

Current label:

**Release Candidate / External Qualification Pending**

## 9. How to continue

If the user says **"follow as per plan"**:

1. inspect current main/open issues/open PRs/CI;
2. do not recreate merged milestones;
3. pick the next unblocked work item from:
   - real Phase 8 evidence / Phase 10 external qualification when hardware/site access exists; or
   - Market Feature Master Program tracks A → I when external evidence is unavailable;
4. create one focused branch/PR;
5. implement;
6. run tests/CI/security/performance gates as applicable;
7. independently review the real diff;
8. fix blockers with regression tests;
9. merge only after gates pass;
10. update handoff/status docs when a major milestone changes.

## 10. Repository authority and provenance

This standalone repository contains the Intelligent VMS product only. The original CamVault snapshot-backup product is intentionally **not** migrated here.

Historical evidence may still reference `Sunny-Gumber/camvault` issue/PR numbers. Keep those references explicit as **legacy source evidence**; do not rewrite them as destination issue/PR numbers that never existed.

During migration, follow `intelligent-vms-v1/docs/program/MIGRATION_PROVENANCE.md`. After the standalone baseline is publicly validated and accepted, all new Intelligent VMS development belongs in this repository under:

`intelligent-vms-v1/`

