# Intelligent VMS — Project Handoff

**Audience:** future ChatGPT chats, engineers, reviewers and project owners.

**Status snapshot date:** 2026-09-28

**Accepted software status:** Release Candidate / External Qualification Pending

**Release Candidate merge:** `c75828efc835f05dabf2d2b7ffdd73e37c0258d6`

This document is a human-readable handoff. Always verify current `main`, open issues, open PRs and CI before making a new factual status claim.

---

## 1. Why this project exists

The Intelligent VMS was created to move beyond a simple CCTV backup product into a full enterprise video-management platform.

The goal is not a single large server. The design is a distributed regional system that can grow toward very large camera estates while keeping media/recording close to cameras and using central services for orchestration, metadata, search, identity, audit and policy.

The project intentionally separates:

- control-plane scale;
- media/relay scale;
- recording/storage scale;
- event/metadata scale;
- AI inference scale;
- playback/export scale;
- health/observability scale.

No one number such as "cameras per server" is treated as universally valid.

---

## 2. Repository layout

### Legacy / original product

The repository root contains the original CamVault snapshot-backup MVP. It is still valid as a separate product history.

### Intelligent VMS

The enterprise VMS lives in:

`intelligent-vms-v1/`

For VMS work, prefer project-local documentation and code under this directory.

Important roots:

- `services/control-api/` — central API/control logic;
- `services/node-agent/` — regional node telemetry/fencing/autonomy;
- `services/regional-spool/` — WAN-loss durable regional queue;
- `services/event-writer/` — event/recording metadata writer;
- `services/onvif-event-worker/` — ONVIF event ingestion;
- `services/alarm-worker/` — alarm/rule processing;
- `services/placement-controller/` — placement execution loop;
- `infra/mediamtx/` — media layer;
- `migrations/` — Alembic database history;
- `tests/` — deterministic regression/qualification tests;
- `tools/` — sizing, benchmark, release, feature-catalog and qualification tooling;
- `docs/` — architecture, operations, performance, product, release and program governance;
- `deploy/` / Helm assets — production deployment/SRE artifacts.

---

## 3. Core architecture

### Control plane

Responsibilities:
- tenant/site/camera identity;
- RBAC/authorization;
- configuration;
- ONVIF onboarding;
- placement ownership;
- health state;
- alarms;
- normalized event acceptance;
- audit;
- search/control APIs;
- release/operator status.

The control plane must not become the media bottleneck.

### Media plane

MediaMTX-based regional execution:
- RTSP ingest;
- browser WebRTC/HLS output;
- node-aware path provisioning;
- regional execution;
- health/path diagnostics.

### Recording plane

- source-copy recording;
- main-stream recording;
- independent recording-node placement;
- retention/timeline/playback;
- recording metadata;
- generation/node evidence attached to recording callbacks;
- stale-evidence rejection.

### Event/metadata plane

- Kafka-compatible event backbone;
- PostgreSQL transactional outbox for DB-backed events;
- regional durable spool for WAN loss;
- ClickHouse search/index path;
- alarm processing;
- idempotent/replay-tolerant event identity.

### AI plane

AI is orchestrated separately from recording. It can be placed/scaled independently and must not become a dependency for continuous recording.

---

## 4. Development history — functional phases

### Phase 1 — Foundation

Built the first Intelligent VMS foundation:
- FastAPI control API;
- PostgreSQL control state;
- MediaMTX media layer;
- Kafka-compatible event backbone;
- ClickHouse event store;
- camera registry;
- basic web wall;
- Compose development environment.

Key principle established: central control, distributed media.

### Phase 2A — ONVIF onboarding

Added:
- WS-Discovery/manual probe;
- device information;
- capability snapshot;
- media profiles;
- stream URI selection;
- automatic main/sub stream selection;
- manual RTSP fallback;
- SSRF/network-target hardening;
- XML/device-input hardening.

### Phase 2B — Platform hardening

Added:
- JWT/RBAC;
- tenant/site scope;
- Alembic migration discipline;
- durable MediaMTX path reconciliation;
- CI migration/image gates;
- stronger auth/network boundaries.

### Phase 3 — Recording and playback

Added:
- continuous source-copy recording;
- main-stream recording separated from live sub-stream;
- fMP4 segmentation;
- retention policy;
- recording metadata;
- timeline;
- playback;
- bounded operator APIs.

### Phase 4 — Events and health

Added:
- tenant/site-scoped event search;
- batched/regional camera-health worker;
- bounded RTSP transport probes;
- offline/recovery hysteresis;
- normalized camera health events;
- health summary;
- sizing tools.

### Phase 5 — ONVIF events, alarms, diagnostics

Added:
- ONVIF event worker/subscription flow;
- normalized event handling;
- alarm/rule functionality;
- advanced diagnostics;
- event pipeline hardening.

### Phase 6 — AI orchestration

Added:
- AI provider/orchestration contract;
- camera AI policy/configuration;
- AI result normalization/publishing;
- separation of AI scheduling from recording ownership.

No model/hardware capacity was certified merely by adding orchestration.

---

## 5. Phase 7 — Distributed Regional VMS

Phase 7 is complete at deterministic software-qualification level.

### 7.1A — Regional registry and placement

PR #194  
Merge: `197be3033ab2a3a9a374455f14995864aa58abc9`

Added:
- infrastructure-node registry;
- region/site mapping;
- role-based placement;
- capacity/load fields;
- heartbeat staleness;
- headroom rules;
- bounded controller batches;
- movement caps;
- deterministic node scoring;
- persisted cursor;
- PostgreSQL advisory leadership lock.

Unknown/zero capacity is not treated as infinite capacity.

### 7.1B — Assignment-driven regional execution

PR #195  
Merge: `91923888d757ae89a5d8c767046beb408fffda57`

Added:
- node-aware MediaMTX/playback resolution;
- distributed onboarding/provisioning states;
- assigned-node live/HLS URLs;
- node-aware health;
- recording on assigned recording node;
- node-aware timeline/playback;
- actual recording-node identity in callbacks;
- durable stale-node cleanup list;
- repeated failover/failback cleanup;
- fail-safe distributed delete;
- single-node fallback preserved.

### 7.1C-A — Trustworthy node heartbeats

PR #199  
Merge: `2d952bca9a8b5ae5d4c99971ff13da4fa549483e`

Added:
- node CPU/RAM/disk/NIC/uptime telemetry;
- MediaMTX path measurements;
- bounded heartbeat retry;
- node-scoped service identity;
- admin-only trusted node registration/endpoints/capacity;
- heartbeat carries measured load only;
- node cannot self-register;
- no fabricated recording Mbps/AI load;
- missing placement-critical measurement makes a role ineligible instead of "0% loaded."

### 7.1C-B — Generation/lease fencing

PR #204  
Merge: `14c81535d105e7740cb931ee182ce4063910e3f0`

Added:
- durable assignment generations;
- durable placement revocations;
- persisted local fence state;
- lease expiry;
- stale-owner rejection;
- recording callback node+generation+lease validation;
- shared PostgreSQL execution fence between placement and media mutation;
- stale snapshot rejection;
- delayed revocation protection;
- safe A -> B -> C -> A failover/failback.

This phase fixed a real race found during review: stale control-plane media mutation was not allowed to race a newer placement generation.

### 7.1C-C — Regional autonomy during WAN/control loss

PR #206  
Merge: `b9bd3982811c126d9b2f60487edae8acf737fb35`

Added:
- bounded pre-granted offline authority;
- regional autonomous mode;
- fenced degraded mode;
- central failover deferment while old offline authority remains valid;
- persistent authority state;
- regional durable spool;
- recording/event/heartbeat backfill;
- historical generation-window validation;
- observed-at heartbeat timestamps;
- stale spooled telemetry cannot appear fresh after reconnect;
- nodes must resynchronize centrally before authority renewal.

Autonomy is bounded to avoid split-brain ownership.

### 7.2 — Reliable event delivery

PR #208  
Merge: `0891f391063fddbccb92352292164279de462795`

Added:
- PostgreSQL transactional event outbox;
- deterministic event/segment identity;
- `FOR UPDATE SKIP LOCKED` multi-replica claims;
- claim tokens/leases;
- broker outage retry with bounded backoff;
- poison-message dead-letter state;
- admin requeue path;
- lazy Kafka connection/reconnect;
- replay-tolerant event sinks/timeline behavior.

This closed the "DB committed but Kafka event disappeared" reliability gap.

### 7.3 — Final regional chaos qualification

PR #210  
Merge: `735979f034041d1712747f6da3f6df3900afd792`

Added a deterministic 14-scenario software chaos matrix covering:
1. media-node loss;
2. recording-node loss;
3. controller interruption/restart;
4. temporary PostgreSQL failure;
5. Kafka outage/recovery;
6. stale heartbeat;
7. unreachable old failover node;
8. repeated A -> B -> C -> A;
9. delayed fence snapshot/revocation;
10. WAN/control isolation;
11. reconnect storm/backlog drain;
12. regional spool full;
13. fence-state disk persistence failure;
14. tenant/site/service isolation.

Phase 7 result: **Development Accepted**.

Boundary: this does not certify physical hardware, WAN equipment, disk arrays or 100K media throughput.

---

## 6. Phase 8 — Benchmark and hardware evidence framework

### 8.1 Benchmark harness

PR #211  
Merge: `3140c7a60422eec3bdce5aa8c3e7df106ea7d700`

Added:
- versioned benchmark-result schema;
- exact commit/environment capture;
- CPU/RAM/NIC/storage/GPU inventory;
- warmup/duration;
- p50/p95/p99;
- failure rate;
- event/control HTTP workload;
- reconnect-storm workload;
- RTSP source/viewer workload plans;
- recording/storage drivers;
- resource sampling;
- JSON/CSV evidence output.

### 8.2 Measured hardware matrix

PR #212  
Merge: `03edf3ae5a86cb591bd3bf257296bcbd0b84d574`

Added:
- measured-only hardware qualification;
- minimum repeat count;
- same software/hardware fingerprint requirement;
- minimum duration/warmup;
- CPU/RAM/failure thresholds;
- conservative minimum observed capacity;
- design headroom;
- N+1 node count;
- explicit UNQUALIFIED state for missing evidence.

### 8.3 Reproducibility and thermal QA

PR #213  
Merge: `3e410551c15587d0410c6cd1c5fe5bc56a4349e0`

Added:
- reproducibility validator;
- workload-specific capacity repeatability;
- thermal/frequency evidence handling;
- evidence fingerprint binding;
- qualification requires reproducibility PASS IDs;
- unknown hardware telemetry stays unmeasured, never guessed as zero.

### Phase 8 current boundary

Software framework is complete. Parent issue #185 remains open because **real target hardware/environment benchmark results are not yet attached**.

Do not convert the framework itself into claims such as:
- "X cameras per server";
- "100K cameras supported on Y nodes";
- "GPU Z can run N streams";
- "storage array supports N days."

Those claims require measured evidence.

---

## 7. Market Feature Master Program

The user supplied a vendor-neutral VMS feature master checklist covering Level 1 through Level 5 plus extended categories.

Repository source of truth:
- `docs/product/VMS_COMPLETE_FEATURE_MASTER_CHECKLIST.md`

Generated catalog:
- `docs/product/FEATURE_CATALOG.csv`

Summary:
- **48 numbered categories**
- **623 deterministic capability rows**

Program:
- `docs/program/MARKET_FEATURE_MASTER_PROGRAM.md`

The product objective is to give every catalog row:
- a support mode;
- an engineering state;
- dependencies;
- automated/device/infrastructure/certification evidence as appropriate;
- known limitations;
- verification date.

All rows are accepted into product scope, but that does not mean all rows are already implemented.

Tracks:
- A Core VMS — categories 1–8;
- B Professional VMS — 9–14;
- C Analytics/metadata/identity — 15–19;
- D Physical-security/control room — 20–22;
- E Enterprise platform — 23–28;
- F Integration/GIS/workflow — 29–31;
- G AI/command centre — 32–35;
- H Cloud/optimization/BI/evidence/commercial — 36–41;
- I Extended market — 42–48.

Current default import state remains Roadmap/TARGET/Not Verified until an existing or new implementation is audited against the row.

### Track A1 — Category 1 evidence audit (2026-09-28)

PR #265 merged the validated evidence overlay so catalog regeneration preserves
reviewed statuses without changing master checklist identities. Category 1 has
43 audited rows: 1 scoped RTSP software path is in `QA`, 42 are still `TARGET`,
and every row remains `NV` for final verification. The other 580 rows remain
`R / TARGET / NV`. See `docs/reviews/CATEGORY1_CAMERA_CAPABILITY_AUDIT.md`
for each feature's evidence and limitation. This is not real-camera or 100K
qualification. The next implementation cluster is safe camera lifecycle:
manual/ONVIF target authorization and metadata/error redaction, diagnostic
detail redaction, recording-aware deletion, and
tenant/site-scoped camera updates and credential rotation.
The first security/correctness slice (manual/ONVIF target authorization,
public metadata/error/health redaction, and recording-aware deletion) is
tracked in #266; camera updates and device write configuration follow later.

---

## 8. Coding-quality program

The project adopted:
- `docs/program/AI_CODING_RULES.md`

Rules cover:
- formatting/naming;
- function design;
- error handling;
- documentation;
- testing;
- module structure;
- dependencies;
- security;
- performance;
- version control;
- APIs;
- data handling;
- architecture;
- product/business alignment.

Later cleanup PRs added:
- public interface docstrings;
- incremental Ruff linting;
- AST-based public-docstring gate;
- CI enforcement;
- no broad mass-suppression strategy.

New work must follow the rule priority:

**Security -> Correctness/tests/error handling -> Product alignment -> Architecture -> Readability/style**

---

## 9. Phase 9 — Production/SRE/security baseline

### 9.1 Kubernetes/Helm foundation

PR #217  
Merge: `83e8a42e95cdf8431fd5459c96c58a9e462b1df0`

Added:
- Helm/Kubernetes production topology;
- workload separation;
- readiness/liveness;
- production web routing;
- metrics foundation;
- deployment validation.

### 9.2 Observability / alerts / SLOs

PR #258  
Merge: `fa5147e461007dca004c04f89341eaf0b412fe80`

Added:
- recording completion health/gap tracking;
- Prometheus metrics;
- saturation/unmeasured-capacity signals;
- outbox backlog/age metrics;
- alert bundle;
- Grafana SRE dashboard;
- external PromQL integration contract;
- SLO/triage documentation.

### 9.3 Backup, restore, DR, upgrade/rollback

PR #259  
Merge: `b16eca8a789b9cf3f5209943ed86bb476d4e9b3f`

Added:
- PostgreSQL backup/restore tooling;
- ClickHouse backup/restore tooling;
- regional fence/spool backup/restore;
- Helm config/history backup;
- upgrade preflight;
- evidence manifest + hash verification;
- deterministic exercise RPO/RTO calculation;
- explicit safety confirmations for destructive restores;
- rollback/restore ordering runbook.

Important: exercise evidence is not a production RPO/RTO guarantee.

### Production security baseline

PR #260  
Merge: `1e94fdc4db743ec1912eab5bc7d9afbb6542258d`

Added:
- OIDC/JWKS production trust requirements;
- fail-closed production auth posture;
- structured security audit logging;
- network policy;
- non-root containers;
- RuntimeDefault seccomp;
- dropped Linux capabilities;
- optional External Secrets integration;
- TLS fail-closed ingress;
- pinned pip-audit;
- Trivy image scanning;
- blocking fixable HIGH/CRITICAL security gate;
- production security runbook.

Residual issue #261 tracks inherited unfixed HIGH CVEs in the Python base images. It is a conditional RC exception only.

### Release Candidate gate

PR #262  
Merge: `c75828efc835f05dabf2d2b7ffdd73e37c0258d6`

Added:
- executable release-candidate gate;
- machine-readable release policy;
- repository evidence hashing;
- feature-catalog integrity check;
- external-evidence policy;
- release-state semantics;
- regression tests for path safety/evidence requirements.

Accepted machine status:

`RELEASE_CANDIDATE_EXTERNAL_QUALIFICATION_PENDING`

Phase 9 parent #189 is closed as a software milestone.

---

## 10. Current unresolved work

### A. Real Phase 8 evidence — #185

Need real target hardware/environment results for:
- control/API;
- media relay;
- recording;
- Kafka/event;
- ClickHouse;
- storage;
- AI;
- network;
- sustained safe headroom.

### B. Phase 10 external qualification

Need real evidence for:
- camera/vendor/firmware interoperability;
- real storage/network/WAN behavior;
- real HA/failure drills;
- target backup/restore RPO/RTO;
- upgrade/rollback with recording continuity;
- production identity/PKI/mTLS/KMS/gateway/SIEM setup;
- penetration/abuse testing;
- real pilot/operator soak.

### C. Security issue #261

Inherited unfixed HIGH CVEs in current Python base images remain tracked. Close only with:
- upstream fixes;
- a demonstrably safer supported base image; or
- documented product-specific reachability/security acceptance.

### D. Market-feature implementation

623 rows are targets, not automatically completed. Existing functionality must first be audited against the catalog, then missing capability tracks implemented and verified.

---

## 11. Release language

Never collapse these labels:

### Development Accepted
A software milestone passed engineering gates.

### Release Candidate
Software/SRE/security release baseline passed.

### Production Qualified
External real-environment evidence passed and Boss accepted it.

### Full Market Feature Complete
Every catalog row met the Market Feature Master Program completion rules.

Current status:

**Release Candidate / External Qualification Pending**

**Not Production Qualified.**

**Not Full Market Feature Complete.**

---

## 12. Working protocol for future chats

When continuing:

1. read `INTELLIGENT_VMS_START_HERE.md`;
2. read this handoff;
3. inspect current main/open issues/open PRs/CI;
4. read the relevant architecture/runbook/tests before editing;
5. obey `AI_CODING_RULES.md`;
6. preserve control/media/recording/AI separation;
7. use one focused branch/PR per milestone;
8. add regression tests for blockers;
9. run normal CI + relevant security/performance/SRE gates;
10. independently review the actual diff;
11. merge only after evidence is green;
12. update this handoff when a major program state changes.

If external hardware/site evidence is unavailable, continue the Market Feature Master Program rather than inventing qualification evidence.


## 13. Track A1 Category 1 completion — 2026-10-01

PR #276 merged at `812052561d7b44fe228db8d6e25293c051081bee` after independent QA/security review of head `bd127673194b024b2c930efb2b7b93602c7ff025`.

Software gates on the reviewed head passed: repository and VMS tests, Ruff/compile, 623-row catalog verification, Phase 7 chaos 14/14, Phase 8 benchmark smoke, Alembic through 0016, Compose, Helm, VMS image builds, dependency audit, and fixable HIGH/CRITICAL Trivy enforcement.

Category 1 now includes the accepted software routes for bounded site-local serial onboarding, credential-safe QR backend onboarding, managed third-stream lifecycle/distributed fencing and cleanup, advertised codec-profile selection, standards-advertised orientation handling, and the manufacturer-driver registry contract. Device-dependent rows remain NV until named vendor/model/firmware evidence exists. The QR market feature also remains constrained by the documented client/scanner boundary.

Report-only inherited Debian HIGH findings for which Trivy reports no fixed package remain tracked under #261. They are not suppressed and are not a Production Qualified waiver.

Product status remains **Release Candidate / External Qualification Pending**. This does not establish Production Qualified status, 100K-camera throughput certification, complete 623-feature coverage, or universal manufacturer compatibility.

Next Track-A recommendation: perform the Category 2 — Live Monitoring evidence-first catalog audit under parent #221, then scope only genuinely missing software-verifiable gaps into a focused milestone.


## Track A2 Category 2 first milestone — 2026-10-01

- Starting main for audit: `dc95569387f04b187ad3fba61fd048ec8c8faf60`.
- Category 2 Live Monitoring: all **21** Feature IDs audited in `docs/reviews/CATEGORY2_LIVE_MONITORING_AUDIT.md`.
- Issue #277 completed; PR #278 merged.
- Reviewed PR head: `0736010327a27210be3e3777bdb16d207d30d633`.
- Merge commit: `9af20f79d729885340481a3b99e688f701f056dd`.
- Delivered P0 software prerequisite: tenant/site-authorized, short-lived RS256 grants with one exact MediaMTX read-path permission; MediaMTX validates locally from cached JWKS with issuer/audience checks. Browser grants stay in Authorization headers, not media URLs/query strings.
- Development WHEP client now uses authenticated ICE/session setup and rejects cross-origin WHEP session Locations before retaining teardown credentials.
- Recording source/path/policy, AI, placement generation/lease/fencing and Category-1 behavior were not changed.
- Exact-head gates passed: CI; Intelligent VMS CI; compile/Ruff; full VMS unit suite; 623-row/48-category catalog; Phase 7 chaos; Phase 8 benchmark smoke; Phase 9 recovery/RC gate; Alembic; Compose; Helm; VMS image builds; dependency audit; fixable HIGH/CRITICAL Trivy enforcement.
- Category-2 real camera/browser/codecs, audio, TURN/NAT, TLS deployment, WAN behavior, viewer concurrency, failover visual continuity and soak remain **NV / External Qualification Pending**.
- Security residual #261 remains: report-only Debian images show inherited HIGH findings without current fixed package versions; no gate is waived.
- Product status remains **Release Candidate / External Qualification Pending**.
- Next recommended Track-A milestone: **F02-007 bounded live-session control/quotas and explicit start/stop semantics**. It is not implemented yet.


## Track A2 Category 2 bounded live-view resource milestone — 2026-10-01

- Starting main: `89789537d6170095ec5536e1d5c97086422d7bb2`.
- Issue #279 completed; PR #281 merged.
- Accepted PR head: `718c8abaada5793f0f1a56b0a0028de994580c0d`.
- Merge commit: `2b1587e17edf218c0c57d13b88fc7b82194edb1c`.
- Feature IDs affected: F02-001, F02-007 and F02-012 as a resource-safety foundation.
- MediaMTX remains authoritative for active reader/WebRTC/HLS lifecycle. Live and optional third paths now carry configurable non-zero `maxReaders`; continuous recording paths deliberately do not.
- Legacy and distributed reconciliation detect missing/stale live reader policy and reapply it to existing live/third paths without waiting for a source/placement change.
- Default `LIVE_VIEW_MAX_READERS_PER_PATH=16` is a safety guardrail only, not measured capacity or a sizing/certification claim.
- Token replay remains possible during grant validity, but one live path cannot create readers beyond the configured media-plane ceiling. Grant expiry still does not revoke an established session.
- Browser crash/network-loss cleanup timing remains MediaMTX-owned and externally qualified. No duplicate durable VMS session registry was introduced.
- Exact per-principal/per-tenant active-session quotas across cameras are not claimed; distributed identity-aware quota/fairness remains future work.
- Safe signing-key rotation/JWKS overlap is separately tracked in #280.
- Exact-head CI PASS: generic CI; Intelligent VMS CI; compile/Ruff/coding policy; full unit suite; 623-row/48-category catalog; Phase 7 chaos; Phase 8 benchmark smoke; Phase 9 recovery/repository gates; Alembic; Compose; Helm; VMS image builds; dependency audit; Trivy fixable HIGH/CRITICAL enforcement.
- Independent adversarial review: PASS after fixing rollout reconciliation for already-existing paths and updating the Category-1 cleanup fixture to remain policy-current.
- Security residual #261 remains open for inherited Debian HIGH findings without available fixed package versions; no Trivy/security gate was weakened.
- Product remains **Release Candidate / External Qualification Pending**.
- Recommended next Category-2 milestone: **selectable fixed/custom live layouts plus selected-camera navigation/drag-drop**, while preserving the new live reader ceiling. Do not implement it as part of #279.

## Track A2 Category 2 signing-key rotation milestone — 2026-10-01

- Starting main: `a838729aa553111c810bb03783dfc274c480009e`.
- Issue #280 completed; PR #282 merged.
- Accepted/reviewed PR head: `c6f897c2d29a448c0cfc40ec46e40ef7a576d594`.
- Merge commit: `38abde20cdb09f1535bfccd5d45ee552ded4fc4d`.
- Independent adversarial review/QA evidence: PR review ID `5376091197`; REVIEW PASS / QA PASS with no blocker.
- Delivered safe RS256/JWKS signing-key rotation: one active private signer plus at most four public-only overlap verification keys, deterministic `kid`, duplicate/invalid/weak/private/excess keyring rejection, and fail-closed token issuance/startup validation.
- Routine rotation requires pre-publishing the next public key, refresh/evidence on every browser-facing MediaMTX node before signer cutover, old-token overlap through TTL plus rollout/clock-skew safety, and explicit retired-key removal/refresh verification.
- MediaMTX v1.21.1 JWKS caching and trusted `POST /v3/auth/jwks/refresh` behavior are documented. Partial regional refresh is explicitly incomplete and can cause node-dependent authorization failures.
- Emergency compromise intentionally removes the compromised public key and invalidates outstanding grants after forced regional refresh; already-established WebRTC sessions are not claimed to be forcibly terminated by JWT rotation.
- Recording source/path/policy/retention, media path provisioning, placement generation/lease/fencing, AI source selection and browser media-session implementation were not changed.
- Exact reviewed head gates PASS: Intelligent VMS compile/unit, vms-test, static/image build, dependency audit and container audit; publish-image skipped as expected.
- Security residual #261 remains open for inherited Debian HIGH findings without available fixed package versions; no security gate was weakened.
- Product remains **Release Candidate / External Qualification Pending**. This milestone does not establish Production Qualified status, universal device compatibility or full Category-2 qualification.
- Category-2 grid/layout work was not started as part of #280.

## Track A2 Category 2 multi-camera live-view grid milestone — 2026-10-01

- Starting main: `90ad8647ca3882836e0c2673a2506bb539696956`.
- Issue #283 completed; PR #284 merged.
- Accepted/reviewed PR head: `71fff275a8afb6b219eed822d0cba1d179286791`.
- Merge commit: `3bdfec6273102ee817d9895f60909525deacd1c4`.
- Independent adversarial review/QA evidence: PR review `5376541868`; REVIEW PASS / QA PASS at deterministic software boundary.
- Delivered fixed 1x1/2x2/3x3/4x4 live layouts, authorized camera-to-tile assignment, active tile, accidental duplicate prevention, independent WHEP tile lifecycle, explicit stop/remove, manual retry, generation-fenced stale async rejection, page/layout cleanup and MediaStream reattachment across DOM reconciliation.
- Review blockers fixed before acceptance: active streams becoming invisible after grid DOM reconciliation; created WHEP sessions not deleted after later negotiation failure; retry double-click suppression; a test escaping defect.
- #278 path-scoped authorization, #281 MediaMTX maxReaders and #282 signing/JWKS rotation are reused unchanged. No second media/auth/quota subsystem was introduced.
- Recording source/path/policy/placement/fencing/completion and AI source selection were not changed. Regression tests verify recording paths remain continuous/non-on-demand and do not inherit live maxReaders.
- Feature evidence: F02-001 remains software QA; F02-002 fixed layouts now software QA with custom/saved layouts still incomplete; F02-007 explicit tile start/stop lifecycle software QA; F02-008 remains partial because drag/drop gesture is not implemented; F02-010 remains missing; F02-012 remains partial; F02-018 remains partial.
- Exact-head gates PASS: generic CI, Intelligent VMS compile/unit, full VMS workflow including 623-row catalog, Phase 7 chaos, Phase 8 smoke, Phase 9 recovery/RC gate, Alembic, Compose, Helm and image builds, dependency audit and container audit.
- Real camera/browser codecs, TURN/NAT/WAN, sixteen-stream workstation/GPU capacity, peer cleanup timing after crashes, failover visual continuity and soak remain NV / External Qualification Pending.
- Security residual #261 remains open; no security gate was weakened.
- Product remains **Release Candidate / External Qualification Pending**.

## Track A2 Category 2 F02-012 live stream-role selection — 2026-10-01

- Starting main: `cc023da65de7d58d8e64b85811a45405adbf6011`.
- Issue #285 completed; PR #286 merged.
- Accepted/reviewed PR head: `6295249da4684b165f6665ee8e0f282ab1ef1fd2`.
- Merge commit: `3230666d9cad9ad6c4a0890665eb2df52ac26bd6`.
- Independent adversarial review/QA evidence: PR review `5377278820`; REVIEW PASS / QA PASS at the deterministic software boundary.
- F02-012 now provides explicit active-tile MAIN/SUB/THIRD selection. MAIN is always available; SUB is offered only when configured; THIRD only when its configured path/key exists. Default remains SUB-if-present, otherwise MAIN.
- The legacy default live path remains unchanged. When SUB exists, explicit MAIN uses a deterministic derived live path; all viewer paths retain #281 maxReaders and #278 exact-path grants. #282 signing/JWKS remains unchanged.
- Role switching reuses #284 per-tile generation fencing, break-before-make cleanup, same-origin WHEP Location checks, tile-local failure and manual retry. Duplicate-camera prevention remains camera-based across roles.
- Review blocker fixed before acceptance: distributed camera deletion initially omitted the new derived MAIN live path; final head removes it from current and stale cleanup nodes with regression coverage.
- Recording remains a separate MAIN-source continuous record path with no live reader ceiling. Live role selection does not mutate recording or AI policy/source configuration.
- Exact-head gates PASS: static/image build, Intelligent VMS compile/unit, full vms-test, dependency audit and container audit; publish image skipped as expected for the PR.
- Physical camera MAIN/SUB/THIRD support, codec/profile combinations, browser/GPU capacity, WAN/TURN/NAT, switch latency, failover visual continuity and soak remain NV / External Qualification Pending.
- Security residual #261 remains open; no security gate was weakened.
- Product remains **Release Candidate / External Qualification Pending**.

## Track A2 Category 2 secure live snapshot milestone — 2026-10-01

- Starting main: `4c858ae368f88dba92947aa9ea38ba98f0afdcd0`.
- Issue #288 completed; PR #289 merged.
- Accepted/reviewed PR head: `74a1e7ed61a97946e153997accf5dbd96e2b23ea`.
- Merge commit: `46f608437ef66727ac6ccfe44b419bb08978560d`.
- Independent adversarial review/QA evidence: PR review `5377466211`; REVIEW PASS / QA PASS at the deterministic software boundary.
- Four-row audit result: F02-005 was missing; F02-006 missing but has an authoritative recording/playback foundation; F02-020 missing; F02-021 partial from authorized MP4 playback.
- Dependency decision: snapshot and clip/export are separate architectures. This milestone implements only secure active-tile snapshot/local download. Manual clip/export remains the next focused recording-backed milestone.
- Snapshot source is the already-authorized decoded frame of the current active tile and exact selected MAIN/SUB/THIRD role. No hidden MAIN upgrade, camera-native/original-sensor claim or silent fallback exists.
- No new backend media endpoint, MediaMTX reader, camera/RTSP credential, FFmpeg/subprocess, server temp file, DB evidence row or persistent snapshot repository was introduced.
- Browser MediaRecorder is rejected for future clip architecture; existing authorized bounded recording playback on record_stream_key is the preferred foundation.
- Review blocker fixed before acceptance: stale snapshot completion originally rerendered the grid unconditionally; final head fences the finalizer on the original camera+role+generation with regression coverage.
- Recording and AI code/configuration are unchanged. Continuous MAIN/source-copy recording remains independent.
- Exact-head gates PASS: static/image build, Intelligent VMS compile/unit, full vms-test, dependency audit and container audit; publish image skipped as expected for the PR.
- F02-005: implemented + software-evidenced, final browser/device qualification NV. F02-020: partial (snapshot local download only). F02-021: partial (snapshot download + existing playback; clip export missing). F02-006: remains missing/target pending recording-backed manual clip/export.
- Product remains **Release Candidate / External Qualification Pending**. Security residual #261 remains separately open.

## Track A2 Category 2 bounded recording-range clip export — 2026-10-01

- Starting main: `81b51af3b1f63dc4c84fcc0b257ed1d6c998e7d4`.
- Issue #290 completed; PR #291 merged.
- Accepted/reviewed PR head: `eed94803a91811b0f24528bf128cc2471193636b`.
- Merge commit: `5c3a500fee327a53a8c282e88ee172142db96aea`.
- Independent review/QA: review `5377973113`; REVIEW PASS / QA PASS at deterministic software boundary.
- Audit decision: F02-006 "Manual recording" is not treated as synonymous with clip export. It remains TARGET/MISSING pending a separate durable Start/Stop manual-intent design. F02-021 clip download and F02-020 local recording-download semantics were dependency-ready and implemented here.
- Export reuses authoritative continuous MAIN/source-copy recording through server-resolved `record_stream_key` and MediaMTX playback. Browser MediaRecorder, second RTSP ingest/recorder, FFmpeg/shell, temp export files and public download URLs are absent.
- Export is admin/operator-only, camera-authorized on each request, timezone-aware, max-duration bounded, requires continuous coverage, rejects gaps, and in distributed mode rejects ranges crossing recording-node/failover boundaries.
- MP4 attachment filenames use sanitized stable camera ID plus UTC start/end. No client-controlled media URL/path/storage key/remux flag exists.
- Resource guardrails: default 900-second duration, 4 concurrent exports per control-api process, configurable 30-second export I/O timeout. These are safety bounds, not measured capacity; concurrency is explicitly not cluster-wide.
- Review fixes before acceptance: stalled export I/O now cannot hold a slot indefinitely; retained historical footage remains exportable after future recording is disabled; test-string defect corrected.
- Export failure does not mutate/stop continuous recording, live WHEP/roles/maxReaders, AI policy/source or #289 snapshot.
- Exact-head gates PASS: compile/unit, full vms-test, static/image build, dependency audit, container audit, 623-row/48-category catalog, Phase 7 chaos 14/14, Phase 8 smoke, Alembic through 0016, Compose and Helm. PR image publish skipped as expected.
- F02-020 and F02-021 are software-QA complete for their stated local snapshot/recording-download and snapshot/clip-download software contracts, while final verification remains NV. F02-006 remains TARGET/MISSING.
- Product remains **Release Candidate / External Qualification Pending**. Security residual #261 remains separately open.

## Track A2 Category 2 durable manual recording — 2026-10-01

- Starting main: `be99bc12dd55065355771b1275fee7d452a0b3f9`.
- Issue #292 completed; PR #293 merged.
- Accepted/reviewed PR head: `b4e24632260be80c3200196c82e24ceada2d8aa0`.
- Merge commit: `7853642d064622dd6e91ca1b8b6aec2796ac7a12`.
- Independent review/QA evidence: PR review `5378965251`; REVIEW PASS / QA PASS at deterministic software boundary.
- F02-006 Manual recording is now implemented as durable server-timed Start/Stop interval intent over authoritative continuous MAIN recording. No second recorder/ingest, browser MediaRecorder, FFmpeg recorder or temp media store was added.
- Alembic 0017 adds shared manual session state with tenant/site/camera/operator ownership, ACTIVE/STOPPED state, immutable max_stop_at and database-enforced one-active-session-per-operator/camera uniqueness. Different operators may independently mark the same camera.
- Start requires current admin/operator camera authorization and active continuous recording. Stop reauthorizes ownership, row-locks and finalizes once. Admin may manage authorized in-scope sessions; ordinary operators own their own sessions.
- Maximum interval uses the #291 export-duration configuration captured immutably at Start. Expired sessions are deterministically finalized on API observation; no browser timer or process-local state is authoritative.
- Refresh/reconnect recovers active sessions from shared DB. Bounded recent history supports retry of the same stopped interval. Manual-session metadata currently follows control-DB administrative retention; no separate automatic history purge is claimed.
- Manual export reuses #291 shared recording-backed export. Gaps and cross-recording-node/failover ranges fail explicitly. Export failure never rewrites Stop history and may be retried.
- Camera deletion finalizes ACTIVE sessions and SET NULL preserves historical metadata; deleted-camera export fails 410 because current camera authorization/source resolution is unavailable.
- Live MAIN/SUB/THIRD, WHEP/maxReaders, AI, snapshot, recording policy/source/retention/placement/fencing/hooks remain independent.
- Exact-head gates PASS: compile/unit/Ruff, full vms-test, static/image build, dependency audit, container audit, 623-row/48-category catalog, Phase 7 chaos 14/14, Phase 9 RC gate, Alembic 0001→0017, Compose and Helm. PR image publish skipped as expected.
- F02-006 engineering state is QA/software-evidenced; final verification remains NV / External Qualification Pending.
- F02-020 and F02-021 remain QA/software-evidenced; architecture is reused, not reopened.
- Product remains **Release Candidate / External Qualification Pending**. Security residual #261 remains separately open.

## First field-test dependency audit and Category 3 recording controls — 2026-10-02

- PR #294 established `docs/product/PRODUCT_REQUIREMENTS.md` as the permanent cross-cutting product/platform contract and updated START_HERE to read it immediately after the bootstrap file.
- During PR #294 validation, the required container gate found a fixable pcre2 HIGH in the web image. Issue #295 / PR #296 refreshed Alpine packages before the existing non-root boundary; dependency and container audits then passed. The gate was not suppressed.
- Field-test dependency audit found that the accepted Category-2 live/manual/snapshot/clip work remains intact, but the grid refactor left both the old continuous-recording helper and `openPlayback()` without UI call sites.
- Dependency order selects Category 3 first: issue #297 / branch `feat/f03-continuous-recording-retention`, Feature IDs F03-001 and F03-013.
- The milestone exposes active-camera continuous MAIN recording and bounded retention controls while preserving current recorder segmentation values instead of replaying legacy hard-coded defaults.
- Recording policy reads distinguish 404/unconfigured from real failures and are fenced to the current active-camera generation.
- Recording-policy mutation and manual Start serialize on the same policy-row lock. Disabling continuous recording is rejected while an unexpired manual interval is ACTIVE, preventing a Start/Disable race from cutting the authoritative media beneath F02-006.
- MAIN/source-copy recording authority, live MAIN/SUB/THIRD choice, AI, snapshot, bounded export, placement/fencing and completion hooks remain separate.
- No migration is required.
- Category 4 still blocks a complete field-test browser path because playback functions exist but are not reachable from the current grid. After #297, prefer a focused F04-001/F04-002/F04-003 single-camera date/time timeline-playback milestone before Windows packaging.
- Genuine Windows blockers remain POSIX-oriented storage defaults, Bash-oriented backup/restore, missing Windows service/installer lifecycle, and no real Windows desktop package. This milestone adds no new platform-specific dependency.
- Product status remains **Release Candidate / External Qualification Pending**; real camera/browser/codec/storage/Windows/capacity/failover evidence is still required.

## Track A4 reachable playback workflow — 2026-10-02

- Starting main: `f5995091ddf7957e881d1e44819c3a3117f13c72`.
- Issue #299 selects F04-002 Playback by date/time, F04-003 Timeline, F04-004 Play/pause, F04-011 Clip selection and F04-014 Start/end-time selection.
- F04-001 is not promoted because its exact wording includes multi-camera playback; this milestone is intentionally single-camera. F04-012 remains unclaimed because existing standard export is MP4-only.
- Existing authorized timeline, recording index, historical/current recording-node resolution, MediaMTX playback/remux and #291 MP4 export are reused.
- The active live tile now provides a normal Playback entrypoint. The playback workspace exposes local date/time, real recording spans, selected recorded time, play/pause, native seek, clip start/end selection, authorized download and Return to live.
- Timeline/date responses are generation-fenced; active-camera change or authorization loss closes stale playback state. Previous playback media is detached before a new selection and on close/page exit.
- The play route now proves actual recording coverage before streaming in local/current-node cases and bounds playback at the first gap. Distributed historical playback remains bounded to the indexed segment/node containing the selected start.
- No recording policy, retention, live role, AI, snapshot or manual-recording state is mutated. No second recorder/playback database, MediaRecorder, FFmpeg path, public recording URL, client-selected node/path or filesystem path is introduced.
- No database migration or new dependency is required.
- Windows portability: no new POSIX-only filesystem/shell/signal/temp-file assumption is added; Windows packaging remains a later milestone.
- Product remains **Release Candidate / External Qualification Pending**; real camera/browser/codec/storage/failover/capacity/Windows qualification remains external.

## Field-Test Release Track — Ubuntu deployment baseline — 2026-10-02

- Starting main: `e2a964ea427ad3545312f0ce8b86d1d925cc357a`.
- Issue #301 / branch `release/ubuntu-field-test-baseline`.
- The deployment audit reuses Compose, PostgreSQL, Redpanda, ClickHouse, MediaMTX, control-api, workers and web plus existing Phase-9 backup/restore/upgrade primitives.
- Gaps selected for this release milestone: fresh-Ubuntu preflight/bootstrap, generated private runtime config, explicit deployed Alembic migration, selected host recording persistence, restart/reboot contract, browser login bridge, minimal existing-API IP camera setup, bounded diagnostics, field backup/restore wrappers, smoke checklist and Windows blocker inventory.
- Browser auth does not create an identity database. An existing JWT is exchanged for a same-origin HttpOnly cookie; cookie-auth mutations require a matching CSRF cookie/header. Token URL/localStorage persistence is prohibited. Production OIDC behavior is not weakened.
- Field-test configuration uses `AUTO_CREATE_SCHEMA=false`, explicit Alembic migrations, random runtime secrets, loopback web/API signaling/admin binds by default and `restart: unless-stopped`.
- Ordinary stop/restart/uninstall/upgrade preserve database volumes, recording media, configuration and secrets. Named-volume purge is separately confirmed and still does not delete the host recording directory or .env.
- Field-test backup covers PostgreSQL plus safe configuration metadata; ClickHouse/media/secrets are explicitly excluded unless their dedicated protected processes are used. Database backup is not video backup.
- Automated validation is designed for Ubuntu 22.04 and 24.04 x86_64 runners. Physical-machine reboot, real camera/codecs, recording continuity, storage durability and capacity remain External Qualification Pending.
- Windows work is deliberately deferred; blockers are classified for the next Windows field-test server baseline.
- No feature-catalog row is promoted solely because of this deployment milestone. Product remains **Release Candidate / External Qualification Pending**.

## Standalone repository migration Phase A — 2026-10-03

- Destination: `Sunny-Gumber/Intelligent-vms-solution`; private staging only.
- Migration issue: destination #1.
- Accepted recovery source remains `Sunny-Gumber/camvault@6a97653e596c9dab3ab212c7b045fea2cd3fbb15`.
- Legacy accepted PR #302 head: `859b213cacd03a99f03e1cc7aa41b93ac55ef915`; merge SHA: `6a97653e596c9dab3ab212c7b045fea2cd3fbb15`.
- Exact destination import checkpoint: `fa4bdc13e89e41bd925aa448c628d999a1201da3` on `migration-public-release-baseline`.
- Exact import proof: 323 accepted VMS source files, zero missing, zero blob mismatches, zero active destination workflows.
- The pre-existing destination `migration` branch was audited and rejected as release authority because it was missing 73 accepted VMS files.
- Layout remains `INTELLIGENT_VMS_START_HERE.md` + `intelligent-vms-v1/`; no flattening.
- Unrelated CamVault backend/application/tests/deployment/history are not migrated.
- The mixed legacy `.github/workflows/ci.yml` is excluded because it contains CamVault backend/image jobs. Dedicated VMS CI/security/Ubuntu workflows are staged non-executably under `migration-staging/workflows/`.
- Minimum VMS-only root helpers required by the dedicated VMS quality gate are migrated: Copilot instructions plus VMS Boss/Developer/Reviewer agent files.
- Public-release security audit is `docs/reviews/PUBLIC_REPOSITORY_SECURITY_AUDIT.md`. No publication blocker remains after genericizing real-place sample labels; synthetic test/dev credentials remain explicitly synthetic.
- Phase-A structural validation: feature catalog 623 rows / 48 categories; Alembic chain 0001 through 0017; sensitive artifact/filename scan PASS.
- Full compile/Ruff/pytest/Compose/Helm is not falsely claimed as destination validation while private; it is a required Phase-B public Actions gate. Legacy source PR #302 had VMS CI, security, generic VMS job and Ubuntu field-test workflows green.
- License decision is explicitly pending owner action; repository visibility does not imply an open-source license.
- Unfinished Windows source remains separate: legacy camvault #303/#304 at `7fc9065a43977329c91988f4dad5e123826f60cc`; do not merge old PR #304.
- Known first Windows continuation defect is test-environment only: Windows pre-install recording regression tests need a deterministic TEST-ONLY `VMS_SECRET_KEY`. Native Windows installer/service execution is still unproven.
- Hard publication gate: while repository is private, do not activate `.github/workflows/`. Stop after Phase A and wait for owner to switch visibility to PUBLIC.
- Product remains **Release Candidate / External Qualification Pending**.

## Native Windows Desktop Client Foundation — issue #6 / PR #7 — 2026-10-04

- Verified starting main: `e49962afaa8b176ad8f7b67a0293642af51b02d3`, the accepted Windows Server baseline.
- Dedicated branch: `feature/windows-desktop-client-foundation`; no server-baseline PR was reused.
- Existing catalog rows selected: F08-001 Windows desktop client, F08-007 Secure login, F08-008 Remember-login. No duplicate feature IDs were created.
- Technology: .NET 10 LTS + WPF x64. WebView2 is restricted to the replaceable secured WHEP/WebRTC renderer, not the application shell.
- The client reuses existing FastAPI authentication/capability/camera/live-grant APIs and MediaMTX/WHEP. It does not add a database, recorder, camera authority or authentication bypass.
- Remote profiles require HTTPS; HTTP is limited to localhost/loopback field testing. Certificate validation remains platform-default with revocation checking.
- Remember-login uses Windows Credential Manager. Profiles JSON, logs and diagnostics do not intentionally contain bearer tokens/passwords/camera credentials.
- Camera navigation consumes server-authorized camera scope and live-role availability.
- Live-session lifecycle is generation-fenced and performs renderer cleanup on stop/switch/logout/unload/application close.
- Client state uses per-user LocalAppData and is separate from server ProgramData.
- Packaging is a deterministic self-contained win-x64 field-test ZIP plus SHA-256 and isolated per-user install/uninstall scripts; it is not claimed as the final commercial installer.
- Automated evidence includes native compile, deterministic positive/edge/negative tests, production-source security scan, package generation and executable smoke launch. Existing CI, Security, Ubuntu and Windows Server field-test workflows remain regression gates.
- F08-001/F08-007/F08-008 are promoted only to QA/software-evidenced; verification remains NV.
- Windows 10/11 hardware qualification, real WHEP/camera/codec validation, DPI/multi-monitor, GPU/native decoding, high-density grids, OIDC/PKCE interactive UX and final signed installer/update channel remain External Qualification Pending.
- Product status remains **Release Candidate / External Qualification Pending**.

## Windows Desktop OIDC/PKCE Authentication UX — issue #8 / PR #9 — 2026-10-04

- Verified starting main: `1a6a0b0260e8fd3d2ae32ffe031f8f4acd163384`.
- Dedicated branch: `feature/windows-desktop-oidc-pkce`.
- Existing VMS server architecture remains the identity authorization boundary: OIDC/JWKS JWT validation plus tenant/site/RBAC, not a new OAuth authorization server.
- Added safe unauthenticated `/api/v1/auth/capabilities` metadata so the desktop does not guess server auth policy.
- Native client uses Duende.IdentityModel.OidcClient 7.1.0, system browser, Authorization Code, PKCE S256, fresh state/verifier/application nonce and a bounded 127.0.0.1 ephemeral callback.
- No client secret, second identity database/JWT issuer, embedded IdP WebView or desktop authorization bypass is introduced.
- Access token remains memory-resident. Optional refresh material is stored separately per server profile in Windows Credential Manager only when Remember Me is selected and a refresh token is issued.
- Refresh is serialized; protected API 401 handling is bounded to one renewal and one retry. Permanent refresh rejection deletes remembered material; transient identity/network failure preserves it without treating the user as authenticated.
- Profile switch/logout/expiry/app shutdown cancel active auth and preserve live/camera/capability cleanup.
- Logout is local desktop logout; global IdP/browser logout is not claimed.
- F08-007/F08-008 receive updated software evidence. F25-003/F25-004 remain TARGET/NV because their broader AD/LDAP/SAML/MFA/certificate/Windows/local-auth wording is not fully implemented.
- Threat review: `docs/security/WINDOWS_DESKTOP_OIDC_THREAT_REVIEW.md`.
- Product remains **Release Candidate / External Qualification Pending**. Named IdP, MFA, proxy/private-CA and Windows 10/11 qualification remain external.
## Native Windows multi-camera live-grid continuation

The desktop live-grid milestone is tracked in destination issue #10 / PR #11.

It builds on the accepted Windows desktop foundation and OIDC/PKCE authentication. The desktop remains a client of the existing control API and MediaMTX/WHEP media plane. Native grid software supports deterministic 1/4/9/16 layout architecture, independent tile lifecycles, server-advertised MAIN/SUB/THIRD role policy, duplicate prevention, focus mode, safe cleanup, and assignment persistence.

The current renderer remains WebView2/WHEP behind ILiveMediaRenderer and is explicitly replaceable by a later native/hardware-decoded implementation. CI evidence for 16 logical tiles is not a real 16-stream workstation performance qualification.

After PR #11 acceptance, future work must still preserve: no direct camera RTSP/credentials, no second recorder/media backend, no hidden live-session leaks, and no capacity claims without real external measurement.
