# Ubuntu Intelligent VMS Field-Test Smoke Checklist

Record exact commit, Ubuntu version, browser, camera manufacturer/model/firmware and
network/storage setup. Automated checks prove deployment mechanics only. Items marked
MANUAL / EXTERNAL require real hardware or operator evidence.

1. Install / automated: generate .env and run vmsctl install; no secret value printed.
2. Start / automated: status and health pass.
3. Login: mint field token, login, verify tenant/site, sign out and in.
4. Add camera / MANUAL / EXTERNAL: add an allowed IP/RTSP camera or use existing ONVIF API.
5. Live / MANUAL / EXTERNAL: assign camera and obtain video.
6. MAIN/SUB/THIRD / MANUAL / EXTERNAL: test only profiles really exposed by camera.
7. Continuous recording / MANUAL / EXTERNAL: enable MAIN recording.
8. Retention: set and re-read a bounded retention period.
9. Recording / MANUAL / EXTERNAL: record real footage and note wall-clock interval.
10. Playback/timeline / MANUAL / EXTERNAL: verify real spans and real gaps.
11. Snapshot / MANUAL / EXTERNAL: download/verify active-tile PNG.
12. Manual recording / MANUAL / EXTERNAL: Start, Stop and verify recording-backed interval.
13. Clip / MANUAL / EXTERNAL: download MP4 within one recorded span.
14. Restart: run vmsctl restart; health, camera config and recording path persist.
15. Post-restart video / MANUAL / EXTERNAL: verify live/playback again.
16. Host reboot / MANUAL / EXTERNAL: verify Docker/services/health/login/config/storage return; record any real recording gap.
17. Diagnostics: collect archive and verify secrets/camera credentials are absent.
18. Backup: verify PostgreSQL dump/checksum and explicit media/ClickHouse/secrets exclusions.
19. Restore drill: on an approved disposable field machine use CONFIRM_RESTORE=YES and verify health.
20. Result: PASS, FAIL or NOT TESTED. Never convert container automation into Ubuntu Production Qualified or camera qualification.
