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

## Current blockers
Owner decisions remain required for legal publisher identity and provider/certificate procurement. Current product remains Release Candidate / External Qualification Pending and is not production-signed.
