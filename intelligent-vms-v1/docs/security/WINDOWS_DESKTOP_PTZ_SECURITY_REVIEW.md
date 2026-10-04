# Windows Desktop PTZ Foundation — Independent Security Review

Issue #14 / PR #15

## Review checklist

- Desktop receives no camera credentials or direct ONVIF/RTSP endpoint.
- Every PTZ route uses admin/operator authorization and `authorized_camera`.
- Tenant/site authorization and site-network target pinning remain authoritative.
- Finite normalized vectors only; zero moves rejected; generation bounded; context ID is UUID.
- Principal+camera+client-context generation fencing rejects stale commands.
- STOP bypasses movement rate limiting and can preempt a slow MOVE without a network-held lock.
- Superseded late MOVE completion triggers compensating STOP.
- Normal UI issues one MOVE per press and one STOP per release; server also bounds movement rate.
- Existing single refresh/single retry auth behavior is reused.
- PTZ coordinator is disposed before server-profile authority changes.
- Logout/shutdown attempt bounded STOP before authenticated state is discarded where possible.
- PTZ failures do not mutate recording/live policy.
- Preset injection is not applicable because presets are intentionally absent.
- Errors and diagnostics exclude raw SOAP, camera credentials, authorization tokens and credential-bearing URLs.

## Residual risks

Physical camera behavior, vendor-specific velocity interpretation, real stop latency, network-loss behavior and firmware interoperability are external qualification. Product remains Release Candidate / External Qualification Pending.