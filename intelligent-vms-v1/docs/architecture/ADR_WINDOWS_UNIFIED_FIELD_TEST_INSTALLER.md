# ADR — Windows unified field-test installer foundation

Status: Accepted for field-test implementation in issue #19. Product remains **Release Candidate / External Qualification Pending**.

## Decision

Use **NSIS 3.13** as the setup container and wizard technology. NSIS provides a mature Windows installer engine, machine-wide elevation, custom pages, silent switches, version resources, uninstall support and future signing compatibility without requiring the VMS application to invent its own executable bootstrap format.

NSIS 3.12 fixed an elevated temporary-directory privilege-escalation class; 3.13 is the pinned compiler baseline for this milestone. NSIS source and primary components are distributed under permissive licenses that permit commercial use. The project does not add a product EULA or change this repository's license decision.

## Architecture

The setup EXE is an orchestration layer. It does not merge server and client runtime/data ownership.

- Setup metadata/uninstaller: `C:\Program Files\Intelligent VMS\`
- Desktop binaries: `C:\Program Files\Intelligent VMS\Client\`
- Desktop settings/logs: existing per-user `%LOCALAPPDATA%\IntelligentVMS\Client\`
- Desktop secrets: Windows Credential Manager
- Server runtime/config/data: existing protected `C:\ProgramData\IntelligentVMS\`
- Recordings: operator-selected local path outside installer-owned binary/config trees
- PostgreSQL: supported existing local x64 service, never automatically removed
- Server service chain: PostgreSQL → IntelligentVMSControl → IntelligentVMSMedia

The installer reuses `Install-WindowsFieldTest.ps1`, `Vms-Windows.ps1`, service-manager logic, MediaMTX pin/hash, migrations, ACLs, backup and diagnostics rather than replacing accepted server architecture.

## Modes and privilege

Interactive setup provides Server, Client and Both. Silent automation uses `/MODE=server|client|both`. Both installs and validates Server before copying Client.

The unified package is machine-wide and therefore requests elevation for all modes. This is deliberate because shared client binaries are installed in Program Files while user-specific client state remains in LocalAppData/Credential Manager.

## Upgrade/repair/downgrade

Machine-wide, non-secret installer state records version, commit, installed components and recording root. A newer candidate becomes Upgrade, same version becomes Repair, and a candidate older than installed version is rejected. Concurrent setup operations are blocked with a global mutex.

Server upgrade/repair delegates to the accepted server `-Upgrade` path: metadata/config backup, orderly service stop, binary replacement, migrations and health validation. Client upgrade preserves per-user state and replaces only Program Files binaries after closing the desktop process.

Database rollback is not promised after migrations. If schema rollback is unsafe, recovery is roll-forward from preserved configuration/database backup and diagnostics.

## Prerequisites

The field-test foundation detects and fails actionably for x64 Windows, elevation, safe recording path, Python 3.12 x64, PostgreSQL 14+, required fixed server ports and WebView2 Evergreen.

PostgreSQL is explicitly **reused**, not silently hijacked or automatically uninstalled. The current field-test foundation does not bundle PostgreSQL or Python; a future commercial packaging milestone may internalize approved prerequisites. Client .NET is self-contained.

## Data safety and security

Default uninstall removes installer-owned binaries, services and shortcuts but preserves recordings, server config/secrets/backups/database, client LocalAppData and Credential Manager state. No recording purge is implemented.

NSIS uses its private per-run `$PLUGINSDIR` only for the bounded preflight script before machine mutation; installed/runtime executables never run from a user-writable install location. The CI baseline pins NSIS 3.13 and treats private extraction ownership as part of the installer security boundary. Existing service path quoting, ProgramData ACLs and service recovery remain authoritative. No broad firewall rules are created and PostgreSQL is not exposed publicly. The artifact is unsigned field-test software and does not claim trusted-publisher status.

Windows 10/11, OEM, endpoint-security, GPO and real-camera qualification remain external.


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
