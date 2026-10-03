# Windows Portability Blocker Inventory

Input baseline: Ubuntu field-test issue #301. Planning input only; not a Windows support claim.

| Area | Classification | Required Windows work |
|---|---|---|
| Control API Python business/auth logic | PORTABLE ALREADY + EXTERNAL QUALIFICATION | Validate Windows runtime/dependencies; keep one shared backend. |
| Browser client/session | PORTABLE ALREADY + EXTERNAL QUALIFICATION | Qualify supported Windows browsers. |
| Camera/tenant/site security contracts | PORTABLE ALREADY | Preserve shared authorization/target validation. |
| Internal /recordings path | CONFIGURATION CHANGE + STORAGE/PATH CHANGE | Native Windows path cannot assume /recordings. |
| /var/lib/vms-node and /var/lib/vms-spool | STORAGE/PATH CHANGE | Add Windows product-data directory abstraction. |
| Bash lifecycle/backup scripts | CODE CHANGE | PowerShell/native cross-platform management required. |
| umask/chmod Unix permissions | CODE CHANGE + STORAGE/PATH CHANGE | Implement Windows ACL policy. |
| Linux Docker Compose server path | SERVICE WRAPPER REQUIRED + INSTALLER REQUIRED | Final Windows path must not be Docker Desktop. |
| Control API/workers lifecycle | SERVICE WRAPPER REQUIRED | Windows services start before login and recover predictably. |
| MediaMTX | SERVICE WRAPPER REQUIRED + EXTERNAL QUALIFICATION | Package/qualify Windows service and media/firewall behavior. |
| PostgreSQL | INSTALLER REQUIRED + EXTERNAL QUALIFICATION | Select supported bundled/external Windows service lifecycle. |
| ClickHouse/Redpanda | CODE CHANGE + INSTALLER REQUIRED + EXTERNAL QUALIFICATION | Select supported Windows-compatible topology without redesigning architecture silently. |
| Firewall | INSTALLER REQUIRED | Add minimal explicit install/remove rules. |
| Recording volume permissions | STORAGE/PATH CHANGE + EXTERNAL QUALIFICATION | Qualify NTFS/ReFS ACL/free-space/retention semantics. |
| Atomic/temp-file behavior | CODE CHANGE + EXTERNAL QUALIFICATION | Audit Windows sharing/locking semantics. |
| POSIX process signals | SERVICE WRAPPER REQUIRED | Use Windows Service Control Manager semantics. |
| Backup/restore CLI/paths | CODE CHANGE + STORAGE/PATH CHANGE | Package Windows-supported tools and protected paths. |
| Upgrade/rollback | INSTALLER REQUIRED + SERVICE WRAPPER REQUIRED | Coordinate service stop, backup/schema preflight and rollback. |
| Uninstall/purge | INSTALLER REQUIRED | Preserve recordings/config/database/secrets by default. |
| Windows 11 64-bit | EXTERNAL QUALIFICATION | First Windows field-test server target after Ubuntu acceptance. |
| Windows Server 2022 | EXTERNAL QUALIFICATION | Qualify after Windows 11. |
| Windows Server 2025 | EXTERNAL QUALIFICATION | Qualify after Server 2022. |
| Windows 10 compatibility | EXTERNAL QUALIFICATION | Validate after required Windows targets. |
| Native Windows desktop client | CODE CHANGE + INSTALLER REQUIRED + EXTERNAL QUALIFICATION | Separate milestone after server baseline. |

No item above authorizes Windows support or Production Qualified status.
