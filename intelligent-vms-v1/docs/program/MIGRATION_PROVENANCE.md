# Migration Provenance — Standalone Intelligent VMS Repository

## Purpose

This file preserves source provenance without publishing the private multi-project Git history.

## Legacy recovery source

- Repository: `Sunny-Gumber/camvault`
- Accepted source main: `6a97653e596c9dab3ab212c7b045fea2cd3fbb15`
- Accepted PR head: `859b213cacd03a99f03e1cc7aa41b93ac55ef915`
- Accepted legacy milestone: issue #301 / PR #302 — Ubuntu 22.04/24.04 Field-Test Deployment Baseline
- Accepted merge commit: `6a97653e596c9dab3ab212c7b045fea2cd3fbb15`

Historical evidence references such as `camvault PR #300` remain legacy evidence. They must not be rewritten as destination PR numbers that never existed.

## Unfinished Windows source

The Windows server milestone was deliberately **not** merged into legacy main.

- Legacy issue: `Sunny-Gumber/camvault #303`
- Legacy PR: `Sunny-Gumber/camvault #304`
- Legacy branch: `release/windows-field-test-server-baseline`
- Frozen/current legitimate head at migration staging: `7fc9065a43977329c91988f4dad5e123826f60cc`
- Legacy PR state at staging: OPEN / UNMERGED
- Changed scope at that head: 30 files, approximately 1901 additions / 75 deletions

Known remaining Windows blocker at that head: the Windows CI pre-install regression step does not configure a TEST-ONLY `VMS_SECRET_KEY` for two existing recording tests. Native installer/service execution has therefore not yet been accepted.

## Clean-snapshot migration

The destination does **not** inherit the legacy camvault Git history.

The accepted import scope is:

- `INTELLIGENT_VMS_START_HERE.md`
- `intelligent-vms-v1/`
- minimum VMS-specific engineering-agent guidance required by the VMS CI contract
- VMS-only workflows, held under non-executable staging while the destination remains private

Unrelated legacy CamVault code, tests, cloud/snapshot applications, deployment tooling, documentation and mixed CamVault workflow jobs are excluded.

The exact accepted VMS import was first committed on destination branch `migration-public-release-baseline` as destination commit `fa4bdc13e89e41bd925aa448c628d999a1201da3`. At that point 323/323 imported VMS files matched the source blobs exactly, with zero missing and zero mismatched files.

Publication-hygiene changes after that exact-import commit are intentionally reviewable as destination-only deltas.

## Destination

- Repository: `Sunny-Gumber/Intelligent-vms-solution`
- Migration issue: #1
- Private-stage branch: `migration-public-release-baseline`

The destination becomes authoritative only after the owner makes it public, required workflows are activated, the accepted baseline passes required destination CI/security/Ubuntu gates, independent migration review passes, and the baseline migration PR is merged.

Until then `Sunny-Gumber/camvault` remains the frozen recovery source.

## Windows continuation after baseline cutover

After accepted destination main is established, create a new destination issue/branch/PR for the Windows server milestone, apply the legitimate legacy PR #304 delta rather than reimplementing it, add the deterministic TEST-ONLY `VMS_SECRET_KEY` setup, and execute Server 2022/2025 native validation.

The native Windows desktop client is a later milestone and is not part of migration or the Windows server baseline.

## Product status

Throughout migration: **Release Candidate / External Qualification Pending**.
