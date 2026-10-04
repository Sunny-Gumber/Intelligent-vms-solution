# Windows unified field-test installer — security review checklist

Issue #19.

Required exact-head review:
- machine-wide elevation is explicit and bounded to setup;
- setup/client binaries live under Program Files; accepted server executable/runtime remains under protected ProgramData;
- no executable runs from user-writable TEMP;
- existing ProgramData ACL hardening remains authoritative;
- secrets are generated/stored by accepted server logic and never printed by installer;
- installer logs redact secret/token/password/credential-like assignments;
- no VMS secret is passed on setup command lines;
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
