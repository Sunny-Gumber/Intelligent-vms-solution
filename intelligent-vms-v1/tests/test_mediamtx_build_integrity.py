"""Offline checks for pinned MediaMTX Windows runtime artifact integrity."""

import hashlib
import json
import re
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
    assert manifest["x_net_version"] == "v0.60.0"
    assert manifest["x_net_checksum"] == "h1:79p50tfZlm0J9YfoDsSi639qSXNGVwEzOPLCxM2FsYU="
    assert go_version == "1.26.9"
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


_CONSUMER_PINS = (
    pytest.param(
        "deploy/windows/Install-WindowsFieldTest.ps1",
        "windows_zip_sha256",
        r'^[ \t]*\$mediaSha[ \t]*=[ \t]*"([0-9a-f]{64})"[ \t]*,
        id="field-test-install-zip",
    ),
    pytest.param(
        "deploy/windows/Install-WindowsFieldTest.ps1",
        "windows_exe_sha256",
        r'^[ \t]*\$mediaExeSha[ \t]*=[ \t]*"([0-9a-f]{64})"[ \t]*,
        id="field-test-install-executable",
    ),
    pytest.param(
        "deploy/windows/installer/Build-Installer.ps1",
        "windows_zip_sha256",
        r'^[ \t]*\$mediaSha[ \t]*=[ \t]*"([0-9a-f]{64})"[ \t]*,
        id="unified-installer-build-zip",
    ),
    pytest.param(
        "tests/test_windows_unified_installer.py",
        "windows_zip_sha256",
        r'^[ \t]*assert "([0-9a-f]{64})" in BUILD[ \t]*,
        id="unified-installer-contract-zip",
    ),
    pytest.param(
        "tests/test_windows_field_test_release.py",
        "windows_zip_sha256",
        r'^[ \t]*assert "([0-9a-f]{64})" in INSTALL[ \t]*,
        id="field-test-contract-zip",
    ),
    pytest.param(
        ".github/workflows/intelligent-vms-windows-field-test.yml",
        "windows_zip_sha256",
        r'^[ \t]*if\(\$hash -ne "([0-9a-f]{64})"\)\{throw "Pinned MediaMTX direct-script fixture hash mismatch"\}[ \t]*,
        id="field-test-ci-zip",
    ),
    pytest.param(
        ".github/workflows/intelligent-vms-windows-installer.yml",
        "windows_exe_sha256",
        r'^[ \t]*if\(\(Get-FileHash -Algorithm SHA256 \$installedMedia\)\.Hash\.ToLowerInvariant\(\) -ne "([0-9a-f]{64})"\)\{throw "Upgrade did not replace the accepted-baseline media runtime"\}[ \t]*,
        id="installer-upgrade-ci-executable",
    ),
)


def _consumer_file(relative_path: str) -> Path:
    """Map a product- or repo-relative pin consumer to its checked-in path."""
    return (ROOT.parent if relative_path.startswith(".github/") else ROOT) / relative_path


def _assert_consumer_pin(path: Path, expected_sha256: str, pattern: str) -> None:
    """Require every active, anchored consumer assignment to match the manifest."""
    actual_hashes = re.findall(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    assert actual_hashes, f"Missing MediaMTX checksum assignment in {path}"
    assert all(digest == expected_sha256 for digest in actual_hashes), (
        f"Stale or conflicting MediaMTX checksum assignment in {path}"
    )


@pytest.mark.parametrize("relative_path,hash_field,assignment_pattern", _CONSUMER_PINS)
def test_verified_runtime_digests_match_all_offline_consumers(
    relative_path: str, hash_field: str, assignment_pattern: str
) -> None:
    """Match each required active ZIP/EXE consumer, not any matching consumer."""
    manifest = json.loads((ROOT / "infra/mediamtx/runtime.json").read_text())
    expected = manifest[hash_field]
    assert re.fullmatch(r"[0-9a-f]{64}", expected)
    _assert_consumer_pin(_consumer_file(relative_path), expected, assignment_pattern)


@pytest.mark.parametrize("relative_path,hash_field,assignment_pattern", _CONSUMER_PINS)
def test_every_consumer_rejects_stale_and_missing_hash_assignment(
    tmp_path: Path, relative_path: str, hash_field: str, assignment_pattern: str
) -> None:
    """Reject one altered or absent consumer even while all other pins remain valid."""
    manifest = json.loads((ROOT / "infra/mediamtx/runtime.json").read_text())
    expected = manifest[hash_field]
    source = _consumer_file(relative_path).read_text(encoding="utf-8")
    assert expected in source, f"Expected source pin missing: {relative_path}"

    # The original test incorrectly passed if any other file had a good digest.
    # Keep the correct digest in a comment to reject comment-only false positives.
    corrupted = source.replace(expected, "0" * 64, 1)
    assert corrupted != source
    mutated_consumer = tmp_path / "mutated-consumer.txt"
    mutated_consumer.write_text(
        corrupted + f"\n# Historical checksum (not active): {expected}\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="Stale or conflicting"):
        _assert_consumer_pin(mutated_consumer, expected, assignment_pattern)
    with pytest.raises(FileNotFoundError):
        _assert_consumer_pin(tmp_path / "missing-consumer.txt", expected, assignment_pattern)
