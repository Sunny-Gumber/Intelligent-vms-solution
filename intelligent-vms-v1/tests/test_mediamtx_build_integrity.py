"""Offline checks for pinned MediaMTX Windows runtime artifact integrity."""

import hashlib
import json
from pathlib import Path

import pytest

from tools.build_mediamtx import verify_windows_artifact_hashes

ROOT = Path(__file__).resolve().parents[1]


def _artifact_manifest(zip_bytes: bytes, exe_bytes: bytes) -> dict:
    """Return test-only SHA-256 pins for byte-exact fabricated artifacts."""
    return {
        "windows_zip_sha256": hashlib.sha256(zip_bytes).hexdigest(),
        "windows_exe_sha256": hashlib.sha256(exe_bytes).hexdigest(),
    }


def test_windows_artifact_hashes_accept_both_exact_pins(tmp_path: Path) -> None:
    """Accept a ZIP only when both the ZIP and executable match approved hashes."""
    archive = tmp_path / "mediamtx.zip"
    executable = tmp_path / "mediamtx.exe"
    archive.write_bytes(b"sample deterministic zip")
    executable.write_bytes(b"sample deterministic executable")
    manifest = _artifact_manifest(archive.read_bytes(), executable.read_bytes())
    assert verify_windows_artifact_hashes(archive, executable, manifest) == manifest["windows_zip_sha256"]


@pytest.mark.parametrize("modified_file", ["zip", "exe"])
def test_windows_artifact_hashes_reject_modified_input(
    tmp_path: Path, modified_file: str
) -> None:
    """Fail closed for independently modified ZIP or executable artifacts."""
    archive = tmp_path / "mediamtx.zip"
    executable = tmp_path / "mediamtx.exe"
    archive.write_bytes(b"trusted zip")
    executable.write_bytes(b"trusted exe")
    manifest = _artifact_manifest(archive.read_bytes(), executable.read_bytes())
    (archive if modified_file == "zip" else executable).write_bytes(b"modified")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        verify_windows_artifact_hashes(archive, executable, manifest)


def test_mediamtx_go_patch_pin_matches_build_consumers() -> None:
    """Keep the patched Go baseline consistent in Linux and Windows builders."""
    manifest = json.loads((ROOT / "infra/mediamtx/runtime.json").read_text())
    go_version = manifest["go_version"]
    assert tuple(map(int, go_version.split("."))) >= (1, 26, 9)
    consumers = [
        (ROOT / "infra/mediamtx/Dockerfile").read_text(),
        (ROOT.parent / ".github/workflows/intelligent-vms-windows-field-test.yml").read_text(),
        (ROOT.parent / ".github/workflows/intelligent-vms-windows-installer.yml").read_text(),
    ]
    assert f"golang:{go_version}-alpine" in consumers[0]
    assert all(f"go-version: '{go_version}'" in content for content in consumers[1:])

@pytest.mark.parametrize("changed_field", ["Version", "Sum"])
def test_go_module_override_rejects_version_or_checksum_drift(changed_field: str) -> None:
    """Check that transitive x/net cannot roll back or change without detection."""
    from tools.build_mediamtx import verify_go_module_pin

    manifest = {
        "x_net_version": "v0.60.0",
        "x_net_checksum": "h1:79p50tfZlm0J9YfoDsSi639qSXNGVwEzOPLCxM2FsYU=",
    }
    module = {"Version": manifest["x_net_version"], "Sum": manifest["x_net_checksum"]}
    verify_go_module_pin(module, manifest)
    module[changed_field] = "unexpected"
    with pytest.raises(RuntimeError, match="version or checksum mismatch"):
        verify_go_module_pin(module, manifest)
