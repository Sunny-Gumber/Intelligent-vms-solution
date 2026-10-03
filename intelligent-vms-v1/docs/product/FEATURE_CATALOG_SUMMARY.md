# VMS Feature Catalog Summary

Generated from `VMS_COMPLETE_FEATURE_MASTER_CHECKLIST.md`.

Deterministic capability rows: **623**

> Count is based on the checklist's middle-dot-separated capability entries. Compound phrases remain one catalog row unless the source itself separates them.

| Category | Name | Maturity | Catalog rows | Workstream |
|---:|---|---|---:|---|
| 1 | Device and Camera Support | LEVEL 1 — Basic / Small VMS | 43 | A-Core-VMS |
| 2 | Live Video Monitoring | LEVEL 1 — Basic / Small VMS | 21 | A-Core-VMS |
| 3 | Basic Recording | LEVEL 1 — Basic / Small VMS | 20 | A-Core-VMS |
| 4 | Playback and Export | LEVEL 1 — Basic / Small VMS | 17 | A-Core-VMS |
| 5 | Basic PTZ Control | LEVEL 1 — Basic / Small VMS | 14 | A-Core-VMS |
| 6 | Basic User Management | LEVEL 1 — Basic / Small VMS | 10 | A-Core-VMS |
| 7 | Basic Events | LEVEL 1 — Basic / Small VMS | 13 | A-Core-VMS |
| 8 | Basic Client Applications | LEVEL 1 — Basic / Small VMS | 9 | A-Core-VMS |
| 9 | Advanced Device Management | LEVEL 2 — Professional VMS | 20 | B-Professional-VMS |
| 10 | Professional Recording and Storage | LEVEL 2 — Professional VMS | 19 | B-Professional-VMS |
| 11 | Professional Playback and Investigation | LEVEL 2 — Professional VMS | 18 | B-Professional-VMS |
| 12 | Professional Alarm Management | LEVEL 2 — Professional VMS | 12 | B-Professional-VMS |
| 13 | Maps and Electronic Maps | LEVEL 2 — Professional VMS | 11 | B-Professional-VMS |
| 14 | Professional Reporting | LEVEL 2 — Professional VMS | 10 | B-Professional-VMS |
| 15 | Advanced Video Analytics | LEVEL 3 — Advanced VMS | 21 | C-Analytics-Metadata-Identity |
| 16 | Metadata Management | LEVEL 3 — Advanced VMS | 6 | C-Analytics-Metadata-Identity |
| 17 | Advanced Search | LEVEL 3 — Advanced VMS | 11 | C-Analytics-Metadata-Identity |
| 18 | ANPR/LPR Management | LEVEL 3 — Advanced VMS | 15 | C-Analytics-Metadata-Identity |
| 19 | Face Management | LEVEL 3 — Advanced VMS | 11 | C-Analytics-Metadata-Identity |
| 20 | Access Control Integration | LEVEL 3 — Advanced VMS | 11 | D-Physical-Security-Control-Room |
| 21 | Intercom and Audio Integration | LEVEL 3 — Advanced VMS | 7 | D-Physical-Security-Control-Room |
| 22 | Video Wall and Control Room | LEVEL 3 — Advanced VMS | 14 | D-Physical-Security-Control-Room |
| 23 | Enterprise Architecture | LEVEL 4 — Enterprise VMS | 11 | E-Enterprise-Platform |
| 24 | Redundancy and High Availability | LEVEL 4 — Enterprise VMS | 12 | E-Enterprise-Platform |
| 25 | Enterprise User and Identity Management | LEVEL 4 — Enterprise VMS | 12 | E-Enterprise-Platform |
| 26 | Cybersecurity | LEVEL 4 — Enterprise VMS | 16 | E-Enterprise-Platform |
| 27 | Privacy and Compliance | LEVEL 4 — Enterprise VMS | 13 | E-Enterprise-Platform |
| 28 | Health Monitoring and Maintenance | LEVEL 4 — Enterprise VMS | 16 | E-Enterprise-Platform |
| 29 | Enterprise Integration | LEVEL 4 — Enterprise VMS | 12 | F-Integration-GIS-Workflow |
| 30 | GIS and Smart-City Features | LEVEL 4 — Enterprise VMS | 12 | F-Integration-GIS-Workflow |
| 31 | Workflow and Automation | LEVEL 4 — Enterprise VMS | 6 | F-Integration-GIS-Workflow |
| 32 | Advanced AI Analytics | LEVEL 5 — Best-in-Class AI and Command-Centre VMS | 13 | G-AI-Command-Centre |
| 33 | Natural-Language and Generative AI | LEVEL 5 — Best-in-Class AI and Command-Centre VMS | 11 | G-AI-Command-Centre |
| 34 | Intelligent Investigation | LEVEL 5 — Best-in-Class AI and Command-Centre VMS | 9 | G-AI-Command-Centre |
| 35 | Command-and-Control | LEVEL 5 — Best-in-Class AI and Command-Centre VMS | 9 | G-AI-Command-Centre |
| 36 | Cloud and Hybrid-Cloud Features | LEVEL 5 — Best-in-Class AI and Command-Centre VMS | 11 | H-Cloud-BI-Evidence-Commercial |
| 37 | Advanced Bandwidth Optimization | LEVEL 5 — Best-in-Class AI and Command-Centre VMS | 10 | H-Cloud-BI-Evidence-Commercial |
| 38 | Business Intelligence | LEVEL 5 — Best-in-Class AI and Command-Centre VMS | 8 | H-Cloud-BI-Evidence-Commercial |
| 39 | Evidence and Legal-Grade Functions | LEVEL 5 — Best-in-Class AI and Command-Centre VMS | 9 | H-Cloud-BI-Evidence-Commercial |
| 40 | Licensing and Commercial Features | LEVEL 5 — Best-in-Class AI and Command-Centre VMS | 10 | H-Cloud-BI-Evidence-Commercial |
| 41 | Deployment, Migration and Support | LEVEL 5 — Best-in-Class AI and Command-Centre VMS | 10 | H-Cloud-BI-Evidence-Commercial |
| 42 | Standards, Certifications and Compliance | ADDITIONAL CATEGORIES | 24 | I-Extended-Market |
| 43 | Hardware Acceleration and Performance | ADDITIONAL CATEGORIES | 10 | I-Extended-Market |
| 44 | Extended Connectivity and Streaming Protocols | ADDITIONAL CATEGORIES | 11 | I-Extended-Market |
| 45 | Mobile and Field Video Sources | ADDITIONAL CATEGORIES | 7 | I-Extended-Market |
| 46 | AI Platform and Model Governance | ADDITIONAL CATEGORIES | 10 | I-Extended-Market |
| 47 | Localization and Accessibility | ADDITIONAL CATEGORIES | 7 | I-Extended-Market |
| 48 | Emerging Investigation and Automation Features | ADDITIONAL CATEGORIES | 11 | I-Extended-Market |

## Default import state

- support mode: `R` (Roadmap/unreleased)
- engineering state: `TARGET`
- verification status: `NV` (Not verified)

Existing repository functionality must be audited before rows are promoted to stronger status.

## Category 1 software audit

The 2026-09-28 [Device and Camera Support audit](../reviews/CATEGORY1_CAMERA_CAPABILITY_AUDIT.md)
tracks all 43 Category 1 rows. RTSP/MediaMTX software wiring is in `QA / I`
with bounded documented scope; 42 remain `TARGET`. All 43 are `NV` for final
verification. The remaining 580 rows in other categories retain their default
`R / TARGET / NV` state. This is not a named-camera, hardware-scale, or
production support claim.

## Preserving engineering evidence

Edit `FEATURE_EVIDENCE_OVERLAY.csv` to record evidence and engineering status by
Feature ID. Keep the master checklist unchanged. The generator allows only the
overlay's mutable fields; names, categories, maturity and workstream identities
continue to come from the checklist.

From `intelligent-vms-v1/`, run:

```bash
python tools/feature_catalog.py
python tools/feature_catalog.py --check
```

Both commands require the overlay file, including when `--overlay` selects a
custom path. Use a header-only overlay for an intentional empty evidence set.
A missing overlay is an error so a misplaced file cannot silently erase existing
evidence. Invalid CSV syntax, column counts, headers, IDs or enumerated states
also fail before the generated catalog is written. Empty cells preserve the
checklist's import defaults. Commit the overlay and regenerated catalog together.
