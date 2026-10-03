# Intelligent VMS — Product Requirements and Target Platform Contract

**Status:** Product intent / roadmap source of truth.  
**Applies to:** `intelligent-vms-v1/` only.  
**Qualification boundary:** A requirement in this document is not a claim that the capability is already implemented, tested, certified, or Production Qualified.

## 1. Product vision

Build a commercial, enterprise-capable, vendor-neutral CCTV Video Management System (VMS) for small sites through distributed multi-site deployments.

The product must support the complete market-feature program in `VMS_COMPLETE_FEATURE_MASTER_CHECKLIST.md` / `FEATURE_CATALOG.csv`. The catalog remains the atomic feature checklist; this document records cross-cutting product requirements that must not be lost between implementation chats.

Core product principles:
- ONVIF-first, standards-based camera interoperability with manufacturer adapters where standards are insufficient.
- One VMS backend/API/security/media architecture serving browser and native clients.
- Continuous source-copy recording using MAIN stream by default.
- Browser/live/AI choices must not silently alter authoritative recording.
- Distributed architecture for large deployments; central control must not become the media bottleneck.
- Security, tenant/site isolation, recording continuity, deterministic failure behavior and auditability are product requirements.
- Hardware/device/capacity support is claimed only after measured or named-device qualification.

## 2. Required deployment platforms

### 2.1 VMS Server

The product target includes all of the following server installation classes:

| Platform | Product requirement |
|---|---|
| Windows 11 64-bit | Required server installation target |
| Windows 10 64-bit | Compatibility target for existing installations; qualification/support policy must account for Microsoft lifecycle |
| Windows Server 2022 | Required professional server target |
| Windows Server 2025 | Required professional server target |
| Ubuntu Linux 22.04 LTS | Required Linux server target |
| Ubuntu Linux 24.04 LTS | Required Linux server target |

Windows workstation/server support must be a genuine product deployment path, not merely an assumption that Docker Desktop will work.

A commercial Windows deployment should progress toward:
- installer-driven setup;
- Server / Client / Both installation choices where appropriate;
- services automatically starting after reboot without an interactive user login;
- configurable recording/storage locations;
- firewall/network configuration guidance or installer integration;
- safe upgrade/rollback;
- configuration and recordings preserved according to explicit uninstall/upgrade policy;
- service health, logs and diagnostics;
- clean recovery after host restart;
- documented database/media-service dependencies.

Linux may remain the primary production/container orchestration platform for large distributed deployments, but Windows support is a first-class product requirement.

## 3. Client platforms

### 3.1 Browser client

The VMS must provide a full browser-based client as a primary client experience.

Desktop browser qualification targets:
- Microsoft Edge;
- Google Chrome;
- Firefox where media/protocol compatibility permits;
- supported browsers on Windows, macOS and Linux.

Mobile browser access on Android/iOS is a target where practical, with limitations explicitly documented.

The browser client should cover normal operator workflows including camera management (subject to RBAC), live monitoring, layouts, recording/playback, search/events, snapshots, clip/manual recording workflows, PTZ and other catalog capabilities as they are implemented.

### 3.2 Native Windows desktop client

A native/packageable Windows desktop application is **required in addition to the browser client**.

Initial OS targets:
- Windows 11 64-bit;
- Windows 10 64-bit compatibility where supportable.

The Windows application must use the same authoritative backend APIs, authorization, media and audit contracts rather than creating a separate VMS backend.

The desktop client roadmap must support VMS/workstation capabilities where native software adds value, including:
- persistent operator workspace/layouts;
- multi-monitor operation;
- full-screen monitoring;
- configurable local snapshot/clip download location;
- keyboard/operator shortcuts;
- native notifications where applicable;
- startup/login options;
- robust long-running multi-camera operation;
- future hardware-decoding/GPU optimization subject to measured qualification;
- safe application update mechanism.

A simple browser shortcut is not sufficient to satisfy the final Windows desktop-client requirement.

### 3.3 Mobile applications

Android and iOS native applications are roadmap requirements after the browser/Windows desktop baseline. They should reuse the same API/security architecture. Until implemented and qualified, browser access must not be described as native mobile-app support.

## 4. Installation profiles

The product should support these deployment patterns:

**Small site / workstation**
- Windows 11 PC;
- VMS Server + Windows Client on the same host where sizing permits;
- browser access also available.

**Professional on-premises**
- Windows Server 2022/2025 or supported Ubuntu LTS VMS server;
- Windows 10/11 operator workstations using native client and/or browser.

**Distributed / enterprise**
- Linux/container/Kubernetes-oriented regional and central services;
- multiple media/recording/AI nodes;
- browser and Windows operator clients;
- bounded regional autonomy and generation/lease/fencing.

**Remote/browser**
- authorized browser client from supported desktop/mobile platforms through a secure deployment topology.

No profile implies a camera-count capacity until Phase 8 measured evidence qualifies it.

## 5. First complete field-test software baseline

Before describing a build as the first **complete field-test VMS**, it should provide an installable and usable end-to-end path, not merely backend APIs.

Target baseline:
- supported server installation on at least the selected Windows test platform and primary Linux platform;
- browser client;
- testable Windows desktop client/package;
- login/RBAC and tenant/site isolation;
- ONVIF/manual camera onboarding;
- camera online/offline status;
- multi-camera live view;
- MAIN/SUB/THIRD selection where camera capability permits;
- bounded live-session behavior;
- continuous MAIN recording;
- recording timeline/playback;
- live snapshot/download;
- durable Start/Stop manual recording intent and recording-backed MP4 clip download;
- basic camera reconnect/recovery behavior;
- storage configuration/retention basics;
- logs/diagnostics sufficient for field testing;
- documented install, upgrade, backup/recovery and known limitations.

PTZ, events/alarms and other features should be brought into the field-test baseline when their dependency order and exact catalog requirements make them necessary. The 623-feature program continues beyond the first field-test build.

A field-test build is **not** Production Qualified.

## 6. Camera and manufacturer compatibility

Target:
- ONVIF-first generic support;
- RTSP where appropriate;
- capability discovery rather than manufacturer assumptions;
- manufacturer-specific driver/plugin framework for non-standard or enhanced features.

Compatibility evidence should eventually be recorded as:
`Manufacturer -> Model -> Firmware -> ONVIF profile(s) -> Streams -> Events -> PTZ -> Analytics -> Tested status`.

Do not claim official manufacturer/model compatibility without named model/firmware evidence.

## 7. Media and recording invariants

- MAIN/source-copy is the authoritative continuous recording source by default.
- SUB/THIRD can be used for live/AI according to explicit policy without changing recording silently.
- Manual recording is durable operator interval intent over authoritative recording unless a future approved architecture explicitly changes this.
- Snapshot semantics must state the actual captured stream/source.
- Export/download must remain authorized and bounded.
- AI failure must not stop recording.
- No duplicate recorder/ingest is introduced merely to implement a UI feature.
- Recording gaps, failover boundaries and unsupported stitching must fail explicitly rather than fabricate continuity.

## 8. Security requirements

The product requires:
- RBAC;
- tenant/site isolation;
- safe secret storage;
- no camera credentials in public URLs/logs;
- TLS-secure deployment paths;
- auditable operator/security actions;
- rate/resource limits;
- secure session/token lifecycle;
- target validation/SSRF protection;
- database migrations with safe upgrade/rollback;
- non-root/least-privilege deployment where applicable;
- vulnerability/dependency/container gates;
- backup/restore and disaster-recovery procedures.

Future secure-transport work should include HTTPS/TLS, secure WebRTC/WSS where applicable, internal mTLS/PKI, certificate lifecycle/trust stores/rotation and explicit plaintext-fallback policy. TLS support must never be implemented by disabling certificate verification.

## 9. Product qualification states

Keep the existing meanings:
- **Development Accepted** — engineering milestone passed.
- **Release Candidate** — applicable software/SRE/security gates passed.
- **Production Qualified** — required real external production evidence passed.
- **Full Market Feature Complete** — every feature-catalog row satisfies completion rules.

Current repository/product claims must continue to use evidence, not aspiration.

## 10. Relationship to feature catalog

This document does not replace:
- `VMS_COMPLETE_FEATURE_MASTER_CHECKLIST.md`;
- `FEATURE_CATALOG.csv`;
- `MARKET_FEATURE_MASTER_PROGRAM.md`;
- architecture/security/release qualification documents.

Instead:
- feature catalog = **what capabilities exist**;
- this document = **what product/platform experience we intend to ship**;
- evidence/qualification documents = **what has actually been proven**.

When a future product decision is made (supported OS, client type, installation model, compatibility requirement, deployment model, commercial UX requirement), update this document so it survives chat/session changes.
