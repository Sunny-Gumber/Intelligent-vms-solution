# ADR — Native Windows Desktop Client Foundation

**Status:** Accepted for PR #7 exact-head qualification.  
**Product status:** Release Candidate / External Qualification Pending.

## Decision

Use **.NET 10 LTS + WPF, x64** for the native Windows desktop shell.

The repository had no prior WPF/WinUI/WebView2 desktop decision. The milestone evaluated .NET 8, WPF, WinUI 3 and WebView2. .NET 10 LTS was selected instead of starting a new long-lived client on .NET 8 near the end of its support lifecycle. WPF was selected over WinUI 3 for the foundation because it provides a mature Windows desktop model, deterministic CI/build behavior and does not force the product into a final MSIX/Windows App SDK packaging decision before the Server/Client/Both installer architecture is proven.

Electron and Python GUI approaches are rejected.

## Native / WebView2 boundary

The application shell, navigation, profiles, API/auth session, capability negotiation, camera tree, settings, logging and diagnostics are native .NET/WPF.

WebView2 is used **only** behind the replaceable `ILiveMediaRenderer` boundary for the first WHEP/WebRTC renderer. It is not the application shell and it is not a second VMS.

The embedded renderer:
- loads only packaged local code through `https://app.intelligentvms.local/`;
- blocks external navigation and new windows;
- denies browser permissions;
- exposes no host objects;
- receives the short-lived media grant through an in-memory web message;
- never receives a bearer token in a URL;
- uses the existing WHEP Authorization header path;
- accepts WHEP session Location only from the same media origin;
- closes the peer connection and issues WHEP DELETE on stop/switch/unload.

This boundary allows a future native/GPU renderer to replace WebView2 without changing server authorization, camera selection or live-session lifecycle.

## Client/server boundary

Desktop Client → authenticated HTTPS FastAPI API → authorized short-lived live grant → existing MediaMTX/WHEP.

The desktop client does not:
- connect directly to PostgreSQL;
- create a recorder;
- store camera RTSP credentials;
- become camera/config authority;
- bypass tenant/site authorization;
- bypass the live-view grant system.

Remote server profiles require HTTPS. HTTP is allowed only for localhost/loopback field-test use. Normal .NET/Windows certificate validation and revocation checking remain enabled; no accept-any-certificate callback exists.

## Authentication and protected storage

The current server exposes bearer/JWT authentication and browser-session exchange, but no desktop username/password authority. This foundation accepts an existing VMS access token and validates it with `/api/v1/auth/session`. It does not invent a desktop bypass.

Optional remember-login stores the token in **Windows Credential Manager** as a Generic Credential. Profiles JSON contains no token/password. A 401 expires the desktop session and deletes the persisted token.

Interactive OIDC Authorization Code + PKCE is a later authentication UX milestone once the product defines desktop client registration and redirect contracts.

## Storage boundary

Per-user state:
- profiles: `%LocalAppData%\IntelligentVMS\Client\config`;
- bounded logs: `%LocalAppData%\IntelligentVMS\Client\logs`;
- cache/WebView2 data: `%LocalAppData%\IntelligentVMS\Client\cache`.

Client binaries use `%LocalAppData%\Programs\IntelligentVMS\Client`.

The client does not own server ProgramData, recordings, PostgreSQL or Windows server services.

## Compatibility boundary

The server currently lacks a formal desktop API-version compatibility endpoint. The safest current contract is successful authenticated parsing of `/api/v1/system/capabilities` with a non-empty `deployment_profile`. Missing/malformed capability data fails as incompatible. No broader API compatibility claim is made.

## Qualification boundary

GitHub hosted Windows Server runners establish deterministic build, unit/security/package and process-smoke evidence only. They do **not** qualify Windows 10/11 hardware, GPU/media decoding, DPI/multi-monitor behavior, camera/vendor/codec matrices or live-grid capacity.
