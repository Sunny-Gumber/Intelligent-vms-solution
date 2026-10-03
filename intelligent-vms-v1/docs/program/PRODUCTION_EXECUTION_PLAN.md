# Intelligent VMS — Production Execution Program

Status: authoritative engineering roadmap for ChatGPT/GitHub execution, human review and any optional repository agents.

## Mission

Complete the Intelligent VMS from the current repository state to a production-ready release candidate, then to Production Qualified only after the required real-camera, hardware, storage, network and failure evidence exists.

This file is the execution order. Do not skip gates because later work appears easier.

## Market feature scope

The product also has a mandatory market-capability program:

- `docs/product/VMS_COMPLETE_FEATURE_MASTER_CHECKLIST.md` — exact source list;
- `docs/product/FEATURE_CATALOG.csv` — deterministic feature IDs and coverage state;
- `docs/program/MARKET_FEATURE_MASTER_PROGRAM.md` — implementation/verification rules.

All 48 numbered checklist categories are accepted into product scope. The generated
catalog currently contains 623 deterministic capability rows. Compound phrases
remain one row unless the source explicitly separates them.

The production program and market-feature program are complementary:
- Phase 8/9/10 prove scale, reliability, security and real-environment readiness;
- the market-feature program proves breadth of functionality;
- neither may fabricate the other's evidence.

A release may be Production Qualified before every optional market capability is
implemented, but it must not be described as **Full Market Feature Complete**
until every catalog row passes the completion rules in the Market Feature Master
Program.

## Current baseline

Already merged:
- Phase 1: architecture/core scaffold
- Phase 2A: ONVIF discovery/capability/onboarding
- Phase 2B: auth/RBAC, migrations, media reconciliation
- Phase 3: source-copy recording, retention, timeline/playback
- Phase 4: event search and camera health
- Phase 5: ONVIF events, alarms, diagnostics
- Phase 6: AI orchestration/provider contract
- Phase 7 Step 1A: regional node registry and placement
- Phase 7 Step 1B: assignment-driven regional media/recording execution

Current program state:
- Phase 7 distributed/regional software qualification is complete and #179 is closed.
- Phase 8 benchmark harness, measured-evidence matrix and reproducibility QA are implemented; #185 remains open for real target-hardware/environment evidence.
- Phase 9 production deployment, observability, backup/DR/rollback, software-security and Release Candidate gates are complete; #189/#190/#191/#192 are closed.
- Accepted software status is **Release Candidate / External Qualification Pending**.
- Release Candidate merge: `c75828efc835f05dabf2d2b7ffdd73e37c0258d6`.
- Real camera, target hardware, storage/network/WAN, site/pilot and deployed security evidence remain required before Production Qualified or hardware/100K claims.
- Issue #261 tracks inherited unfixed HIGH CVEs in the current Python base images; this is a visible Release Candidate exception, not a Production Qualified waiver.
- Market Feature Master Program is the authoritative capability-breadth backlog. The 48-category / 623-row catalog is a target/evidence system, not a claim that every capability is implemented.
- Permanent continuation handoff: `docs/program/PROJECT_HANDOFF.md` and repository-root `INTELLIGENT_VMS_START_HERE.md`.

Existing phase umbrellas:
- #179 Phase 7 distributed regional VMS — complete
- #185 Phase 8 benchmark and hardware qualification — software framework complete, real evidence pending
- #189 Phase 9 production release, observability and SRE — complete
- #201 master production program — open until external qualification / remaining product program work is complete

## Program-wide non-negotiable rules

1. One source of truth per responsibility.
2. Control plane must not carry all video traffic.
3. Recording remains source-copy by default.
4. AI is separately schedulable and may fail without stopping recording.
5. Credentials/tokens must never appear in public APIs, logs, committed files or URLs.
6. Tenant/site authorization applies to every user-visible or mutable resource.
7. Distributed execution uses generation/lease/fencing semantics.
8. New ownership must be ready before destructive cleanup of old ownership.
9. Every schema change uses Alembic and CI migration checks.
10. No unbounded 100K-camera scans, retry storms, N+1 hot paths or giant in-memory state loads.
11. No "cameras per server", 100K, GPU, storage or latency claim without measured workload evidence.
12. Single-node mode remains a valid deployment profile until intentionally retired.
13. Every blocker fixed must gain a regression test.
14. Every production milestone requires QA evidence and independent Reviewer PASS.
15. "Production Qualified" is forbidden until external qualification gates are attached.

---

# Phase 7 — Distributed Regional VMS

Parent: #179

## 7.1C-A — Regional Node Agent & Resource Heartbeats
Tracking: #196 / PR #199

Owner agents:
- Architect: topology and trust boundary
- Developer: node-agent + control API integration
- Security: node-scoped identity and endpoint trust
- QA: metrics/backoff/auth regression
- Reviewer: independent sign-off
- Performance: metric semantics only; no synthetic hardware claims

Required:
- node identity, region and roles
- CPU/RAM/disk/NIC/uptime measurements
- MediaMTX reachability and path counters
- bounded heartbeat/retry behavior
- service-token node scope
- node service cannot mutate trusted public/control endpoints
- node-scoped token cannot run cluster-wide placement
- default single-node deployment unchanged

Exit gate:
- branch updated to current main
- both VMS CI and repo CI green
- compile/tests/Compose/container build green
- Security PASS
- Reviewer PASS
- Boss merge decision

## 7.1C-B — Execution Fencing & Lease Enforcement

Goal:
A node may execute media/recording/AI ownership only while it owns the current assignment generation and valid lease.

Required:
- node-side assignment generation cache
- lease refresh/expiry behavior
- fencing token/generation on work acquisition
- stale owner stops starting new work
- old owner cannot reassert after failover
- safe clock-skew policy
- controller restart recovery
- failback behavior
- no duplicate long-running recorder after fencing

Tests:
- A -> B failover
- A disconnected, B becomes owner
- A reconnects with stale generation and is rejected
- B loses lease
- controller restart
- concurrent controller replicas
- repeated A -> B -> C -> A

Exit gate:
- no BLOCKER/HIGH data-loss or split-brain finding
- chaos tests deterministic in CI where possible

## 7.1C-C — Regional Autonomy During WAN/Control Loss

Goal:
Existing local media and recording continue during central control-plane outage.

Required:
- cached desired state at regional/node layer
- explicit offline mode
- local recording continues
- active live paths continue where network permits
- no destructive cleanup while ownership authority is uncertain
- queued health/events/backfill on reconnect
- bounded reconnect storm behavior
- operator-visible degraded/offline state

Test:
- remove control API/network
- verify existing recording continues
- restore connection
- reconcile without duplicate recorders or losing retained segments

## 7.2 — Reliable Event Delivery

Required before production:
- transactional outbox or equivalent DB/Kafka delivery boundary
- idempotent consumers/event IDs
- bounded retry/DLQ policy
- no silent loss when DB commit succeeds but broker publish fails
- replay/repair runbook

## 7.3 — Regional Failure/Chaos Gate

QA + Reviewer must execute:
- media node loss
- recording node loss
- controller loss
- DB/broker temporary outage
- stale heartbeat
- disk full/near full
- unreachable old failover node
- reconnect storm
- region isolation
- tenant isolation

Phase 7 exit:
- #179 can close only when distributed ownership, failover, fencing and regional autonomy pass.
- Do not claim 100K throughput here. That belongs to Phase 8.

---

# Phase 8 — Benchmark & Hardware Qualification

Parent: #185
Existing workstreams: #186, #187, #188

Purpose:
Replace assumptions with measured coefficients and produce deployable hardware profiles.

## 8.1 — Benchmark Harness

Build repeatable tools for:
- logical camera/control-plane load
- RTSP/media source generation
- live viewer load
- recording write throughput
- playback read load
- ONVIF/event generation
- Kafka/ClickHouse ingest/query load
- AI provider/inference load
- reconnect/failover storms
- resource telemetry capture

Every result record must include:
- commit SHA
- hardware model/CPU/RAM/NIC/storage/GPU
- OS/kernel/container runtime
- test config
- warmup
- duration
- workload
- p50/p95/p99
- CPU/RAM/NIC/disk/GPU utilization
- failures/drops/reconnects

## 8.2 — Scale Levels

Run increasingly:
1. 10–50 camera functional reference
2. 500-camera site class
3. 2,000-camera site/cluster class
4. 10,000-camera regional simulation
5. 100,000 logical-channel control/event simulation

100K media recording may require distributed generators/hardware; never infer it from a tiny test.

## 8.3 — Hardware Qualification

Produce measured node classes:
- control/API
- media relay
- recording
- event/broker/search
- AI
- storage tier

For each class state:
- CPU
- RAM
- NIC
- OS/boot storage
- media storage
- sustained safe throughput
- measured test
- N+1 headroom
- known bottleneck
- scale-out trigger

GPU recommendations require model/resolution/FPS/precision measurements.

## 8.4 — Storage Qualification

Measure:
- sequential write/read
- concurrent segment close/open
- filesystem/object-store latency
- WAL/index effects
- retention cleanup
- object request rate
- local spool/backfill after WAN outage
- recovery after full disk

## 8.5 — Benchmark Acceptance

Phase 8 closes only when:
- result schema and harness are versioned
- results are reproducible
- hardware matrix references actual result IDs/files
- no recommendation exceeds tested safe utilization
- Performance Agent and QA sign off
- Reviewer confirms no unsupported claims

---

# Phase 9 — Production Release, Security, Observability & SRE

Parent: #189
Existing workstreams: #190, #191, #192

## 9.1 — Production Deployment

Required:
- Kubernetes/Helm or documented supported production orchestrator
- separate control/media/recording/event/AI workloads
- readiness/liveness
- pod/node disruption behavior
- regional topology
- resource requests/limits from Phase 8 evidence
- rolling rollout
- rollback

## 9.2 — Identity, Secrets & Transport Security

Required:
- OIDC/JWT production trust
- service identities
- secrets manager/KMS integration
- TLS/mTLS for sensitive internal boundaries
- certificate rotation procedure
- no static production secrets in repository
- tenant/site authorization tests
- audit logging
- rate limits/abuse controls

## 9.3 — Observability

Required:
- Prometheus metrics
- dashboards
- alerts for camera/media/recording/node/broker/DB/AI
- SLOs and alert thresholds
- recording-gap detection
- node saturation
- disk exhaustion
- event lag
- outbox/DLQ backlog
- auth/security signals

## 9.4 — Backup/Restore & Disaster Recovery

Must be exercised, not documented only:
- PostgreSQL backup/restore
- ClickHouse backup/restore or rebuild strategy
- object/media storage recovery/lifecycle
- configuration/secret recovery
- region loss scenario
- documented RPO/RTO
- restore evidence

## 9.5 — Upgrade/Rollback

Test:
- N-1 -> N migration
- rolling service upgrade
- mixed-version compatibility where supported
- failed migration/rollout recovery
- rollback limitations documented
- media recording continuity checked

## 9.6 — Production Security Review

Security Agent + Reviewer:
- auth/RBAC/tenant isolation
- SSRF/endpoints
- camera secrets
- dependency/container scanning
- model artifact integrity
- webhook/internal callback auth
- evidence/export permissions
- audit trail
- DOS/resource exhaustion
- network policies

## 9.7 — Release Candidate Gate

A release candidate requires:
- all software CI green
- migration tests green
- backup/restore exercised
- upgrade/rollback exercised
- SRE runbooks complete
- security review PASS
- no unresolved BLOCKER/HIGH issue accepted without explicit release decision
- benchmark matrix available

This status is **Release Candidate**, not yet Production Qualified.

---

# Phase 10 — External Production Qualification

This phase requires environment evidence outside normal CI.

## 10.1 — Real Camera Interoperability Matrix

At minimum test target production vendors/models/firmware for:
- discovery/onboarding
- main/sub stream
- H.264/H.265
- events
- PTZ where applicable
- recording/playback
- reconnect
- time sync
- analytics metadata where applicable

## 10.2 — Real Storage/Network Qualification

Exercise:
- target storage hardware/object store
- target NIC/switching
- packet loss/jitter
- WAN outage/backfill
- full/near-full storage
- sustained recording duration

## 10.3 — Pilot Deployment

Recommended progression:
- 10–50 real cameras
- 24–72 hour soak
- then 100s/500+ according to available site
- failure drills
- operator workflow validation

## 10.4 — Production Qualified Gate

Only Boss may declare Production Qualified, and only when evidence exists for:
- real camera matrix
- target hardware benchmark
- storage/network qualification
- HA/failover drills
- backup/restore
- upgrade/rollback
- security sign-off
- operational runbooks
- known limitations

If external hardware/site evidence is unavailable, stop at **Release Candidate / External Qualification Pending**. Do not fabricate completion.

---

# Agent Assignment Matrix

| Work | Primary | Mandatory reviewers/support |
|---|---|---|
| standards/protocol questions | VMS Research | Architect |
| system/data/API design | VMS Architect | Security, Performance as needed |
| implementation | VMS Developer | QA |
| functional/failure validation | VMS QA | Reviewer |
| final engineering review | VMS Reviewer | Boss |
| CPU/RAM/NIC/storage/GPU | VMS Performance | QA |
| security/trust/secrets | VMS Security | Reviewer |
| deploy/monitor/backup/DR | VMS SRE | Security, QA |
| phase acceptance | VMS Boss | requires QA + Reviewer evidence |

# Boss Loop

For each milestone:

```text
Inspect current main/issues/PRs/CI
        ↓
Select next unblocked milestone
        ↓
Research/Architect as required
        ↓
Developer
        ↓
QA
        ↓
Performance/Security/SRE gates as applicable
        ↓
Reviewer
        ↓
Blocker? ── yes ──> responsible agent -> regression test -> repeat
        │
        no
        ↓
Boss acceptance
        ↓
Merge one reviewable PR
        ↓
Update issue/report
        ↓
Next milestone
```

Never run two overlapping implementations of the same milestone. Close or supersede duplicates before continuing.
