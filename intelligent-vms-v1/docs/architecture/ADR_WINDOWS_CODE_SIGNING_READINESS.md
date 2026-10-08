# ADR - Windows production code-signing readiness

Status: readiness foundation only. Provider/certificate purchase and production signing require owner approval.

## Decision
Use Authenticode with trusted timestamping for future release artifacts. Candidate pipeline:

build unsigned -> validate/test -> hash -> trusted signing boundary -> sign -> verify publisher/signature/timestamp -> produce final post-signing hash -> publish.

Field-test builds remain UNSIGNED_EXPECTED; this is not equivalent to signed PASS.

## Provider-neutral options
- hardware-backed organization code-signing certificate
- managed cloud signing service
- organization-approved HSM/signing infrastructure

Selection depends on legal publisher identity, cost, CI integration, operational complexity, availability and reputation requirements. No commercial provider is hardcoded.

## Key custody
Private signing keys must never be committed, copied into source, printed in logs, stored as plaintext repository secrets or broadly distributed to developer workstations. Prefer managed/HSM custody with auditable least-privilege signing authorization.

## Rotation and revocation
Record certificate identity/serial/validity in release metadata, support planned rotation, and maintain an emergency revocation/incident process. Previously signed artifacts remain traceable by hash and timestamp.

## Verification
Release automation must verify Authenticode validity, expected publisher, timestamp and final file hash. Signing and SmartScreen reputation are separate; a valid signature does not guarantee established reputation.

The qualification harness maps `Get-AuthenticodeSignature` Status through Microsoft's SignatureStatus enum, accepting both the name and the numeric value (Valid 0, UnknownError 1, NotSigned 2, HashMismatch 3, NotTrusted 4, NotSupportedFileFormat 5, Incompatible 6; https://learn.microsoft.com/en-us/dotnet/api/system.management.automation.signaturestatus?view=powershellsdk-7.4.0). Values outside that enum fail closed. Expected publisher and timestamp policy remain owner decisions and are not applied by this status map.

### Expect-unsigned fail-closed (VMS-FIX-045, issue #105 item d)

Boss default pending signing ADR owner confirmation.

`qualify.py verify-signature --expect-unsigned` accepts only `NotSigned` (numeric `2`, string `"2"`, or `"NotSigned"`). `UnknownError` (numeric `1`, string `"1"`, or `"UnknownError"`) is an unreadable or invalid signature, not an unsigned file. Every other status, and any unknown, unparsable, missing, or empty status, exits non-zero. The printed status is `FAIL`. The detail starts with one reason code:

- `SIGNATURE_STATUS_REJECTED:<Name>` for a known status that is not accepted
- `SIGNATURE_STATUS_UNPARSED` for a value outside the enum
- `SIGNATURE_STATUS_EMPTY` for a blank status
- `SIGNATURE_STATUS_MISSING` when Status is absent or null

Without `--expect-unsigned`, only `Valid` exits 0. A PE with no certificate table stays `UNSIGNED_EXPECTED` when `--expect-unsigned` is set; that field-test path does not consult signature status and does not treat `UnknownError` as unsigned. Publisher and timestamp checks remain owner decisions (issue #105 item c). This map does not claim a real signed-artifact run.

## Current blockers
Owner decisions remain required for legal publisher identity and provider/certificate procurement. Current product remains Release Candidate / External Qualification Pending and is not production-signed.
