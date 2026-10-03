# Windows VMS Server Field-Test Smoke Checklist

Record exact repository commit, Windows edition/build, CPU architecture, PostgreSQL
version, browser, camera manufacturer/model/firmware, storage filesystem and network.

Automated CI evidence is not physical Windows/camera/capacity qualification.

1. Install: elevated PowerShell, choose an absolute local recording volume, run native installer; confirm no Docker/WSL dependency.
2. Services: PostgreSQL, IntelligentVMSControl and IntelligentVMSMedia are registered and Automatic.
3. Health: VMS readiness and media_node are OK.
4. Browser/login: open local `http://127.0.0.1:8000`; mint field token; authenticate; verify session/CSRF flow.
5. Camera / MANUAL EXTERNAL: add an allowed camera; verify credentials are absent from URL/logs.
6. Live / MANUAL EXTERNAL: obtain actual video.
7. MAIN/SUB/THIRD / MANUAL EXTERNAL: test only configured camera profiles.
8. Recording / MANUAL EXTERNAL: enable continuous MAIN; verify actual files are created on selected Windows volume.
9. Retention: configure/re-read bounded retention; externally observe deletion semantics over a practical period.
10. Playback/timeline / MANUAL EXTERNAL: verify actual recorded spans and gaps.
11. Snapshot / MANUAL EXTERNAL: download active-tile PNG.
12. Manual recording / MANUAL EXTERNAL: Start/Stop durable interval and export it.
13. Clip / MANUAL EXTERNAL: download MP4 inside one continuous recorded span.
14. Service restart: restart VMS services; camera config, database and recording directory persist.
15. Post-restart / MANUAL EXTERNAL: verify live/playback and measure any real recording gap.
16. Windows reboot / MANUAL EXTERNAL: reboot; verify automatic services, health, login/config/storage return; record actual recording continuity.
17. Diagnostics: collect ZIP; verify camera/database credentials, bearer values, tokens and private keys are absent.
18. Backup: create PostgreSQL dump/checksum; confirm recording media is explicitly excluded.
19. Restore drill: on approved disposable test state, restore with explicit confirmation and verify health/config.
20. Uninstall: remove VMS services; verify database, recordings, config/secrets and backups remain.
21. Purge guard: prove database purge refuses without exact confirmation; recordings remain untouched.
22. Result: mark each item PASS / FAIL / NOT TESTED. Never convert Server CI into Windows 11, Windows 10, physical reboot, real camera or Production Qualified evidence.
