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
