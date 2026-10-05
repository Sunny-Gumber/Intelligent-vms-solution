# Windows unified field-test installer — security review checklist

Issue #19.

Required exact-head review:
- machine-wide elevation is explicit and bounded to setup;
- setup/client binaries live under Program Files; accepted server executable/runtime remains under protected ProgramData;
- only the bounded preflight script runs from NSIS private per-run `$PLUGINSDIR`; installed/runtime executables never run from user-writable locations;
- existing ProgramData ACL hardening remains authoritative;
- secrets are generated/stored by accepted server logic and never printed by installer;
- installer logs redact secret/token/password/credential-like assignments;
- no VMS secret is passed on setup command lines; PostgreSQL bootstrap credential is inherited only through the child-process environment and is cleared immediately after preflight failure or setup completion;
- recording paths reject UNC, drive roots and overlap with Program Files/ProgramData;
- service image paths remain quoted and dependencies remain PostgreSQL → Control → Media;
- no broad firewall rule and no public PostgreSQL exposure;
- WebView2 is detected, not replaced by an unverified browser payload;
- NSIS compiler is pinned to 3.13.0 in CI;
- downgrade is blocked;
- global mutex blocks concurrent setup operations;
- ordinary uninstall preserves recordings/config/secrets/backups/database/client user state;
- rollback claims remain limited where DB migration is not safely reversible;
- package is unsigned and does not claim trusted publisher status.

Windows 10/11, security-product and corporate-policy compatibility remain External Qualification Pending.


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
