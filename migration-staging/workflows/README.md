# Workflow Staging — Do Not Activate While Private

These files are intentionally outside `.github/workflows/` so GitHub Actions cannot run during private migration staging.

Accepted-source provenance: `Sunny-Gumber/camvault@6a97653e596c9dab3ab212c7b045fea2cd3fbb15`.

Staged accepted-baseline workflows:

- `intelligent-vms-ci.yml`
- `intelligent-vms-security.yml`
- `intelligent-vms-ubuntu-field-test.yml`

The source repository's generic `.github/workflows/ci.yml` is **not** migrated because it contains unrelated CamVault backend/image jobs. Its VMS coverage is provided by the dedicated Intelligent VMS workflow and will be reviewed/adapted during Phase B.

The Windows field-test workflow is not part of accepted main. It belongs to the frozen legacy Windows delta at `7fc9065a43977329c91988f4dad5e123826f60cc` and will be recreated with the Windows milestone only after the public accepted baseline is merged.

After the owner confirms this repository is PUBLIC, move/adapt only the required VMS workflows into `.github/workflows/` and run the Phase B validation gates.
