# External qualification runbook

## Prepare
1. Obtain the exact accepted candidate artifact without modifying it.
2. Generate a VMS-WIN-YYYYMMDD-### run ID.
3. Record Git SHA, installer version and SHA-256 before installation.
4. Create a pseudonymous machine ID and collect only fields defined by the machine schema.
5. Record Windows edition/version/build/servicing channel/lifecycle. For Windows 10, never generalize one edition/build into a blanket support statement.
6. Keep Defender/security tooling enabled and record its state.

## Execute
Follow the central matrix and scenario document. Run only rows supported by the actual lab. Every BLOCKED_EXTERNAL or NOT_APPLICABLE row requires a reason. Every FAIL records expected, actual, evidence, severity, issue and retest reference. Retests create a new result; do not erase the original FAIL.

## Guided real-camera path
Register camera -> verify online -> camera manifest -> MAIN/SUB/THIRD -> Live -> supported grid size -> record -> playback -> export -> PTZ if supported -> event if supported -> controlled reboot/recovery -> evidence bundle.

Never write camera credentials, bearer tokens, DB credentials or private keys into evidence.

## Evidence
Produce qualification-result.json, qualification-summary.md, performance.csv/json where applicable, sanitized diagnostics, machine manifest, camera manifests and release-manifest.json.

Evidence is immutable after finalization. Corrections create an amendment or a new run.

## Severity
BLOCKER: data loss, security bypass, recording destruction, destructive installer failure, unsafe PTZ motion, or service cannot operate.
CRITICAL: core live/record/playback unusable, recurring crash, major authorization failure.
MAJOR: important feature unreliable.
MINOR: low-impact UX defect.

Unresolved isolation failure, data loss, destructive uninstall, recording corruption, unauthorized access, credential leakage or unsafe PTZ STOP behavior blocks production qualification.

## Cleanup
Remove only explicitly temporary qualification files. Do not automatically delete recordings/evidence, purge databases, uninstall the product, reboot or reset networking.

## Submission
Run validator, summary and release-manifest generation, then independent review. Layer A CI is never a substitute for external-machine or real-camera evidence.

## Authenticode check before install or release evidence (VMS-FIX-045)

Boss default pending signing ADR owner confirmation.

Run `qualification/windows/scripts/qualify.py verify-signature` on the installer or release artifact before recording signing status.

Exit 0 and status `UNSIGNED_EXPECTED` under `--expect-unsigned` means only one of these:

- the artifact is a zero-length placeholder, such as the hosted field-test `unsigned.bin`; or
- the artifact is a well-formed PE32/PE32+ and the security directory is exactly `(0, 0)`; or
- the certificate table is inside the file and the status is only `NotSigned`.

Stop the run on exit 1. The detail is the reason code:

- `PE_SECURITY_DIR_OUT_OF_RANGE` — the security directory is non-zero but the certificate is not fully inside the file, including a one-byte truncation. This is not an unsigned field-test pass.
- `PE_MALFORMED:<reason>` — the DOS, PE, or optional header is truncated or inconsistent, the magic is unknown, or the certificate table is misaligned.
- `NOT_A_PE_FILE` — the file is non-empty and is not a PE. Text, ZIP, MSI, and scripts use this code. Do not pass them with `--expect-unsigned` and expect a pass.
- `SIGNATURE_STATUS_REJECTED:UnknownError` — invalid or unreadable signature. Numeric `1`, `"1"`, and `"UnknownError"` all use this code.
- `SIGNATURE_STATUS_REJECTED:<Name>`, `SIGNATURE_STATUS_UNPARSED`, `SIGNATURE_STATUS_EMPTY`, `SIGNATURE_STATUS_MISSING`, or `SIGNATURE_STATUS_AMBIGUOUS` — any other status, including a JSON object that repeats `Status`.

Omit `--expect-unsigned` for a build that must be signed. Only `Valid` exits 0. This check does not verify publisher or timestamp, and a zero exit is not production signing.
