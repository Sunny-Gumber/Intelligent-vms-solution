# Phase A Migration Validation Record

**Date:** 2026-10-03  
**Destination branch:** `migration-public-release-baseline`

This record distinguishes checks actually executed during private staging from checks deliberately deferred to public GitHub Actions.

## Executed agent-side validation

| Check | Result |
|---|---|
| Source accepted SHA re-fetch | PASS — `6a97653e596c9dab3ab212c7b045fea2cd3fbb15` |
| Legacy Windows PR #304 head re-fetch | PASS — `7fc9065a43977329c91988f4dad5e123826f60cc`, open/unmerged |
| Clean-snapshot completeness before hygiene | PASS — 323 source VMS blobs, 0 missing, 0 mismatched at destination import commit `fa4bdc13e89e41bd925aa448c628d999a1201da3` |
| Active destination workflows | PASS — zero files under `.github/workflows/` during private staging |
| Feature catalog structural check | PASS — 623 data rows, 48 category IDs (1–48) |
| Alembic chain structural check | PASS — 17 sequential migrations, `0001` through `0017` |
| Sensitive filename/artifact scan | PASS — only committed env-like file is `.env.example`; no key/keystore/dump/archive/log artifact in accepted VMS tree |
| Targeted public-release content audit | PASS after documented sample-label sanitization; see `PUBLIC_REPOSITORY_SECURITY_AUDIT.md` |
| Unrelated CamVault workflow exclusion | PASS — mixed source `.github/workflows/ci.yml` is not staged |
| Required VMS root helper dependency | PASS — only VMS-specific Copilot/agent guidance required by dedicated VMS CI is migrated |

## Prior source evidence — not destination validation

Legacy PR #302 accepted head `859b213cacd03a99f03e1cc7aa41b93ac55ef915` previously had successful source-repository runs for:

- Intelligent VMS CI;
- Intelligent VMS Security;
- CI (including its VMS job);
- Intelligent VMS Ubuntu Field Test.

Those runs support provenance of the accepted source baseline, but they are **not** reported as destination CI.

## Deliberately not executed in private destination

The current tool path provides authenticated repository access through the GitHub connector but no local private-repository filesystem checkout. The migration also explicitly forbids activating private-repository Actions. Therefore the following are recorded as **NOT RE-EXECUTED IN DESTINATION PHASE A**:

```bash
cd intelligent-vms-v1
python -m compileall -q services tools tests
ruff check services tools tests
python tools/check_public_docstrings.py
VMS_SECRET_KEY=ci-test-key pytest -q tests
python tools/feature_catalog.py --check
python tools/phase7_chaos_matrix.py
cp .env.example .env && docker compose config -q
helm lint deploy/helm/intelligent-vms
helm template vms deploy/helm/intelligent-vms
```

The catalog and migration structures were independently checked agent-side as recorded above. Full executable validation is a mandatory Phase B gate after the owner makes the repository public.

No private Actions minutes were consumed for destination validation.
