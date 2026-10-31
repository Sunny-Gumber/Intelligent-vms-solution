# Installers

| Path | Target | Status |
| --- | --- | --- |
| `windows/install.ps1` | Windows Server 2022/2025, Windows 11 | **Never executed** |
| `windows/uninstall.ps1` | Windows | **Never executed** |
| `../deploy/ubuntu/install.sh` | Ubuntu 22.04/24.04 | **Never executed** |
| `../deploy/docker/` | Any Docker host | Exercised during development |

No Windows or clean Ubuntu host was available in the build environment.
Expect to correct details on first install, and update these scripts with
what you learn.

## Building an MSI

The PowerShell installer is the supported path for 0.1.0-alpha. For a signed
MSI:

1. Use WiX Toolset v4 or Advanced Installer.
2. Harvest `InstallPath` after running `install.ps1 -SkipServices`.
3. Register services in a custom action so dependency ordering is preserved.
4. Sign with an EV code-signing certificate — unsigned installers for
   security software will be blocked by SmartScreen and most customer
   endpoint policies.

## Non-negotiables for any installer

- `keys\` ACL restricted to SYSTEM and Administrators, inheritance removed
- A generated secret key, never a shipped default
- Database migration applied before services start
- Recorder stops **before** the API on uninstall, so segments finalize
- Uninstall preserves data unless explicitly told otherwise
