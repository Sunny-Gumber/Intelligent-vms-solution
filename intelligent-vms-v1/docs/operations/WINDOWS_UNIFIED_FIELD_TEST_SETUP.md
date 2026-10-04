# Windows unified field-test setup

This is a **field-test installer foundation**, not the final commercial installer and not Production Qualified.

## Build

CI pins NSIS 3.13.0 and runs:

```powershell
.\deploy\windows\installer\Build-Installer.ps1 -CommitSha <sha>
```

Primary artifact: `IntelligentVMS-FieldTest-Setup-x64-0.2.0.exe`, plus a sibling `.sha256`.

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

Existing-version detection converts a normal Install into repair or upgrade. Downgrade is blocked.

## Prerequisites

Client is self-contained .NET 10 win-x64 but requires the official Microsoft WebView2 Evergreen Runtime.

Server/Both reuse the accepted Windows field-test server architecture and currently require Python 3.12 x64 plus a supported local PostgreSQL x64 service version 14 or newer. Interactive Setup asks for the local PostgreSQL administrator password in a masked field and passes it only through the child-process environment for first-time DB bootstrap; it is not placed on the command line or installer log. Unattended setup must provide the same `VMS_POSTGRES_ADMIN_PASSWORD` environment variable before launch. Setup does not uninstall shared PostgreSQL and does not download arbitrary latest prerequisites.

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
