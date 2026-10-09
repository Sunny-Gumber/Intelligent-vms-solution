# Intelligent VMS

Standalone repository for the Intelligent VMS product.

**Product status:** **Release Candidate / External Qualification Pending**

This repository does **not** claim Production Qualified status, Full Market Feature Complete status, certified camera counts, certified hardware capacity, or certified Windows support.

## Repository layout

- `INTELLIGENT_VMS_START_HERE.md` — mandatory bootstrap for engineers and agents.
- `intelligent-vms-v1/` — application, tests, migrations, deployment assets, product/program documentation, benchmarks and release tooling.
- `.github/agents/` and `.github/copilot-instructions.md` — VMS-specific engineering-agent guidance.
- `migration-staging/workflows/` — non-executable workflow staging used only while this repository remains private during migration.

The VMS product directory is intentionally **not flattened** during repository migration.

## Migration state

The accepted clean-snapshot source baseline is:

- legacy recovery repository: `Sunny-Gumber/camvault`
- accepted legacy main: `6a97653e596c9dab3ab212c7b045fea2cd3fbb15`
- accepted milestone: Ubuntu 22.04/24.04 Field-Test Deployment Baseline
- unfinished legacy Windows work is preserved separately at head `7fc9065a43977329c91988f4dad5e123826f60cc` and is **not** part of the accepted baseline.

Until migration issue #1 completes all post-public validation gates, `Sunny-Gumber/camvault` remains the frozen authoritative recovery source. See `intelligent-vms-v1/docs/program/MIGRATION_PROVENANCE.md`.

## Start here

Read `INTELLIGENT_VMS_START_HERE.md` before changing code.

For the accepted Ubuntu field-test procedure, see:

- `intelligent-vms-v1/docs/operations/UBUNTU_FIELD_TEST.md`
- `intelligent-vms-v1/docs/testing/UBUNTU_FIELD_TEST_SMOKE.md`

The normal engineering gates include Python compilation, Ruff/public-docstring checks, full pytest, the deterministic 623-row / 48-category feature catalog check, Alembic migration validation, Compose/Helm validation, security scanning, chaos/release gates and Ubuntu field-test validation.

From a clean environment, the Python test gate is:

```bash
cd intelligent-vms-v1 && pip install -r tests/requirements.txt && VMS_SECRET_KEY=ci-test-key python3 -m pytest -q tests
```

`tests/requirements.txt` includes the pinned control-api requirements, so that single install collects the suite.

## Security

Never commit camera credentials, service tokens, production secrets, private signing keys, diagnostic archives or customer data. Read `SECURITY.md` and the security documents under `intelligent-vms-v1/docs/security/`.

## License

**LICENSE DECISION PENDING — OWNER ACTION**

Repository visibility does not itself grant an open-source license. No license is being added or implied by this migration.
