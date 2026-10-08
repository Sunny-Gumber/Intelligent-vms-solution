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

`qualify.py verify-signature --expect-unsigned` exits 0 in only three cases:

- The file is empty (the documented field-test `unsigned.bin` placeholder). Status `UNSIGNED_EXPECTED`. Detail `PE has no Authenticode certificate table`. PowerShell is not started. No other non-PE file takes this path. MSI, ZIP, scripts, and text fail closed with `NOT_A_PE_FILE`. Nothing in this repository passes those files to `--expect-unsigned`.
- The file is a well-formed PE32 or PE32+ and its security data directory is exactly `(0, 0)`. Every offset needed to read that directory is inside the file, `SizeOfOptionalHeader` includes it, and `NumberOfRvaAndSizes` is at least 5. Status `UNSIGNED_EXPECTED`. Same detail. PowerShell is not started.
- The certificate blob is fully inside the file, 8-byte aligned, at least 8 bytes, and `Get-AuthenticodeSignature` Status is `NotSigned` (numeric `2`, string `"2"`, or `"NotSigned"`).

`UnknownError` (numeric `1`, string `"1"`, or `"UnknownError"`) is an invalid signature. It exits 1. Every other status, and any unknown, unparsable, missing, empty, or duplicate status, exits 1. The printed status is `FAIL`. The detail is one of:

- `SIGNATURE_STATUS_REJECTED:<Name>` for a known status that is not accepted
- `SIGNATURE_STATUS_UNPARSED` for a value outside the enum
- `SIGNATURE_STATUS_EMPTY` for a blank status
- `SIGNATURE_STATUS_MISSING` when Status is absent or null
- `SIGNATURE_STATUS_AMBIGUOUS` when a JSON object repeats a key
- `NOT_A_PE_FILE` for a non-empty file that does not start with `MZ`
- `PE_SECURITY_DIR_OUT_OF_RANGE` when a non-zero security directory is past EOF or its offset and size overflow
- `PE_MALFORMED:<reason>` for a truncated or inconsistent DOS, PE, or optional header, an unknown optional-header magic, a misaligned certificate table, or a security directory that is neither `(0, 0)` nor an in-file certificate

Without `--expect-unsigned`, only `Valid` exits 0. A one-byte-short certificate table is `PE_SECURITY_DIR_OUT_OF_RANGE`, not an unsigned file. Publisher and timestamp checks remain owner decisions (issue #105 item c). This map does not claim a real signed-artifact run.

## Current blockers
Owner decisions remain required for legal publisher identity and provider/certificate procurement. Current product remains Release Candidate / External Qualification Pending and is not production-signed.
