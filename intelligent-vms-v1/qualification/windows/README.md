# Windows external qualification harness

Issue #21. Layer A is software/readiness evidence only. It does not qualify Windows 10/11, any camera/vendor/model, capacity, or production signing.

Allowed states: NOT_RUN, BLOCKED_EXTERNAL, PASS, FAIL, PASS_WITH_LIMITATION, NOT_APPLICABLE.

Evidence sources: HOSTED_CI, VM_EXTERNAL, PHYSICAL_MACHINE, REAL_CAMERA, SIMULATED.

The validator rejects PASS/PASS_WITH_LIMITATION for external-only Windows 10/11, real-camera/PTZ/event, real soak/capacity/performance and real-storage test IDs when source is HOSTED_CI or SIMULATED.

Every finalized run is bound to a VMS-WIN-YYYYMMDD-### run ID, exact Git SHA, installer filename/SHA-256, product/server/client versions and DB schema.

Commands use qualification/windows/scripts/qualify.py: new-run, validate, summarize, release-manifest, redact, verify-signature and performance-template.

## Operator note — verify-signature --expect-unsigned (VMS-FIX-045)

Boss default pending signing ADR owner confirmation.

Exit 0 with `--expect-unsigned` happens only when:

- the file length is 0 (the field-test `unsigned.bin` placeholder), status `UNSIGNED_EXPECTED`; or
- the file is a well-formed PE32/PE32+ whose security directory is exactly `(0, 0)`, status `UNSIGNED_EXPECTED`; or
- an in-file certificate table reports `NotSigned` (`2`, `"2"`, or `"NotSigned"`), status `UNSIGNED_EXPECTED`.

Anything else exits 1 with status `FAIL`. Reason codes:

- `NOT_A_PE_FILE` — non-empty text, ZIP, MSI, scripts, or any file that does not start with `MZ`. No caller in this repo passes those to `--expect-unsigned`. Do not treat them as unsigned.
- `PE_SECURITY_DIR_OUT_OF_RANGE` — security directory is non-zero but the certificate is past EOF, or the offset and size overflow.
- `PE_MALFORMED:<reason>` — truncated or inconsistent DOS/PE/optional header, unknown magic, misaligned table, or a directory that is neither `(0, 0)` nor an in-file certificate.
- `SIGNATURE_STATUS_REJECTED:<Name>`, `SIGNATURE_STATUS_UNPARSED`, `SIGNATURE_STATUS_EMPTY`, `SIGNATURE_STATUS_MISSING`, `SIGNATURE_STATUS_AMBIGUOUS` — certificate table was readable and the status is not an accepted `NotSigned`.

`UnknownError` exits 1. A 415-byte PE whose directory says the certificate ends at byte 416 is out of range, not unsigned. The same file at 416 bytes is checked as a signature. Without `--expect-unsigned`, only `Valid` exits 0. A zero exit is not a signed release and does not check publisher or timestamp.

No harness command reboots, kills services, changes networking, fills disks, uninstalls the product, deletes recordings, or uploads evidence. Disruptive scenarios remain explicit tester actions in an authorized lab.
