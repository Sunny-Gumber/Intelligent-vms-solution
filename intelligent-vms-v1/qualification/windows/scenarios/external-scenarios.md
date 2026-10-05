# External qualification scenarios

All disruptive scenarios are opt-in and require an authorized non-production lab.

## Installer
Fresh Client/Server/Both; UAC; Start Menu; WebView2; PostgreSQL prerequisite; service registration; reboot/startup; repair; accepted-baseline upgrade; uninstall/reinstall; preservation of recordings/config/client LocalAppData/Credential Manager. Capture SmartScreen exactly; never disable Defender to make a test pass.

## Client and auth
Launch, resize, minimize/restore, clean shutdown, 100/125/150/200% DPI where available, multi-monitor where available, sleep/wake. Validate system-browser OIDC callback, Remember Me, logout, expiry, profile switching and login network interruption. Named Entra/Keycloak/Okta rows stay NOT_RUN until tested.

## Real camera, codecs and grids
Record exact camera model/firmware/ONVIF profiles. Exercise MAIN/SUB/THIRD where available, H.264 and H.265 separately, and only resolutions actually produced by hardware. Grid qualification requires the actual simultaneous real-stream count.

## PTZ
Up/Down/Left/Right, Zoom In/Out, STOP, repeated moves, network loss while moving, app close, active-tile switch and logout. Unexpected continued PTZ motion after STOP/lifecycle teardown is a BLOCKER for that device/profile.

## Playback, export and events
Use real recordings with multiple spans and real gaps. Test play/pause/resume/seek/gap/end/retention/logout/close. Export must match camera and approximate requested interval and open in an approved external player. Hardware events test only what the device actually emits; measure source-to-visible latency when timestamps permit.

## Recording continuity and recovery
For every disruption record start/end, last segment before failure, first segment after recovery and measured gap. Never report continuous without timeline measurement. Controlled scenarios include Control termination, Media termination, Windows reboot, camera reboot and server-camera link interruption. PostgreSQL kill is excluded unless a separate DB-recovery procedure is approved.

## Network
Latency/loss/throttle/disconnect only on an authorized test interface or isolated lab. Record parameters. Never alter corporate/production networking.

## Storage
Record filesystem and disk class. Local SSD/HDD only when actually tested. NAS/RAID remain NOT_RUN until hardware exists. Low-disk uses a dedicated test volume with a mandatory reserve; never fill the OS partition to 100%.

## Soak and performance
Tiers: SMOKE 30-60m; SHORT about 2h; EXTENDED about 8h; QUALIFICATION CANDIDATE 24h; LONG 48-72h+ when later required. Capture uptime, service restarts, CPU/RAM/handles, disk growth, recording gaps, reconnects, errors and crashes with bounded logs. If no approved thresholds exist, label values MEASURED rather than inventing PASS criteria.
