# Windows Server Deployment (2022 / 2025)

> **Not executed.** No Windows host was available during development. The
> installer and service definitions are delivered as source and have never
> been run. Treat this as a first-install runbook, not a verified procedure.

## 1. Prerequisites

- Windows Server 2022 or 2025, or Windows 11 for a workstation install
- PostgreSQL 15+ for Windows
- FFmpeg (full build) on `PATH`, or set `ffmpeg_path` in the configuration
- MediaMTX `mediamtx.exe`
- Python 3.12 (64-bit)

## 2. Install

Run PowerShell **as Administrator**:

```powershell
cd installers\windows
.\install.ps1 -InstallPath "C:\Program Files\Intelligent VMS" `
              -DataPath "C:\ProgramData\IntelligentVMS" `
              -DatabaseUrl "postgresql+psycopg://vms:PASSWORD@localhost/intelligentvms"
```

The script:
1. Creates the install and `ProgramData` directories.
2. Restricts ACLs on `keys\` to `SYSTEM` and `Administrators` only.
3. Creates a virtual environment and installs the packages.
4. Writes `vms.toml` with a generated secret key.
5. Runs the Alembic migration.
6. Registers three Windows services with correct dependency ordering.
7. Starts them.

## 3. Services

| Service | Depends on | Purpose |
| --- | --- | --- |
| `IntelligentVMS-MediaMTX` | — | Media gateway |
| `IntelligentVMS-API` | postgresql, MediaMTX | Control API |
| `IntelligentVMS-Recorder` | IntelligentVMS-API | Recording workers |

All are set to **Automatic (Delayed Start)** with a restart-on-failure
recovery policy.

## 4. Firewall

```powershell
New-NetFirewallRule -DisplayName "Intelligent VMS API"    -Direction Inbound -LocalPort 8080 -Protocol TCP -Action Allow
New-NetFirewallRule -DisplayName "Intelligent VMS WebRTC" -Direction Inbound -LocalPort 8889 -Protocol TCP -Action Allow
New-NetFirewallRule -DisplayName "Intelligent VMS WebRTC ICE" -Direction Inbound -LocalPort 8189 -Protocol UDP -Action Allow
```

Do **not** open the camera VLAN to the client network. See
`docs/deployment/firewall.md`.

## 5. First sign-in

```powershell
Get-Content "C:\ProgramData\IntelligentVMS\first-run-credentials.txt"
```

Change the password, then delete the file.

## 6. Storage

Add recording volumes through the web UI or the API. For a dedicated array:

```
D:\VMS\Recordings
```

The service account needs **Modify** on that path. A volume that exists but is
not writable is rejected at registration rather than discovered at 3 a.m.

## 7. Upgrade and uninstall

```powershell
.\upgrade.ps1        # stops services, backs up config, migrates, restarts
.\uninstall.ps1      # removes services and program files; KEEPS ProgramData
```

`uninstall.ps1` preserves recordings and configuration unless
`-RemoveData` is passed explicitly.
