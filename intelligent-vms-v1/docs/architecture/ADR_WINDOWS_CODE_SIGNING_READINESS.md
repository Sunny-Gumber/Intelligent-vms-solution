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

### Expect-unsigned fail-closed (VMS-FIX-045, issue #105 item d) and input hardening (VMS-FIX-122)

Boss default pending signing ADR owner confirmation.

`qualify.py verify-signature` prints one JSON object `{"status","detail"}`. Exit 0 statuses are exactly SIGNED_VALID, UNSIGNED_EXPECTED, and NOT_RUN. Every other status is `FAIL` and the process exits 1.

- SIGNED_VALID exits 0 with or without --expect-unsigned when Get-AuthenticodeSignature Status is Valid (numeric 0, "0", or Valid). `Valid` is never `UNSIGNED_EXPECTED`. The detail is the cmdlet JSON. Publisher and timestamp are not checked.
- UNSIGNED_EXPECTED exits 0 only with --expect-unsigned, and only for a regular file of length 0, a well-formed (0, 0) security directory, or NotSigned. A symlink is followed; `stat.S_ISREG` is taken from the opened descriptor, so the target must be that regular file.
- NOT_RUN exits 0 with or without --expect-unsigned when the certificate table is in-file and os.name is not nt. Detail: Authenticode cryptographic verification requires Windows. On Linux this is the result. PowerShell is not started. The status is not `UNSIGNED_EXPECTED`.

The three `UNSIGNED_EXPECTED` cases are:

- The opened file is a regular file of length 0 (the field-test `unsigned.bin` placeholder, including a symlink that resolves to one) and a bounded read returns no bytes. `st_size` alone is not enough. Detail `PE has no Authenticode certificate table`. PowerShell is not started. Content from procfs, sysfs, or any other size-0 pseudo-file is not this placeholder: non-PE bytes are `NOT_A_PE_FILE`, and a read error is `ARTIFACT_UNREADABLE:<errno-name>`. `/dev/null`, `/dev/zero`, FIFOs, sockets, block devices, and directories are not this placeholder.
- The file is a well-formed PE32 or PE32+ and its security data directory is exactly `(0, 0)`. Every offset needed to read that directory is inside the file, `SizeOfOptionalHeader` includes it, and `NumberOfRvaAndSizes` is at least 5. Same detail. PowerShell is not started.
- The certificate blob is fully inside the file, 8-byte aligned, at least 8 bytes, and `Get-AuthenticodeSignature` on the private copy reports `NotSigned` (numeric `2`, string `"2"`, or `"NotSigned"`).

Without `--expect-unsigned`, a zero-length regular file and a `(0, 0)` security directory are `FAIL`. `SIGNED_VALID` and `NOT_RUN` still exit 0. `NotSigned` is `FAIL`.

`UnknownError` (numeric `1`, string `"1"`, or `"UnknownError"`) is an invalid signature. It exits 1. Every other status, and any unknown, unparsable, missing, empty, or duplicate status, exits 1. The printed status is `FAIL`. The detail is one of:

- `SIGNATURE_STATUS_REJECTED:<Name>` for a known status that is not accepted
- `SIGNATURE_STATUS_UNPARSED` for a value outside the enum
- `SIGNATURE_STATUS_EMPTY` for a blank status
- `SIGNATURE_STATUS_MISSING` when Status is absent or null
- `SIGNATURE_STATUS_AMBIGUOUS` when a JSON object repeats a key
- `NOT_A_PE_FILE` for a non-empty file that does not start with `MZ`. MSI, ZIP, scripts, and text use this code. Nothing in this repository passes those files to `--expect-unsigned`.
- `PE_SECURITY_DIR_OUT_OF_RANGE` when a non-zero security directory is past EOF or its offset and size overflow
- `PE_MALFORMED:<reason>` for a truncated or inconsistent DOS, PE, or optional header, an unknown optional-header magic, a misaligned certificate table, a security directory that is neither `(0, 0)` nor an in-file certificate, or `header_exceeds_parse_window` when classification would materialize more than 1 MiB (1048576 bytes) of header fields
- `ARTIFACT_UNREADABLE:<errno-name>` when the path cannot be opened. `<errno-name>` is Python `errno.errorcode`, for example `ENOENT` (missing path or dangling symlink), `EACCES` (permission denied), `ELOOP` (symlink loop), or `EISDIR` (a directory, including when `fstat` shows a directory after open). Exit 1. No traceback.
- `ARTIFACT_NOT_REGULAR_FILE` when the opened descriptor is not `stat.S_ISREG`, and also when `open` fails but `stat` shows a socket, FIFO, or device. A Unix socket whose `open` returns `ENXIO` uses this code, not `ARTIFACT_UNREADABLE:ENXIO`. Character devices (`/dev/null`, `/dev/zero`), block devices, FIFOs, and sockets use this code even when `st_size` is 0. On Unix the open uses `O_NONBLOCK`, so a FIFO does not block. Exit 1. PowerShell is not started.
- `ARTIFACT_TOO_LARGE` when `st_size` is greater than 512 MiB (536870912 bytes). The file is not read. Exit 1.
- `ARTIFACT_CHANGED` when the bytes classified are not the bytes `Get-AuthenticodeSignature` verified. The checker streams a private copy in 1 MiB (1048576 bytes) chunks and compares the classified header spans with that copy. An fd is held open across the cmdlet. The verdict is kept only when that fd and the path still share `st_dev`, `st_ino`, `st_size`, and `st_mtime_ns`, and the SHA-256 still matches the bytes hashed before the call. On Linux an inotify watch must also stay quiet, so restoring the bytes and `st_mtime_ns` is still `ARTIFACT_CHANGED`. A mismatch, a watch event, or a short read exits 1. Residual: if inotify cannot be armed, including on Windows where it does not exist, a same-user writer who restores both the bytes and `st_mtime_ns` before the post-check is not distinguished from an unchanged file. A missing watch is not itself a failure and must not raise. The cmdlet reads the path, not the held fd.

Classification reads only the DOS, COFF, optional-header, and security-directory fields it needs. It does not load the certificate blob. The 512 MiB (536870912 bytes) cap is applied to `st_size` before that read. Linux `NOT_RUN` does not create the private copy, because PowerShell is not started. A one-byte-short certificate table is `PE_SECURITY_DIR_OUT_OF_RANGE`, not an unsigned file. Publisher and timestamp checks remain owner decisions (issue #105 item c). This map does not claim a real signed-artifact run.

## Current blockers
Owner decisions remain required for legal publisher identity and provider/certificate procurement. Current product remains Release Candidate / External Qualification Pending and is not production-signed.
