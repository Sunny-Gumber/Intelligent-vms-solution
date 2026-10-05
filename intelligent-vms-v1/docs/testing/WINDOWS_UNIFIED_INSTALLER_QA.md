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


## v0.2.1 Windows installer qualification blocker remediation

Issue #23 / PR pending.

The 0.2.1 field-test candidate fixes defects found during the first real Windows 11 qualification attempt without changing the product qualification claim.

- PostgreSQL client discovery uses the selected Windows service ImagePath, official PostgreSQL installation registry entries, standard 64-bit Program Files paths, and only then a validated PGBIN fallback. Candidates must expose psql/createdb/postgres, be AMD64 PE binaries, and report PostgreSQL major 14+.
- MediaMTX 1.21.1 is now a pinned installer build input. Build verifies SHA-256 `faa97974861eb75a68b5aa326c78e7e7a6f670b5ef191bace78e715130381f23` and embeds the ZIP into the installer payload. Normal installation performs no GitHub MediaMTX download and re-verifies the bundled hash before extraction.
- Quiet/unattended setup passes `-NonInteractive`; missing PostgreSQL bootstrap credentials fail immediately instead of entering an invisible `Read-Host`.
- PostgreSQL admin credentials stay in process environment only. The generated VMS database password is passed to the env generator through a temporary environment variable, never a process argument. Role create/update SQL uses a protected temporary SQL file rather than embedding the database password in a command line.
- Fresh-install bootstrap has explicit phases: `config_generated`, `role_ready`, `database_ready`, `completed`. A preserved `vms.env` alone is not treated as proof that bootstrap completed. Retry reconciles the role/database using the preserved VMS DB secret and does not rotate legitimate configuration.
- Long-running external commands use bounded execution; health validation remains explicitly bounded.
- Server installer stage output is streamed into `setup.log` while the child is running. Safe markers identify PostgreSQL discovery/auth/role/database, Python environment, runtime install, Alembic, service install/start, health and completion.
- GitHub-only failure injection points exist solely for deterministic retry CI and are inert outside GitHub Actions.
- Product remains **Release Candidate / External Qualification Pending**. This remediation does not qualify Windows 11, Windows 10, cameras, performance/capacity, or production signing.
