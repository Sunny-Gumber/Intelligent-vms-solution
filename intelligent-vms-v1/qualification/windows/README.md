# Windows external qualification harness

Issue #21. Layer A is software/readiness evidence only. It does not qualify Windows 10/11, any camera/vendor/model, capacity, or production signing.

Allowed states: NOT_RUN, BLOCKED_EXTERNAL, PASS, FAIL, PASS_WITH_LIMITATION, NOT_APPLICABLE.

Evidence sources: HOSTED_CI, VM_EXTERNAL, PHYSICAL_MACHINE, REAL_CAMERA, SIMULATED.

The validator rejects PASS/PASS_WITH_LIMITATION for external-only Windows 10/11, real-camera/PTZ/event, real soak/capacity/performance and real-storage test IDs when source is HOSTED_CI or SIMULATED.

Every finalized run is bound to a VMS-WIN-YYYYMMDD-### run ID, exact Git SHA, installer filename/SHA-256, product/server/client versions and DB schema.

Commands use qualification/windows/scripts/qualify.py: new-run, validate, summarize, release-manifest, redact, verify-signature and performance-template.

## Operator note — verify-signature (VMS-FIX-045, VMS-FIX-122)

Boss default pending signing ADR owner confirmation.

`qualify.py verify-signature` prints one JSON object `{"status","detail"}`. Exit 0 statuses are exactly SIGNED_VALID, UNSIGNED_EXPECTED, and NOT_RUN. Every other status is `FAIL` and the process exits 1.

- SIGNED_VALID exits 0 with or without --expect-unsigned when Get-AuthenticodeSignature Status is Valid (numeric 0, "0", or Valid). `Valid` is never treated as unsigned.
- UNSIGNED_EXPECTED exits 0 only with --expect-unsigned, and only for a regular file of length 0, a well-formed (0, 0) security directory, or NotSigned.
- NOT_RUN exits 0 with or without --expect-unsigned when the certificate table is in-file and os.name is not nt. Detail: Authenticode cryptographic verification requires Windows. On Linux this is the result. It is not an unsigned pass.

`UNSIGNED_EXPECTED` under `--expect-unsigned` is only:

- a regular file (`stat.S_ISREG`) of length 0, or a symlink that resolves to one (the field-test `unsigned.bin` placeholder), detail `PE has no Authenticode certificate table`; or
- a well-formed PE32/PE32+ whose security directory is exactly `(0, 0)`, same detail; or
- an in-file certificate table whose private-copy status is `NotSigned` (`2`, `"2"`, or `"NotSigned"`).

`/dev/null`, `/dev/zero`, FIFOs, devices, sockets, and directories are not the empty placeholder.

Reason codes for exit 1:

- `NOT_A_PE_FILE` — non-empty text, ZIP, MSI, scripts, or any file that does not start with `MZ`. No caller in this repo passes those to `--expect-unsigned`. Do not treat them as unsigned.
- `PE_SECURITY_DIR_OUT_OF_RANGE` — security directory is non-zero but the certificate is past EOF, or the offset and size overflow.
- `PE_MALFORMED:<reason>` — truncated or inconsistent DOS/PE/optional header, unknown magic, misaligned table, a directory that is neither `(0, 0)` nor an in-file certificate, or `header_exceeds_parse_window` when header fields would exceed 1 MiB (1048576 bytes).
- `SIGNATURE_STATUS_REJECTED:<Name>`, `SIGNATURE_STATUS_UNPARSED`, `SIGNATURE_STATUS_EMPTY`, `SIGNATURE_STATUS_MISSING`, `SIGNATURE_STATUS_AMBIGUOUS` — certificate table was readable and the status is not an accepted `NotSigned`.
- `ARTIFACT_UNREADABLE:<errno-name>` — missing path (`ENOENT`), dangling symlink (`ENOENT`), permission denied (`EACCES`), symlink loop (`ELOOP`), or directory (`EISDIR`). No traceback.
- `ARTIFACT_NOT_REGULAR_FILE` — the opened file is not a regular file. A zero `st_size` on a device or FIFO does not count.
- `ARTIFACT_TOO_LARGE` — `st_size` is above 512 MiB (536870912 bytes). The file is not read.
- `ARTIFACT_CHANGED` — the private copy PowerShell verified does not match the classified bytes. The copy is streamed in 1 MiB (1048576 bytes) chunks, hashed before and after the cmdlet, and rejected on a mismatch.

`UnknownError` exits 1. A 415-byte PE whose directory says the certificate ends at byte 416 is out of range, not unsigned. The same file at 416 bytes is checked as a signature. Without `--expect-unsigned`, a zero-length regular file and a `(0, 0)` directory are `FAIL`; `SIGNED_VALID` and Linux `NOT_RUN` still exit 0. A zero exit is not a signed release and does not check publisher or timestamp. Classification reads header fields only and does not load the certificate blob.

No harness command reboots, kills services, changes networking, fills disks, uninstalls the product, deletes recordings, or uploads evidence. Disruptive scenarios remain explicit tester actions in an authorized lab.
