# Intelligent VMS — Market Feature Master Program

Source of truth:
- `docs/product/VMS_COMPLETE_FEATURE_MASTER_CHECKLIST.md`

## Product objective

The Intelligent VMS targets coverage of **every capability written in the master
checklist**, across all 48 numbered categories from Level 1 through Level 5 and
the extended categories.

"Coverage" does not mean every feature must be implemented natively. The master
checklist itself allows camera-dependent, integration/plugin, additional
analytics-server, licence-gated, cloud-only and on-prem-only delivery modes.

The goal is therefore:

> Every feature has an intentional supported delivery path, implementation state,
> verification evidence and documented limitations. No feature is silently omitted.

## Two-axis status model

### Support mode

Use the checklist's vendor-neutral codes:

| Code | Meaning |
|---|---|
| N | Natively supported |
| C | Camera-dependent |
| I | Integration/plugin |
| A | Additional analytics server |
| L | Additional licence/module |
| CL | Cloud-only |
| OP | On-premises only |
| P | Partially supported |
| R | Roadmap/unreleased |
| NS | Not supported |
| NV | Not verified |

### Engineering state

Every feature also has one engineering state:

| State | Meaning |
|---|---|
| TARGET | accepted into product scope |
| DESIGNED | architecture/API/UX contract approved |
| IMPLEMENTING | active code/integration work |
| QA | implementation complete, verification running |
| VERIFIED | evidence-backed support on named version/profile |
| EXTERNAL_EVIDENCE | software is ready but certification/device/site evidence is still required |

A support-mode code is not proof by itself. A feature may be labelled supported
only when engineering state is VERIFIED or the explicitly required external
evidence is attached.

## Definition of "all market features complete"

The product may use the status **Full Market Feature Complete** only when:

1. every written capability in the master checklist has a unique Feature ID;
2. every Feature ID has a support mode and engineering state;
3. no Feature ID remains `NV`, `R`, `NS`, `P`, TARGET, DESIGNED or IMPLEMENTING;
4. native capabilities have automated tests and documentation;
5. camera-dependent capabilities list tested vendor/model/firmware evidence;
6. integrations list tested integration/version/protocol evidence;
7. analytics-server features list required compute/model/provider and benchmark evidence;
8. cloud/on-prem-only features state their deployment constraint;
9. licence/module features are enforced technically and documented commercially;
10. certification/attestation items have real third-party evidence where the
    checklist calls for certification or authorization;
11. biometric features include privacy/consent/retention/human-review controls;
12. maximum scale claims reference measured Phase 8 evidence;
13. tender/RFI claims link to evidence and a last-verification date.

Until then, we report exact coverage status, never "supports everything."

## Feature-record schema

Each capability row must track at least:

- Feature ID
- category
- maturity level
- exact feature name
- description / acceptance criteria
- support mode
- engineering state
- camera/device dependency
- server/AI dependency
- licence/module dependency
- on-prem availability
- cloud availability
- desktop/mobile/web availability
- API availability
- maximum measured scale
- minimum supported version
- tested version
- automated-test reference
- supporting document
- evidence reference
- verification status
- known limitations
- owner/workstream
- last verification date

This extends the 25 tender/RFI comparison fields from the master checklist with
engineering ownership and test evidence.

## Implementation workstreams

### Track A — Core VMS
Categories 1–8:
device/camera support, live monitoring, recording, playback/export, PTZ, users,
basic events and clients.

### Track B — Professional VMS
Categories 9–14:
advanced device management, storage, investigation, alarms, electronic maps and
reporting.

### Track C — Analytics, metadata and identity
Categories 15–19:
video analytics, metadata, forensic search, ANPR/LPR and face management.

### Track D — Physical-security integrations and control room
Categories 20–22:
access control, intercom/audio and video wall/control-room functions.

### Track E — Enterprise platform
Categories 23–28:
enterprise architecture, HA, identity, cybersecurity, privacy/compliance and
health/maintenance.

### Track F — Integration, GIS and workflow
Categories 29–31:
enterprise APIs/integrations, GIS/smart-city and rules/workflow automation.

### Track G — AI and command centre
Categories 32–35:
advanced AI, natural-language/generative AI, intelligent investigation and
command-and-control.

### Track H — Cloud, optimization, BI, evidence and commercial platform
Categories 36–41:
cloud/hybrid, bandwidth optimization, BI, legal-grade evidence, licensing and
deployment/migration/support.

### Track I — Extended market requirements
Categories 42–48:
standards/certifications, hardware acceleration, extended protocols, field
video sources, AI governance, localization/accessibility and emerging
investigation/automation.

## Execution priority

The market-feature program does **not** invalidate the existing production
engineering sequence.

1. Finish the currently active Phase 8 evidence/reproducibility work.
2. Complete Phase 9 production/SRE/security foundations.
3. Maintain the feature catalog continuously while those foundations land.
4. Implement feature tracks in dependency order, starting with missing Core and
   Professional VMS capabilities before optional Level-5 UX.
5. Do not postpone architectural prerequisites (identity, evidence, plugin SDK,
   metadata schema, workflow engine, licensing framework) until the end merely
   because their visible UI features appear later.

## Architecture principles for broad feature coverage

- Prefer capability/plugin contracts over vendor-specific hard-coding.
- Keep camera features capability-discovered and device-profile driven.
- Keep AI providers/models replaceable.
- Normalize events/metadata so camera, AI and third-party sources share search,
  alarms, workflow and investigation.
- Keep evidence/chain-of-custody immutable and independent of UI.
- Treat maps/GIS, access, intercom, BMS, IoT, CAD, PSIM and ITSM as adapters to
  stable internal contracts.
- Keep commercial licensing separate from safety-critical recording ownership.
- Never make recording dependent on AI, cloud, GIS or business intelligence.
- Preserve regional autonomy and distributed ownership/fencing from Phase 7.
- Scale/throughput claims remain measured-only under Phase 8 rules.

## Verification classes

### Software-verifiable
Can be proven in CI/integration environments: APIs, RBAC, workflows, exports,
search, metadata, alarms, maps data model, licensing logic, audit/evidence
logic, client behavior and many integrations through emulators.

### Device-verifiable
Requires named cameras/encoders/decoders/intercom/PTZ/ANPR/thermal/radar/etc.
Record vendor, model and firmware.

### Infrastructure-verifiable
Requires real server/NIC/storage/GPU/cloud/network evidence.

### Certification-verifiable
Requires external audit/certification/authorization. Code completion never
equals certification.

### Legal/policy-verifiable
Requires jurisdiction/customer policy decisions in addition to software
controls, especially biometrics, evidence retention and privacy.

## Release relationship

Existing labels remain:

- **Development Accepted** — engineering milestone passed.
- **Release Candidate** — production software/SRE/security gates passed.
- **Production Qualified** — required real-camera/hardware/network evidence passed.
- **Full Market Feature Complete** — every capability in the market master
  checklist is verified through an accepted support route.

Production Qualified does not automatically mean Full Market Feature Complete,
and vice versa.
