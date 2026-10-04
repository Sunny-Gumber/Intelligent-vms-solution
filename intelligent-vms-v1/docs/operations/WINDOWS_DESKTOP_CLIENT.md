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
