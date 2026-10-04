# Windows Desktop Client Foundation QA

Issue #6 / PR #7.

## Automated deterministic coverage

The Windows Client workflow validates:
- dependency restore;
- x64 native WPF compile with warnings as errors;
- profile validation including remote-HTTP rejection;
- profile persistence and corrupt-settings quarantine;
- Windows Credential Manager token round-trip;
- token/password/credential-URL redaction;
- authentication state transitions and logout;
- 401/session-expired behavior;
- capability parsing and unsupported-feature gating inputs;
- authorized camera-list parsing and site grouping;
- live-grant camera/role/expiry/TLS safety;
- live start/switch/stop renderer cleanup;
- diagnostics redaction;
- package ownership isolation;
- production source security scan;
- self-contained win-x64 package + SHA-256;
- executable `--smoke-test` process launch.

The existing Intelligent VMS CI, Security, Ubuntu 22.04/24.04 field-test and Windows Server 2022/2025 field-test workflows remain independent regression gates.

## Manual/external QA still required

Automated hosted-runner success is not Windows 10/11 qualification and does not prove real video rendering. External qualification must cover:
- fresh client install/start/configure/connect/authenticate;
- real capabilities and authorized cameras;
- real WHEP media start/stop/switch;
- session expiry during live view;
- server disconnect during media;
- app shutdown during active media;
- real certificate failures/private CA behavior;
- corrupted settings/missing credential entry;
- restart preserving safe profiles;
- uninstall preserving VMS server runtime and recordings;
- real Windows 10/11 DPI/multi-monitor/control-room behavior.

## OIDC/PKCE deterministic software evidence — issue #8 / PR #9

The Windows Client test suite adds:
- RFC 7636 S256 known-vector validation;
- fresh state/verifier/application nonce and prepared-request validation;
- HTTPS authorization endpoint and no-client-secret checks;
- wrong/missing state, replay, wrong path and malformed callback rejection;
- real 127.0.0.1 ephemeral HttpListener callback;
- callback timeout and cancellation;
- unauthenticated safe auth-capability parsing;
- one bounded 401 refresh/retry and repeated-401 failure;
- profile/purpose-isolated Credential Manager entries;
- Credential Manager size-bound failure;
- authorization-code/verifier/access/refresh/ID-token/header redaction;
- existing camera/live grant/WHEP cleanup regressions;
- .NET dependency vulnerability audit and production-source auth-boundary scan.

No external IdP credentials are required by normal CI. Real IdP/MFA/proxy/private-CA/Windows 10/11 qualification remains external.


## OIDC/PKCE milestone additions

Issue #8 / PR #9 adds deterministic coverage for RFC 7636 S256, fresh library state/verifier plus application nonce, safe auth capability negotiation, loopback callback target/state/replay/timeout/cancellation, invalid callback non-consumption, ambiguous response rejection, profile-origin credential isolation, protected OIDC refresh-token purpose separation, one bounded 401 refresh retry, secret redaction, and stale-attempt generation-fencing contracts. Real IdP/browser/Windows qualification remains external.
## Native multi-camera live-grid QA — issue #10 / PR #11

Deterministic Windows Client CI now validates the native grid state/session layer in addition to the existing OIDC, camera, live-grant, WHEP, TLS, diagnostics, packaging and smoke-launch contracts.

Covered software scenarios include:

- deterministic 1x1, 2x2, 3x3 and 4x4 layout definitions;
- 1, 4, 9 and 16 logical tile architecture;
- server-advertised role policy: SUB grid preference, MAIN 1-view/focus preference and safe fallback;
- duplicate-camera rejection;
- layout shrink teardown;
- focus enter/exit role transition and hidden-session stop;
- one tile failure while another remains live;
- rapid camera A -> B replacement with stale/cancelled A unable to replace B;
- 16 logical sessions using deterministic fake live-grant/render components;
- safe persisted layout without tokens/WHEP URLs/passwords;
- restore filtering for removed/unauthorized camera IDs;
- repeated assign/start/clear lifecycle;
- stop-all cleanup with no logical active sessions remaining.

The 16-session test is a state/lifecycle test only. It does not decode sixteen real streams and is not evidence for real workstation capacity.

Independent QA must still review the exact candidate head and the existing live API/WHEP contract. Real WebView2 media rendering against representative cameras/codecs remains External Qualification Pending.
