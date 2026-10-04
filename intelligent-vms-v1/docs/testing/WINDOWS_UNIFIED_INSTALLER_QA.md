# Windows unified field-test installer — independent QA plan

Issue #19.

Exact-head automated evidence must cover:
- NSIS package build and SHA-256;
- Server-only clean install/health/uninstall/data preservation;
- Client-only install/smoke/uninstall without touching server services;
- Both install with Server first, server health and client executable;
- same-version repair restores an intentionally removed client binary while preserving server config;
- upgrade simulation from the accepted script/client baseline preserves config/data and runs current migrations;
- unsafe downgrade is rejected;
- invalid recording paths and representative port/prerequisite failures fail before destructive changes;
- default uninstall retains recording tree, config, backups, database and client LocalAppData;
- Windows Server 2022 and 2025 matrix;
- existing Windows Client, VMS CI, Security and Ubuntu 22.04/24.04 regressions remain green.

Hosted runners do not establish Windows 10/11, real-camera, endpoint-security, GPO or production-signing qualification.
