# Windows Portability Blocker Inventory

Baseline source: Ubuntu field-test #301. Current implementation milestone: #303.

This inventory distinguishes **field-test foundation addressed by #303** from items that
still require commercial packaging or external qualification. It is not a Windows
Production Qualified claim.

| Area | Classification | #303 field-test disposition / remaining boundary |
|---|---|---|
| Control API Python business/auth logic | PORTABLE ALREADY | Reused unchanged except deployment-profile/static-web switches; Server CI required. |
| Browser client/session | PORTABLE ALREADY | Same browser/session/CSRF logic; FastAPI serves assets in Windows small-site. |
| Camera/tenant/site security | PORTABLE ALREADY | Same authorization and network-target validation. |
| Internal `/recordings` assumption | CONFIGURATION CHANGE + STORAGE/PATH CHANGE | Windows generator emits drive-aware forward-form MediaMTX path from administrator-selected local volume. |
| `/var/lib/vms-node`, `/var/lib/vms-spool` | STORAGE/PATH CHANGE | Not used by windows-small-site because regional/distributed execution is disabled; enterprise profile unchanged. |
| Bash lifecycle/backup | CODE CHANGE | Native PowerShell field lifecycle/backup/restore/diagnostics added. |
| chmod/umask | CODE CHANGE + STORAGE/PATH CHANGE | Windows field profile uses explicit ACLs via icacls for config/recordings/diagnostics. |
| Linux Docker/Compose server path | SERVICE WRAPPER REQUIRED + INSTALLER REQUIRED | Native SCM path added; no Docker Desktop/WSL2 for small-site. Final Setup.exe/MSI still required. |
| Control API lifecycle | SERVICE WRAPPER REQUIRED | Native SCM service using isolated Python runtime. |
| MediaMTX lifecycle | SERVICE WRAPPER REQUIRED + EXTERNAL QUALIFICATION | Official Windows amd64 binary, pinned checksum, native SCM service; real camera/codec/reboot still external. |
| PostgreSQL | INSTALLER REQUIRED + EXTERNAL QUALIFICATION | Native PostgreSQL service remains authoritative and is automatically configured; field prerequisite is installed PostgreSQL. Final installer must bundle/manage it. |
| ClickHouse/Redpanda | PROFILE DECISION + EXTERNAL QUALIFICATION | Not mandatory for core small-site loop; remain enterprise-distributed dependencies. |
| Firewall | INSTALLER REQUIRED | Loopback-only profile needs no inbound allow rule; firewall is never disabled. Secure remote/TLS exposure remains future installer work. |
| Recording volume ACL/free space | STORAGE/PATH CHANGE + EXTERNAL QUALIFICATION | Absolute local path + ACL/free-space diagnostics; UNC explicitly rejected. NTFS/ReFS durability remains external. |
| Atomic/temp-file behavior | CODE CHANGE + EXTERNAL QUALIFICATION | No new Windows temp-media path introduced; recording filesystem semantics require soak/crash qualification. |
| POSIX recording hook | CODE CHANGE | Cross-platform Python completion helper removes curl/$VAR dependency on Windows. |
| Process/service stop | SERVICE WRAPPER REQUIRED + EXTERNAL QUALIFICATION | SCM control/media services with bounded stop/recovery; physical reboot/recording gap remains external. |
| Backup/restore paths | CODE CHANGE + STORAGE/PATH CHANGE | Native pg_dump/pg_restore workflow and ProgramData backup path; recording media explicitly excluded. |
| Upgrade/rollback | INSTALLER REQUIRED + SERVICE WRAPPER REQUIRED | Field upgrade backs up/stops/replaces/migrates/restarts; rollback only across compatible schema or backup restore. |
| Uninstall/purge | INSTALLER REQUIRED | Normal uninstall removes service registrations only; DB purge separately confirmed; recordings/config preserved. |
| Windows 11 64-bit | EXTERNAL QUALIFICATION | Primary target; no matching GitHub-hosted x64 Windows 11 runner, so physical/VM qualification remains required. |
| Windows Server 2022 | AUTOMATED FIELD-TEST VALIDATION | Dedicated hosted x64 native-service CI. |
| Windows Server 2025 | AUTOMATED FIELD-TEST VALIDATION | Dedicated hosted x64 native-service CI. |
| Windows 10 64-bit | EXTERNAL QUALIFICATION | Compatibility target only; no hosted runner and not preferred new deployment platform. |
| Native Windows desktop client | CODE CHANGE + INSTALLER REQUIRED + EXTERNAL QUALIFICATION | Explicitly next milestone after Windows server acceptance; not started in #303. |

No row above authorizes Production Qualified status, certified camera/storage/capacity
claims, or Windows desktop-client availability.
