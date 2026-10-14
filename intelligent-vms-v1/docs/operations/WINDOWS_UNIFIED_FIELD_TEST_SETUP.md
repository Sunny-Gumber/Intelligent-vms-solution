# Windows unified field-test setup

This is a **field-test installer foundation**, not the final commercial installer and not Production Qualified.

## Build

CI pins NSIS 3.13.0 and runs:

```powershell
.\deploy\windows\installer\Build-Installer.ps1 -CommitSha <sha>
```

Primary artifact: `IntelligentVMS-FieldTest-Setup-x64-<product_version>.exe` (currently 0.2.1, per `release/windows/product-version.json`), plus a sibling `.sha256`.

## Interactive install

Run Setup as administrator and choose Server, Client or Both. For Server/Both choose a local recording folder.

## Silent examples

```text
Setup.exe /S /MODE=client
Setup.exe /S /MODE=server /RECORDINGROOT=D:\VMS\Recordings
Setup.exe /S /MODE=both /RECORDINGROOT=D:\VMS\Recordings
Setup.exe /S /MODE=both /ACTION=repair /RECORDINGROOT=D:\VMS\Recordings
Setup.exe /S /MODE=both /ACTION=upgrade /RECORDINGROOT=D:\VMS\Recordings
```

Existing-version detection converts a normal Install into repair or upgrade.
A lower **product version** is rejected, but an older security build with the
same product version can be classified as repair. An approved release must
therefore be selected by its exact installer SHA-256 and reviewed commit, not
product version alone. Withdraw obsolete installer packages; this field-test
installer does not implement cryptographic anti-rollback against every prior
same-version build.

## Prerequisites

Client is self-contained .NET 10 win-x64 but requires the official Microsoft WebView2 Evergreen Runtime.

Server/Both reuse the accepted Windows field-test server architecture and currently require Python 3.12 x64 plus a supported local PostgreSQL x64 service version 14 or newer. Interactive Setup asks for the local PostgreSQL administrator password in a masked field and passes it only through the child-process environment for first-time DB bootstrap; it is not placed on the command line or installer log. Unattended setup must provide the same `VMS_POSTGRES_ADMIN_PASSWORD` environment variable before launch. Setup does not uninstall shared PostgreSQL and does not download arbitrary latest prerequisites.

### Media runtime build cache and network boundary

PR #174 retains MediaMTX runtime label `1.21.1-vms.1` while patching the Go
compiler and x/net dependency. The accepted MediaMTX build is identified by the
source commit plus **both** the ZIP and executable hashes recorded in
`infra/mediamtx/runtime.json`. Older same-name cached ZIPs fail the checksum
gate. On a stale-hash packaging failure, use a fresh `-OutputRoot` or remove
only `<OutputRoot>/build-inputs/mediamtx_v1.21.1-vms.1_windows_amd64.zip`
before rebuilding. Never delete retained recordings or server configuration.

MediaMTX installation uses the verified bundled ZIP and requires no live
MediaMTX download. **The full Server/Both installation is not air-gapped:**
`Install-WindowsFieldTest.ps1` upgrades pip and installs Python requirements;
Python, PostgreSQL, WebView2 and other applicable prerequisites follow the
documented provisioning paths. No fully offline setup is claimed or tested.

## Data ownership

| Component | Path/owner | Upgrade | Default uninstall |
|---|---|---|---|
| Setup metadata | Program Files / machine | replaced | removed |
| Client binaries | Program Files / machine | replaced | removed |
| Client settings/logs | LocalAppData / user | preserved | preserved |
| Client credentials | Credential Manager / user | preserved | preserved |
| Server app/runtime | protected ProgramData / machine | replaced | binaries removed |
| Server config/secrets | protected ProgramData / machine | preserved | preserved |
| Server logs | protected ProgramData / machine | preserved/bounded | preserved |
| Backups | protected ProgramData / machine | preserved | preserved |
| Recordings | selected local folder | preserved | preserved |
| PostgreSQL DB/data | PostgreSQL service owner | migrated/preserved | preserved |

No recording purge is implemented by this field-test setup.

## Failure/recovery

A global setup mutex blocks concurrent install/upgrade/uninstall operations. Preflight occurs before component mutation. Server upgrade uses the accepted backup path. On failure, redacted installer diagnostics are retained and existing recordings/config are not deleted. Database schema rollback is not guessed; recovery after a migrated schema failure is roll-forward from preserved backup/config.

Hosted CI on Windows Server 2022/2025 is software evidence only and does not qualify Windows 10/11, OEM images, endpoint protection, GPO, real cameras or production signing.


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
