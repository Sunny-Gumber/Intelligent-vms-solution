# Windows Native Server Field-Test Baseline

Issue #303 implements the **windows-small-site** field-test server profile. It is not
the final polished Setup.exe/MSI and does not start the native Windows desktop client.

Product state remains **Release Candidate / External Qualification Pending**.

## Target matrix

| OS | Product priority | Automated evidence in this milestone | Status boundary |
|---|---|---|---|
| Windows 11 64-bit | Primary | No matching GitHub-hosted x64 Windows 11 runner | Physical/VM qualification required |
| Windows Server 2022 x64 | Professional | GitHub-hosted windows-2022 native-service matrix | Automated field-test deployment evidence |
| Windows Server 2025 x64 | Professional | GitHub-hosted windows-2025 native-service matrix | Automated field-test deployment evidence |
| Windows 10 64-bit | Compatibility only | No hosted runner | External compatibility qualification; not preferred new deployment |

## Field-test prerequisites

- x64 Windows target listed above;
- elevated PowerShell;
- Python 3.12 x64 installed;
- native PostgreSQL x64 14 or newer installed as a Windows service;
- outbound HTTPS during install for the pinned official MediaMTX v1.21.1 Windows archive.

The field installer automatically configures the VMS database, Python environment,
MediaMTX, services, migrations and protected configuration. The tester does not edit
database configuration or launch individual VMS components.

The **final commercial installer** must bundle/install approved Python/PostgreSQL/
MediaMTX dependencies and present Server/storage/network choices in Setup.exe/MSI.
The current prerequisite boundary is deliberate field-test foundation, not final UX.

## Install

Choose a local recording volume. Example:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\deploy\windows\Install-WindowsFieldTest.ps1 `
  -RecordingRoot "D:\VMS\Recordings" `
  -CameraCidr "192.168.1.0/24"
```

If PostgreSQL is not already initialized for local administrator access, the installer
prompts securely for its `postgres` administrator password. CI supplies this through
the process-only `VMS_POSTGRES_ADMIN_PASSWORD` environment variable; do not put a
database password on the command line.

Installation creates:

- `%ProgramData%\IntelligentVMS\app` — installed VMS assets;
- `runtime\venv` — isolated Python 3.12 environment;
- `runtime\mediamtx` — pinned native MediaMTX executable;
- `config\vms.env` — protected secret/configuration file;
- `config\mediamtx.yml` — loopback native media configuration;
- `logs` and `backups`;
- the administrator-selected recording directory.

Config and recording paths have inherited ACLs removed and grant full access only to
LocalSystem and local Administrators for this field-test baseline.

## Native service lifecycle

Services:

1. native PostgreSQL service;
2. `IntelligentVMSControl`;
3. `IntelligentVMSMedia`.

Control depends on PostgreSQL; Media depends on Control. Both VMS services are
Automatic delayed-start and receive bounded restart-on-failure actions.

Use:

```powershell
.\deploy\windows\Vms-Windows.ps1 -Action Status
.\deploy\windows\Vms-Windows.ps1 -Action Health
.\deploy\windows\Vms-Windows.ps1 -Action Stop
.\deploy\windows\Vms-Windows.ps1 -Action Start
.\deploy\windows\Vms-Windows.ps1 -Action Restart
```

Open `http://127.0.0.1:8000` locally. Generate a short-lived field-test token with the
installed Python/runtime and existing `deploy/field-test/mint_access_token.py`, using
`%ProgramData%\IntelligentVMS\config\vms.env`.

## Small-site capability profile

Available core field loop:

- authenticated browser and authorized camera add;
- MAIN/SUB/THIRD where configured;
- live view;
- continuous MAIN source-copy recording and retention;
- local MediaMTX timeline/playback;
- snapshot;
- durable manual-recording interval intent;
- bounded MP4 export;
- health and diagnostics.

Not available in this profile:

- Kafka/Redpanda event pipeline;
- ClickHouse event history;
- alarm-worker processing;
- regional/distributed placement and failover;
- enterprise event/recording-metadata fan-out;
- compact AI orchestration UI.

Those capabilities are not removed from the product. They remain in the existing
enterprise-distributed deployment profile.

## PostgreSQL

PostgreSQL remains authoritative. No SQLite replacement exists. The installer detects
a native x64 PostgreSQL service, sets it to Automatic, creates/updates a random `vms`
role password, creates the `vms` database if absent, stores credentials only in protected
configuration and runs explicit Alembic migrations with `AUTO_CREATE_SCHEMA=false`.

The final commercial installer must own PostgreSQL packaging/version policy. This
field-test baseline validates shared product behavior against PostgreSQL versions
present on the Server 2022 and Server 2025 hosted runners.

## MediaMTX

The installer downloads only `mediamtx_v1.21.1_windows_amd64.zip` and verifies SHA-256
`faa97974861eb75a68b5aa326c78e7e7a6f670b5ef191bace78e715130381f23`.

MediaMTX uses the same JWT/JWKS trust, dynamic live paths, source-copy MAIN recording,
timeline/playback and export behavior as Linux. The Windows recording hook uses the VMS
Python helper and reads its token from protected configuration; secrets are not embedded
in the MediaMTX command.

## Recording storage

Recording storage must be an absolute local Windows path such as
`D:\VMS\Recordings`. UNC/network recording storage is explicitly rejected in this
baseline. NTFS/ReFS long-run durability, removable storage, SAN/NAS and crash consistency
require external qualification. Restart, upgrade and normal uninstall never delete
recordings.

## Network and firewall

The field profile is loopback-only:

- 8000/TCP browser + control API;
- 9997/TCP MediaMTX API;
- 9998/TCP metrics;
- 9996/TCP playback;
- 8889/TCP WebRTC signaling;
- 8189/UDP WebRTC media;
- 8888/TCP HLS;
- 8554/TCP RTSP listener.

The installer therefore creates no inbound Windows Firewall allow rule and never
disables Windows Firewall. Camera RTSP/ONVIF connectivity is outbound. Secure remote
browser/TLS exposure remains a later installer qualification item.

## Secrets and authentication

Secrets live only in protected `vms.env`; generated values are not printed. Diagnostics
record configuration presence only and redact configured passwords, tokens, private
keys, bearer values and credential-bearing RTSP/HTTP(S) URLs.

The existing same-origin HttpOnly browser session + CSRF bridge is reused. Local HS256
is field-test bootstrap only. Production OIDC rules remain fail-closed.

## Backup / restore

```powershell
.\deploy\windows\Vms-Windows.ps1 -Action Backup
```

The quick backup contains a PostgreSQL custom dump/checksum and safe MediaMTX
configuration. It does not back up recording media. Secrets are excluded unless
`-IncludeSecrets` is deliberately supplied.

Restore is confirmation-gated:

```powershell
.\deploy\windows\Vms-Windows.ps1 -Action Restore `
  -BackupFile "C:\...\database.dump" `
  -Confirmation RESTORE_WINDOWS_FIELD_TEST_DATABASE
```

Restore stops VMS writers, restores PostgreSQL, reapplies current migrations, restarts
services and leaves recording media untouched. Encrypted camera credentials require the
original `VMS_SECRET_KEY`.

## Upgrade / rollback

Run the installer from the new approved checkout with the same RecordingRoot and
`-Upgrade`. Upgrade first creates a PostgreSQL backup and stops VMS services, then
replaces application assets, reapplies dependencies/migrations, restarts and health
checks. Application rollback is supported only when schema-compatible; otherwise
restore the approved pre-upgrade database/config state. No forced irreversible Alembic
downgrade is claimed.

## Diagnostics

```powershell
.\deploy\windows\Vms-Windows.ps1 -Action Diagnostics
```

The bounded ZIP includes Windows build/architecture, VMS/PostgreSQL service state,
health/readiness, recording path/free space, relevant firewall rule state and the latest
500 VMS log lines after redaction. The archive ACL is limited to SYSTEM and Administrators.

## Uninstall / purge

Normal `-Action Uninstall` removes only the two VMS SCM registrations. PostgreSQL data,
recordings, protected configuration/secrets, backups and installed assets remain.

Database purge is separate and requires
`-Confirmation DELETE_WINDOWS_FIELD_TEST_DATABASE`. Even that action does not delete
recordings or protected configuration.

Follow `docs/testing/WINDOWS_FIELD_TEST_SMOKE.md` for real host/camera qualification.
