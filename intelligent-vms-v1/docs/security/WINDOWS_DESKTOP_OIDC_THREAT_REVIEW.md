# Windows Desktop OIDC / PKCE Threat Review

**Milestone:** issue #8 / PR #9  
**Boundary:** generic native OIDC public-client software evidence only.

| Threat | Mitigation | Residual |
| --- | --- | --- |
| Authorization-code interception | Authorization Code + PKCE S256 | Host/process compromise external |
| PKCE downgrade | S256 required and exact challenge verified; plain rejected | IdP interoperability external |
| State CSRF | Fresh state and exact callback comparison | Browser/OS compromise external |
| Nonce/replay | Fresh nonce and validated identity-claim binding | Named IdP behavior external |
| Callback hijacking | Exact 127.0.0.1 ephemeral callback and route | Privileged local malware |
| Listener exposure | No 0.0.0.0/LAN bind; bounded lifetime | Host HTTP.sys/firewall policy |
| Browser URL leakage | Only protocol-required auth request; no full auth URL logging | Browser history/provider policy |
| Token persistence | Access token memory-only; optional refresh in Credential Manager | Windows account compromise |
| Refresh-token theft | Profile/purpose isolated protected target | Windows Credential Manager trust |
| Cross-profile confusion | Profile switch cancels auth/media and clears API/camera/capability state; changing scheme/host/port rotates the profile credential identity and deletes old remembered material | None known in software boundary |
| Logout semantics | Local access/refresh removal and media cleanup; no global logout claim | Browser SSO can remain |
| Secret logging | Code/verifier/access/refresh/ID tokens and auth headers redacted/excluded | OS dump memory |
| Crash-dump exposure | No plaintext token files | OS crash-dump policy external |
| Memory lifecycle | Active references cleared/cancelled on logout/profile/app close | Managed-memory zeroization not guaranteed |
| Credential-store isolation | Separate per-profile manual vs OIDC-refresh targets | Platform trust |
| TLS handling | Remote VMS/authority HTTPS; normal chain/hostname/revocation validation | Private CA deployment external |
| Malicious server profile | Existing target validation + safe advertised authority contract | Trusted malicious VMS can point to its configured IdP |
| Open redirects | Redirect URI generated locally and exact-match validated | IdP redirect registration external |
| Race conditions | Attempt-local cancellation ownership, generation fencing for login/restore/refresh, profile checks and serialized refresh | OS scheduling external |
| Stale callback | Invalid state/malformed callbacks do not consume the valid attempt; valid callback is atomically single-use; listener is disposed on completion/cancel/timeout | None known in software boundary |

## Result

No desktop client secret, password store, embedded IdP login, second identity authority or authorization bypass is introduced. Remaining named-IdP/MFA/conditional-access/proxy/private-CA/Windows qualification stays External Qualification Pending.
