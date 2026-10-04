# Native Windows Desktop Client Foundation

**Milestone:** issue #6 / PR #7  
**Product status:** Release Candidate / External Qualification Pending.

## Implemented foundation

- .NET 10 LTS WPF x64 native application shell.
- Single-instance application lifecycle and bounded crash-safe logging.
- Client-local VMS server profiles with hostname/IP, scheme and port validation.
- Remote HTTPS requirement; localhost/loopback HTTP field-test exception only.
- Existing VMS bearer-session validation, logout and 401/expired-session handling.
- Optional token persistence through Windows Credential Manager.
- Existing `/api/v1/system/capabilities` negotiation.
- Existing authorized `/api/v1/cameras` camera inventory grouped by server-returned site.
- One active secured live view through existing live grant → MediaMTX/WHEP.
- MAIN/SUB/THIRD role values are consumed from the server response; the desktop client does not invent availability.
- Stop/switch/logout/window-close media cleanup.
- Capability-aware Events visibility.
- Redacted diagnostics and bounded rotating desktop logs.
- Self-contained versioned win-x64 field-test ZIP + SHA-256.
- Per-user install/uninstall scripts isolated from server services/configuration/recordings.

Playback and full event workstation features are intentionally not implemented in this foundation.

## Server profiles

Profiles contain display name, scheme, host and port only. Credentials are not accepted in server URLs. Remote plaintext HTTP is rejected. Malformed/unsafe addresses are rejected before HTTP calls.

## Authentication

The desktop foundation does not create a new identity store or login bypass. It accepts an existing VMS access token and validates it through the current server authority. Optional remembered session material is stored in Windows Credential Manager, never profiles JSON.

System-browser OIDC Authorization Code + PKCE S256 is implemented as generic software evidence in issue #8 / PR #9. No named IdP or external Windows qualification is claimed.

## Live media

The client requests:

`POST /api/v1/live/cameras/{camera_id}/access?stream_role=...`

The server performs camera tenant/site authorization and returns a short-lived path-scoped grant. The desktop renderer sends that grant in WHEP Authorization headers; camera RTSP credentials never enter the desktop client.

This milestone implements and tests one active live tile. It does not claim 4/9/16/64-camera performance. The renderer and session controller are camera-independent so later grid and native/hardware-decoding work can extend them.

## Diagnostics / logs

Safe diagnostics include application version, OS, architecture, configured safe server address, connection state, server deployment profile, client log location and media-session state.

Passwords, access tokens, refresh tokens, Authorization headers, camera credentials and private keys are excluded/redacted.

## Packaging / uninstall

`clients/windows/packaging/Publish-Client.ps1` produces a self-contained win-x64 field-test ZIP and SHA-256.

Default uninstall removes only the per-user client binary/shortcut. Client preferences remain for upgrade/reinstall continuity. Optional client-data purge requires explicit confirmation.

The client installer/uninstaller does not manage IntelligentVMSControl, IntelligentVMSMedia, PostgreSQL, server ProgramData or recordings.

## External qualification still required

- Windows 11 x64 physical/VM qualification.
- Windows 10 x64 physical/VM qualification and lifecycle caveat review.
- Real VMS interactive authentication deployment.
- Real WHEP playback against representative cameras/codecs.
- Certificate/private-CA workflows.
- DPI, multi-monitor and control-room usability.
- Disconnect/soak behavior on real desktop environments.
- Native/GPU decoder path and multi-camera capacity.
- Final signed Server / Client / Both commercial installer and update channel.

## Organization sign-in (OIDC/PKCE)

The desktop negotiates authentication policy from `GET /api/v1/auth/capabilities`. It only shows organization sign-in when the server advertises a compatible public-client contract.

Organization sign-in opens the **system default browser**. The IdP sign-in page is never hosted in the media WebView2 control. The native flow uses Authorization Code + PKCE S256 and a temporary `127.0.0.1` callback.

Authentication success is not treated as authorization by itself. The returned access token is validated again through the existing VMS session endpoint before server capabilities or authorized cameras are loaded.

Remember Me is explicit:
- OFF: OIDC access/session material is transient.
- ON: when the IdP issues a refresh token, only that minimum renewal credential is stored in Windows Credential Manager under a server-profile-specific OIDC target. If a saved profile's server origin changes, the old remembered credential identity is deleted and a new profile identity is created before authentication can be restored.
- Access tokens remain memory-resident.

A 401 can trigger one serialized renewal and one retry. A second 401 expires the VMS desktop session. Permanent refresh rejection removes remembered material; transient network/IdP failure preserves it but does not claim the user is currently authenticated.

Logout is local desktop logout in this milestone: active media is stopped, in-memory tokens are cleared and protected desktop session material is deleted. Remote/global IdP SSO logout is not claimed.

No named IdP, Windows 10/11, MFA, conditional-access, proxy or private-CA qualification is claimed.
## Native multi-camera live grid foundation

Issue #10 / PR #11 adds a native WPF live-grid workspace with deterministic 1/4/9/16 view layouts. This extends the existing one-tile secure live path without changing the VMS server/media authority.

Implemented behavior:

- one centralized LiveGridCoordinator;
- sixteen independent logical tile slots;
- camera-to-selected-tile assignment and camera double-click assignment;
- accidental duplicate-camera prevention;
- per-tile explicit Empty/Loading/Connecting/Live/Offline/Unauthorized/Failed/Stopping state;
- independent per-tile live-grant/WHEP lifecycle;
- generation/cancellation fencing for rapid camera/role/layout changes;
- grid SUB preference when advertised;
- 1-view/focus MAIN preference when advertised;
- safe fallback only to server-advertised MAIN/SUB/THIRD roles;
- in-window focus mode with non-focused sessions stopped;
- layout shrink cleanup;
- logout/profile-switch/session-expiry/application-close grid teardown;
- client-local safe layout persistence;
- redacted layout/tile diagnostics.

The persistence file contains profile ID, fixed layout, selected tile, and camera IDs only. It never contains access tokens, refresh tokens, media grants, WHEP resource URLs, Authorization headers, RTSP URLs, or camera credentials. Layout assignments are restored only after authentication and the authorized camera inventory are validated; live media does not auto-start after process restart.

Duplicate cameras are intentionally prevented in this foundation to avoid accidental duplicate WHEP readers. Automatic reconnect is intentionally not implemented; failed tiles remain operator-driven to avoid retry storms.

Current WebView2 remains a replaceable media-only renderer. One renderer per tile is acceptable for software-foundation evidence, but 16-view architecture is not a measured performance claim. CPU/RAM/GPU, real-camera codecs, real 1/4/9/16 stream smoothness, DPI/multi-monitor, low-bandwidth/packet-loss, and soak qualification remain external.

## Windows Desktop Playback Foundation — issue #12 / PR #13 — 2026-10-04

- Verified starting main: `8ca6f0a2ba6a4fa2a8da1c27750bffc64811e60a`.
- Dedicated branch: `feature/windows-desktop-playback-foundation`.
- Reuses existing authorized recording timeline, MP4 playback and bounded clip-export endpoints; no server/schema/recorder/playback-backend change was required.
- Native WPF playback workspace supports one authorized camera, selected-date availability, gap-aware timeline, play/pause/resume/seek/stop, renderer-derived position and same-span clip selection/export.
- Internally uses offset-aware UTC query/media timestamps; operator display uses the Windows local timezone because a site-timezone contract is not currently exposed. DST-forward/back day-length tests are deterministic.
- Seek into a true recording gap does not fabricate content or silently jump; it reports no recording at the requested time.
- Only 1× playback is advertised. F04-005 fast/slow remains TARGET/NV.
- F03-007 recording calendar remains TARGET/NV because the foundation has selected-date availability rather than month/day calendar marking.
- F04-001 remains TARGET/NV as a whole because synchronized/multi-camera playback is not implemented; the single-camera subset is evidenced by existing focused playback rows.
- Playback media uses a restricted replaceable WebView2 renderer. The media URL contains no bearer credential; native request interception injects Authorization only for the active VMS origin/playback path.
- Live sessions stop before playback use; playback stops when leaving the workspace. Logout, auth expiry, profile/server switch and app shutdown tear down playback context.
- Existing server export remains authoritative for authorization, continuous coverage, node boundary and limits; desktop never browses recording folders.
- Product remains **Release Candidate / External Qualification Pending**. Windows 10/11, codec, 4K, high-speed, GPU, storage and long-duration playback qualification remain external.

