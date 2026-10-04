# Intelligent VMS — Implementation History

This is the chronological build history intended for future chats and maintainers.

Always verify current GitHub state before using this as a live status source.

---

## 2026-09-23 — Phase 1: architecture and core

Commit:
`623abaa57c735b60d5fbbf1b2685cd526460f2ff`

Commit title:
**Add Intelligent VMS V1 architecture and Phase-1 core**

Established:
- FastAPI control plane;
- PostgreSQL control state;
- MediaMTX media layer;
- Kafka-compatible event backbone;
- ClickHouse event store;
- camera registry;
- basic web wall;
- distributed-first architecture direction.

---

## 2026-09-23 — Phase 2A: ONVIF onboarding

Commit:
`a779d23d29ac25059d953658712e20ba9d6c1da5`

Commit title:
**Intelligent VMS Phase 2A: ONVIF onboarding + capacity model**

Added:
- ONVIF discovery/probe;
- capability/media profile handling;
- stream URI selection;
- onboarding;
- early capacity model;
- QA/compile gates.

---

## 2026-09-23 — Phase 2B: secure control plane

Commit:
`42837ea972898ba888c341a970ff236cabfa522f`

Commit title:
**Intelligent VMS Phase 2B: secure control plane and durable reconciliation**

Added:
- JWT/RBAC;
- tenant/site authorization;
- Alembic migration gates;
- durable MediaMTX reconciliation;
- persistent reconciler cursor/service state;
- JWT algorithm separation;
- stronger CI schema checks.

---

## 2026-09-23 — Phase 3: recording/playback

Commit:
`2cc192b997af0536aded314db7fa0e38a53595d4`

Commit title:
**Intelligent VMS Phase 3: recording, retention, timeline and playback**

Added:
- source-copy recording;
- retention;
- timeline;
- playback;
- recording throughput/segment sizing tool;
- timezone/bounded-window review fixes;
- recording migration/image gates.

---

## 2026-09-24 — Phase 4: event intelligence and camera health

Commit:
`8bb10dc2c6a814703bc0fa806ff8f993180b89fe`

Commit title:
**Intelligent VMS Phase 4: event intelligence and camera health**

Added:
- event search;
- camera-health engine;
- resource model/runbooks;
- health UI/event panel;
- bounded transport probes;
- durable hysteresis;
- regional/batched health behavior;
- ClickHouse timestamp hardening.

---

## 2026-09-25 — Phase 5: ONVIF events, alarms and diagnostics

Commit:
`0cfaedb6d4f2d8c29b13099d53f984cb8ad86ee8`

Commit title:
**Intelligent VMS Phase 5: ONVIF events, alarms and diagnostics**

Added:
- alarm rule/instance models and APIs;
- ONVIF PullPoint/event worker;
- normalized ONVIF events;
- alarm matching/deduplication;
- Kafka alarm worker;
- MediaMTX diagnostics;
- sharding/performance controls;
- alarm UI and acknowledgement;
- event-worker/runbook/resource documentation;
- deterministic event IDs and SOAP namespace fixes.

---

## 2026-09-25 — Phase 6: AI orchestration

Commit:
`95e58a4a97c14562d155ca2e39f3c3e5c09b2ce3`

Commit title:
**Intelligent VMS Phase 6: AI orchestration and provider contract**

Added:
- AI model/policy entities;
- AI control API;
- provider/artifact contract;
- normalized AI result pipeline;
- bounded AI policies/results;
- UI controls;
- inference-only policy semantics;
- separation between camera metadata and AI inference policy.

---

# 2026-09-26 — Phase 7: Distributed Regional VMS

## Step 1A — regional registry/placement

PR #194  
Merge:
`197be3033ab2a3a9a374455f14995864aa58abc9`

Added:
- node registry;
- regions/site mapping;
- capacity/load-aware placement;
- staleness/headroom;
- bounded movement;
- controller cursor;
- advisory lock.

## Step 1B — assignment-driven media execution

PR #195  
Merge:
`91923888d757ae89a5d8c767046beb408fffda57`

Added:
- assigned-node media/recording execution;
- node-aware live/playback URLs;
- distributed health;
- recording-node callbacks;
- durable stale cleanup/failback handling;
- single-node compatibility.

## Step 1C-A — regional node agent / trustworthy heartbeat

PR #199  
Merge:
`2d952bca9a8b5ae5d4c99971ff13da4fa549483e`

Added:
- host/media telemetry;
- node-scoped service identity;
- admin-managed node endpoints/capacity;
- measured-only heartbeat;
- no unauthorized node self-registration;
- safe handling for missing role metrics.

## Step 1C-B — generation/lease fencing

PR #204  
Merge:
`14c81535d105e7740cb931ee182ce4063910e3f0`

Added:
- assignment generations;
- revocations;
- local persisted fencing state;
- stale generation rejection;
- lease expiry;
- recording evidence fencing;
- placement/execution serialization;
- failover/failback race regressions.

## Step 1C-C — bounded regional autonomy

PR #206  
Merge:
`b9bd3982811c126d9b2f60487edae8acf737fb35`

Added:
- bounded offline authority;
- regional autonomous/degraded states;
- central failover deferment;
- durable regional spool;
- reconnect/backfill;
- observed heartbeat timestamps;
- stale telemetry protection.

## Reliable event delivery

PR #208  
Merge:
`0891f391063fddbccb92352292164279de462795`

Added:
- transactional outbox;
- idempotent event/segment identity;
- multi-replica claiming;
- retry/DLQ;
- lazy Kafka reconnection;
- replay-tolerant consumers;
- operator repair path.

## Final regional chaos qualification

PR #210  
Merge:
`735979f034041d1712747f6da3f6df3900afd792`

Result:
- deterministic 14/14 software chaos gate;
- Phase 7 parent #179 closed;
- Phase 7 status: **Development Accepted**.

---

# 2026-09-26 to 2026-09-27 — Phase 8 benchmark/evidence framework

## Benchmark harness

PR #211  
Merge:
`3140c7a60422eec3bdce5aa8c3e7df106ea7d700`

Added:
- versioned evidence schema;
- workload drivers;
- environment/hardware/resource capture;
- percentile/failure metrics;
- RTSP/source/viewer/storage/event/reconnect tooling.

## Measured-only hardware matrix

PR #212  
Merge:
`03edf3ae5a86cb591bd3bf257296bcbd0b84d574`

Added:
- measured-evidence-only capacity qualification;
- repeat/duration/warmup/resource thresholds;
- safe headroom;
- N+1;
- explicit UNQUALIFIED output.

## Reproducibility QA

PR #213  
Merge:
`3e410551c15587d0410c6cd1c5fe5bc56a4349e0`

Added:
- reproducibility validator;
- workload-specific repeatability;
- thermal/frequency evidence;
- hardware/software fingerprint binding;
- reproducibility PASS required for qualification.

Phase 8 software framework is complete.

Parent #185 stays open because real target hardware/environment evidence is pending.

---

# 2026-09-27 to 2026-09-28 — Production/SRE/security

## Phase 9.1 — Kubernetes/Helm

PR #217  
Merge:
`83e8a42e95cdf8431fd5459c96c58a9e462b1df0`

Added:
- Kubernetes/Helm foundation;
- production topology;
- probes;
- Prometheus foundation;
- web routing;
- deployment gates.

## Code-quality/documentation program

Subsequent merged PRs documented public interfaces and enforced coding standards across:
- routers;
- services;
- node agent;
- regional spool;
- tooling;
- benchmark code.

Final quality gate added:
- pinned Ruff;
- syntax/undefined-name/control-flow/whitespace checks;
- AST public-interface docstring enforcement;
- no blanket suppression strategy.

## Phase 9.2 — observability/SLO

PR #258  
Merge:
`fa5147e461007dca004c04f89341eaf0b412fe80`

Added:
- recording-gap health;
- Prometheus metrics;
- node saturation/unmeasured signals;
- outbox pressure metrics;
- alert rules;
- Grafana dashboard;
- SLO/triage contracts.

## Phase 9.3 — backup/restore/DR/upgrade

PR #259  
Merge:
`b16eca8a789b9cf3f5209943ed86bb476d4e9b3f`

Added:
- Postgres backup/restore;
- ClickHouse backup/restore;
- regional state/spool backup;
- Helm config/history backup;
- upgrade preflight;
- evidence hash manifest;
- exercise RPO/RTO calculation;
- rollback/recovery runbook.

## Production security baseline

PR #260  
Merge:
`1e94fdc4db743ec1912eab5bc7d9afbb6542258d`

Added:
- OIDC/JWKS production requirements;
- fail-closed auth;
- structured audit;
- network policies;
- non-root/seccomp/capability drop;
- optional External Secrets;
- TLS ingress requirements;
- pip-audit;
- Trivy image scanning;
- fixable HIGH/CRITICAL block.

Issue #261 created for inherited unfixed Python-base HIGH CVEs.

## Release Candidate gate

PR #262  
Merge:
`c75828efc835f05dabf2d2b7ffdd73e37c0258d6`

Added:
- executable release gate;
- versioned release policy;
- evidence hashing;
- catalog-integrity check;
- external-evidence policy;
- release-state regression tests.

Accepted status:

**RELEASE_CANDIDATE_EXTERNAL_QUALIFICATION_PENDING**

Phase 9 parent #189 closed.

---

# Market Feature Master Program

The user-provided market checklist is stored as:

`intelligent-vms-v1/docs/product/VMS_COMPLETE_FEATURE_MASTER_CHECKLIST.md`

The deterministic generated catalog contains:
- **48 categories**
- **623 feature rows**

The implementation program is:

`intelligent-vms-v1/docs/program/MARKET_FEATURE_MASTER_PROGRAM.md`

Important:
- all rows are accepted as product scope;
- not all rows are implemented;
- support can be native/camera/integration/analytics/licence/cloud/on-prem;
- no row may be claimed supported without the appropriate evidence.

---

# Current status after this history

Software status:
**Release Candidate / External Qualification Pending**

Still open:
- #185 — real target-hardware/environment evidence;
- #261 — inherited unfixed Python-base HIGH CVE tracking;
- #201 — master program remains open until external qualification and remaining product program work complete.

Still required for Production Qualified:
- real camera matrix;
- real target hardware;
- storage/network/WAN;
- real HA drills;
- real RPO/RTO;
- production security deployment evidence;
- pilot/soak/operator validation.

Still required for Full Market Feature Complete:
- audit/implement/verify every one of the 623 feature rows.

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
## 2026-10-04 — Native Windows Multi-Camera Live Grid Foundation

Issue #10 / PR #11 extends the accepted .NET 10 WPF desktop client from one active tile to a centralized multi-camera live-grid foundation.

The implementation adds deterministic 1/4/9/16 layouts, independent tile state/renderers, a central LiveGridCoordinator, duplicate-camera prevention, bounded connection establishment, per-tile generation/cancellation fencing, SUB-preferred grid role selection, MAIN-preferred 1-view/focus selection, layout-shrink cleanup, profile/auth/app-close cleanup, safe client-local assignment persistence, and grid diagnostics.

The media path is unchanged: authorized camera -> existing VMS live grant -> existing MediaMTX/WHEP -> renderer. No direct RTSP, camera credentials, second recorder, second media backend, token-in-URL path, or recording-policy mutation is introduced.

The current WebView2-per-tile renderer is intentionally replaceable. This milestone provides software architecture/lifecycle evidence only and does not qualify real 16-stream smoothness, GPU decoding, Windows 10/11, camera/codec capacity, CPU/RAM, WAN, DPI/multi-monitor, or long-duration soak.
