# Windows external qualification harness

Issue #21. Layer A is software/readiness evidence only. It does not qualify Windows 10/11, any camera/vendor/model, capacity, or production signing.

Allowed states: NOT_RUN, BLOCKED_EXTERNAL, PASS, FAIL, PASS_WITH_LIMITATION, NOT_APPLICABLE.

Evidence sources: HOSTED_CI, VM_EXTERNAL, PHYSICAL_MACHINE, REAL_CAMERA, SIMULATED.

The validator rejects PASS/PASS_WITH_LIMITATION for external-only Windows 10/11, real-camera/PTZ/event, real soak/capacity/performance and real-storage test IDs when source is HOSTED_CI or SIMULATED.

Every finalized run is bound to a VMS-WIN-YYYYMMDD-### run ID, exact Git SHA, installer filename/SHA-256, product/server/client versions and DB schema.

Commands use qualification/windows/scripts/qualify.py: new-run, validate, summarize, release-manifest, redact, verify-signature and performance-template.

## Operator note — verify-signature --expect-unsigned (VMS-FIX-045)

Boss default pending signing ADR owner confirmation. Behaviour change from the FIX-027 status map: `--expect-unsigned` no longer treats `UnknownError` as unsigned.

- No Authenticode certificate table, including the empty field-test `unsigned.bin`: `UNSIGNED_EXPECTED`, exit 0. PowerShell is not called.
- Certificate table present and status `NotSigned` only (`2`, `"2"`, or `"NotSigned"`): `UNSIGNED_EXPECTED`, exit 0.
- `UnknownError` in every form, and every other status including unknown, unparsable, missing, or empty: `FAIL`, exit 1. The detail starts with `SIGNATURE_STATUS_REJECTED:<Name>`, `SIGNATURE_STATUS_UNPARSED`, `SIGNATURE_STATUS_EMPTY`, or `SIGNATURE_STATUS_MISSING`.
- Without `--expect-unsigned`, only `Valid` exits 0.

A zero exit on an expected-unsigned artifact is not a signed release and does not check publisher or timestamp.

No harness command reboots, kills services, changes networking, fills disks, uninstalls the product, deletes recordings, or uploads evidence. Disruptive scenarios remain explicit tester actions in an authorized lab.
