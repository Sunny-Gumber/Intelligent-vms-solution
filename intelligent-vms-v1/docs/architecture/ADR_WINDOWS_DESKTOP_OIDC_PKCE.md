# ADR — Windows Desktop OIDC Authorization Code + PKCE

**Status:** Proposed for issue #8 / PR #9 exact-head acceptance.  
**Product status:** Release Candidate / External Qualification Pending.

## Decision

Use Duende.IdentityModel.OidcClient 7.1.0 for the native Windows public-client OIDC protocol, with the system default browser, Authorization Code, PKCE S256, and a short-lived loopback callback bound only to `127.0.0.1` on an ephemeral port.

The existing Intelligent VMS API remains the authorization authority. The configured IdP issues tokens; after OIDC completes, the desktop validates the access token against the existing VMS `/api/v1/auth/session` endpoint before loading authorized capabilities/cameras. No second user database, JWT issuer, role model, or desktop auth bypass is introduced.

## Server metadata

`GET /api/v1/auth/capabilities` exposes only non-secret authentication policy/public-client metadata: required modes, public authority, public client ID, scopes, loopback callback type and S256 support. No client secret, token, credential or JWKS internals are returned.

Desktop OIDC is disabled unless explicitly configured. Production OIDC posture rejects an empty public client ID, missing `openid` scope and control characters in scope configuration.

## Request/callback security

Each attempt uses fresh library state and PKCE verifier plus a fresh application nonce. The client verifies the prepared request uses HTTPS, response_type=code, the current redirect URI, exact state/nonce, S256 and the current SHA-256 challenge, and contains no client secret.

The callback is only `http://127.0.0.1:<ephemeral>/oidc/callback/`. It never binds to LAN/0.0.0.0, is single-use, rejects wrong/missing state, unexpected path and malformed responses, and closes on success, error, cancellation, timeout, profile switch or app shutdown.

Duende performs discovery, code redemption and token validation. The application additionally binds the validated identity response to its fresh nonce.

## Token lifecycle

Access tokens remain memory-resident. With explicit Remember Me and an IdP-issued refresh token, only the refresh token is stored in Windows Credential Manager under `IntelligentVMS.Desktop/<profile-id>/oidc-refresh`. Manual-token storage uses a different target.

Refresh is serialized. One protected API 401 can cause at most one refresh and one retry. Repeated 401 expires the session. Permanent refresh rejection deletes persisted refresh material; transient network/discovery failure preserves it while reporting an unauthenticated/offline-restorable state.

Logout is local desktop logout for this milestone. It clears desktop access/refresh state and stops sensitive UI/media but does not claim global IdP/browser logout.

## Browser boundary

IdP login is never hosted in WebView2. WebView2 remains media-only.

## Qualification boundary

F25-003/F25-004 remain TARGET/NV because their wording includes broader AD/LDAP/SAML/MFA/certificate/Windows/local-auth behavior. No named IdP, Windows 10/11, MFA, proxy, private-CA or enterprise policy qualification is claimed.
