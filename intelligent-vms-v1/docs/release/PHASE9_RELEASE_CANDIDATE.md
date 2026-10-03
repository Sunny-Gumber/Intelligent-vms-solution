# Phase 9 Release Candidate Readiness Report

Issue: #192

## Decision contract

The executable gate for this report is:

```bash
python tools/phase9_release_gate.py --output /tmp/phase9-release-report.json
```

When the software gate passes while any external qualification item is still pending,
the only permitted status is:

```text
RELEASE_CANDIDATE_EXTERNAL_QUALIFICATION_PENDING
```

This status means the software/SRE/security release-candidate baseline is ready for
Boss acceptance after the required CI and Reviewer gates pass. It does **not** mean
Production Qualified.

The gate intentionally never emits `PRODUCTION_QUALIFIED`. Even when every external
evidence item is attached, it stops at
`EXTERNAL_EVIDENCE_READY_FOR_BOSS_REVIEW` because the Production Execution Program
reserves that final decision for the Boss after external evidence review.

## Machine-readable policy

The versioned policy is:

`release/phase9_release_policy.json`

It checks required repository evidence and governance contracts for:

- the Phase 7 deterministic distributed/chaos software exit gate;
- the Phase 8 benchmark/hardware-matrix and reproducibility framework;
- Kubernetes/Helm production deployment and upgrade preflight;
- Phase 9 observability, alerts and SLO documentation;
- backup/restore, DR and rollback evidence tooling;
- the Phase 9 production-security baseline;
- normal Intelligent VMS CI and the dedicated security workflow;
- integrity of the 623-row market feature catalog.

Required files are hashed into the generated JSON report. Required documentation
markers are also checked so a placeholder or accidentally replaced file does not count
as evidence merely because a path exists.

## CI and merge evidence

The repository gate is one part of the Release Candidate decision. Before #192 is
accepted, the final pull-request head must also show:

- repository CI PASS;
- Intelligent VMS CI PASS;
- Intelligent VMS Security PASS;
- migration checks PASS;
- Phase 7 chaos qualification PASS;
- Phase 8 benchmark harness smoke PASS;
- Phase 9 recovery/upgrade-preflight smoke PASS;
- Helm/Compose/image gates PASS;
- Reviewer PASS or an explicit conditional release decision for a tracked issue.

The exact PR head, workflow runs, Reviewer decision and merge commit are recorded in
the #192/#201 Boss reports rather than hard-coded into this document.

## Phase 8 interpretation

Phase 8 provides a measured-evidence schema, benchmark tools, reproducibility checks
and a hardware-matrix compiler. The compiler deliberately reports unsupported
hardware/demand combinations as `UNQUALIFIED`.

No camera-per-server, 100K media, GPU, storage or latency claim may be inferred from
the existence of the benchmark framework. Real target-hardware results remain an
external qualification input.

## Security exception tracking

Issue #261 tracks inherited HIGH findings in the current Python base images for which
the Phase 9 Trivy run reports no fixed package version. The security workflow:

1. reports all HIGH/CRITICAL image findings;
2. separately blocks fixable HIGH/CRITICAL findings.

The #261 exception is limited to Release Candidate evaluation, remains visible in the
machine-readable release report, and is not a Production Qualified waiver.

## External qualification blockers

The current policy keeps the following items at `PENDING` until real evidence is
attached:

1. real-camera interoperability across target vendors/models/firmware;
2. measured target-hardware capacity and safe headroom;
3. target storage/network/WAN/backfill qualification;
4. real-environment HA, failure and regional-recovery drills;
5. target backup/restore with measured RPO/RTO;
6. N-1 to N upgrade and rollback with recording continuity;
7. deployed OIDC/PKI/mTLS/KMS, gateway limits, immutable SIEM and penetration/abuse
   evidence;
8. pilot soak and operator-workflow validation.

A future Phase 10 evidence update may change an item from `PENDING` to `PASS` only
when `evidence_path` points to a non-empty repository evidence artifact. The
executable gate hashes that artifact into its report.

## Market-feature relationship

The 48-category / 623-row feature catalog is a separate breadth program. The Release
Candidate gate verifies catalog integrity and keeps it connected to release governance,
but it does not claim all catalog capabilities are implemented or verified.

Therefore:

- Release Candidate != Production Qualified;
- Release Candidate != Full Market Feature Complete;
- Production Qualified requires Phase 10 external evidence;
- Full Market Feature Complete requires the separate market-feature completion rules.

## Failure behavior

The executable gate returns a non-zero exit code when:

- a required software evidence file is missing;
- a required evidence marker is removed;
- the feature catalog row count/IDs are invalid;
- a release exception is marked blocking;
- an external item is marked PASS without a real evidence file;
- policy paths attempt to escape the repository;
- the policy schema is unsupported.

Use `--require-external` during Phase 10 qualification work to make any pending
external item fail the command. That mode still stops at Boss review and never
self-declares Production Qualified.
