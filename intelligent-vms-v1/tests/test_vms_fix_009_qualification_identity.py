"""Regression: qualification stamps must follow canonical identity manifests.

Expected product, schema, and MediaMTX values are loaded from the manifests at
runtime so a later version bump does not make these assertions stale.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "qualification" / "windows" / "scripts" / "qualify.py"
PRODUCT_MANIFEST = ROOT / "release" / "windows" / "product-version.json"
RUNTIME_MANIFEST = ROOT / "infra" / "mediamtx" / "runtime.json"
PRODUCT = json.loads(PRODUCT_MANIFEST.read_text(encoding="utf-8"))
RUNTIME = json.loads(RUNTIME_MANIFEST.read_text(encoding="utf-8"))
# Historical stand-in previously written even when no MediaMTX artifact existed.
STAND_IN_SHA256 = "faa97974861eb75a68b5aa326c78e7e7a6f670b5ef191bace78e715130381f23"
SHA256_NOT_PROVIDED = "NOT_PROVIDED"


def _load_qualify():
    spec = importlib.util.spec_from_file_location("qualify_vms_fix_009", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_manifests(root: Path, *, product=None, runtime=None, product_text=None, runtime_text=None):
    product_path = root / "release" / "windows" / "product-version.json"
    runtime_path = root / "infra" / "mediamtx" / "runtime.json"
    product_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    if product_text is not None:
        product_path.write_text(product_text, encoding="utf-8")
    elif product is not None:
        product_path.write_text(json.dumps(product), encoding="utf-8")
    if runtime_text is not None:
        runtime_path.write_text(runtime_text, encoding="utf-8")
    elif runtime is not None:
        runtime_path.write_text(json.dumps(runtime), encoding="utf-8")


def _new_run(tmp_path: Path, *extra: str, installer: str = "IntelligentVMS-FieldTest-Setup-x64.exe"):
    output = tmp_path / "qualification-result.json"
    command = [
        sys.executable,
        str(SCRIPT),
        "new-run",
        "--sequence",
        "1",
        "--git-sha",
        "a" * 40,
        "--installer",
        installer,
        "--installer-sha256",
        "b" * 64,
        "--output",
        str(output),
        *extra,
    ]
    completed = subprocess.run(command, capture_output=True, text=True, cwd=ROOT, check=False)
    return completed, output


def test_new_run_stamps_canonical_identity_and_does_not_fabricate_mediamtx_hash(tmp_path):
    completed, output = _new_run(tmp_path)
    assert completed.returncode == 0, completed.stderr
    stamped = json.loads(output.read_text(encoding="utf-8"))
    build = stamped["build"]
    assert build["product_version"] == PRODUCT["product_version"]
    assert build["installer_version"] == PRODUCT["installer_version"]
    assert build["server_version"] == PRODUCT["server_package_version"]
    assert build["client_version"] == PRODUCT["client_package_version"]
    assert build["db_schema_revision"] == PRODUCT["schema_baseline"]
    assert build["mediamtx"]["version"] == RUNTIME["runtime_version"]
    assert build["mediamtx"]["sha256"] == SHA256_NOT_PROVIDED
    assert build["mediamtx"]["sha256_status"] == SHA256_NOT_PROVIDED
    assert build["product"] == PRODUCT["product"]
    assert STAND_IN_SHA256 not in output.read_text(encoding="utf-8")
    assert stamped["evidence_source"] == "SIMULATED"
    assert stamped["test_profile"] == "LAYER_A_SIMULATION"
    assert stamped["overall_result"] == "NOT_RUN"
    assert stamped["qualification_run_id"].endswith("-001")


def test_supplied_matching_identity_is_accepted(tmp_path):
    completed, output = _new_run(
        tmp_path,
        "--product-version",
        PRODUCT["product_version"],
        "--installer-version",
        PRODUCT["installer_version"],
        "--server-version",
        PRODUCT["server_package_version"],
        "--client-version",
        PRODUCT["client_package_version"],
        "--schema-revision",
        PRODUCT["schema_baseline"],
        "--mediamtx-version",
        RUNTIME["runtime_version"],
    )
    assert completed.returncode == 0, completed.stderr
    stamped = json.loads(output.read_text(encoding="utf-8"))
    build = stamped["build"]
    assert build["product_version"] == PRODUCT["product_version"]
    assert build["installer_version"] == PRODUCT["installer_version"]
    assert build["server_version"] == PRODUCT["server_package_version"]
    assert build["client_version"] == PRODUCT["client_package_version"]
    assert build["db_schema_revision"] == PRODUCT["schema_baseline"]
    assert build["mediamtx"]["version"] == RUNTIME["runtime_version"]
    assert build["mediamtx"]["sha256"] == SHA256_NOT_PROVIDED
    assert stamped["evidence_source"] == "SIMULATED"


@pytest.mark.parametrize(
    ("flag", "supplied"),
    [
        ("--product-version", "9.9.9"),
        ("--installer-version", "9.9.9.0"),
        ("--server-version", "9.9.9"),
        ("--client-version", "9.9.9"),
        ("--schema-revision", "0000"),
        ("--mediamtx-version", "0.0.0-mismatch"),
    ],
)
def test_supplied_identity_mismatch_fails(tmp_path, flag, supplied):
    completed, output = _new_run(tmp_path, flag, supplied)
    assert completed.returncode != 0
    assert "supplied identity mismatch" in completed.stderr
    assert supplied in completed.stderr
    assert output.exists() is False


def test_mediamtx_artifact_hash_is_computed_when_it_matches_a_manifest_pin(tmp_path):
    artifact = tmp_path / "mediamtx.zip"
    artifact.write_bytes(b"mediamtx-runtime-bytes")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    root = tmp_path / "product"
    runtime = dict(RUNTIME)
    runtime["windows_zip_sha256"] = digest
    _write_manifests(root, product=PRODUCT, runtime=runtime)
    completed, output = _new_run(
        tmp_path,
        "--product-root",
        str(root),
        "--mediamtx-artifact",
        str(artifact),
    )
    assert completed.returncode == 0, completed.stderr
    media = json.loads(output.read_text(encoding="utf-8"))["build"]["mediamtx"]
    assert media["version"] == RUNTIME["runtime_version"]
    assert media["sha256"] == digest
    assert media["sha256_status"] == "COMPUTED"
    assert media["sha256"] != STAND_IN_SHA256
    stamped = json.loads(output.read_text(encoding="utf-8"))
    assert stamped["evidence_source"] == "SIMULATED"
    assert stamped["build"]["product_version"] == PRODUCT["product_version"]
    assert stamped["build"]["db_schema_revision"] == PRODUCT["schema_baseline"]


def test_windows_exe_pin_is_accepted_for_observed_artifact(tmp_path):
    artifact = tmp_path / "mediamtx.exe"
    artifact.write_bytes(b"mediamtx-exe-bytes")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    root = tmp_path / "product"
    runtime = dict(RUNTIME)
    runtime["windows_exe_sha256"] = digest
    _write_manifests(root, product=PRODUCT, runtime=runtime)
    completed, output = _new_run(tmp_path, "--product-root", str(root), "--mediamtx-artifact", str(artifact))
    assert completed.returncode == 0, completed.stderr
    media = json.loads(output.read_text(encoding="utf-8"))["build"]["mediamtx"]
    assert media["sha256"] == digest
    assert media["sha256_status"] == "COMPUTED"
    assert media["version"] == RUNTIME["runtime_version"]


def test_source_archive_pin_is_not_treated_as_the_runtime_artifact(tmp_path):
    artifact = tmp_path / "mediamtx-source.tar.gz"
    artifact.write_bytes(b"source-tarball")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    root = tmp_path / "product"
    runtime = dict(RUNTIME)
    runtime["source_sha256"] = digest
    _write_manifests(root, product=PRODUCT, runtime=runtime)
    completed, output = _new_run(tmp_path, "--product-root", str(root), "--mediamtx-artifact", str(artifact))
    assert completed.returncode != 0
    assert "observed identity mismatch" in completed.stderr
    assert digest in completed.stderr
    assert output.exists() is False


def test_observed_mediamtx_artifact_mismatch_fails(tmp_path):
    artifact = tmp_path / "mediamtx.zip"
    artifact.write_bytes(b"not-the-pinned-runtime")
    completed, output = _new_run(tmp_path, "--mediamtx-artifact", str(artifact))
    assert completed.returncode != 0
    assert "observed identity mismatch" in completed.stderr
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() in completed.stderr
    assert output.exists() is False


def test_missing_mediamtx_artifact_fails(tmp_path):
    missing = tmp_path / "absent-mediamtx.zip"
    completed, output = _new_run(tmp_path, "--mediamtx-artifact", str(missing))
    assert completed.returncode != 0
    assert "mediamtx artifact missing" in completed.stderr
    assert output.exists() is False


def test_missing_product_manifest_fails(tmp_path):
    root = tmp_path / "product"
    _write_manifests(root, runtime=RUNTIME)
    completed, output = _new_run(tmp_path, "--product-root", str(root))
    assert completed.returncode != 0
    assert "canonical manifest missing" in completed.stderr
    assert "product-version.json" in completed.stderr
    assert output.exists() is False


def test_missing_runtime_manifest_fails(tmp_path):
    root = tmp_path / "product"
    _write_manifests(root, product=PRODUCT)
    completed, output = _new_run(tmp_path, "--product-root", str(root))
    assert completed.returncode != 0
    assert "canonical manifest missing" in completed.stderr
    assert "runtime.json" in completed.stderr
    assert output.exists() is False


def test_malformed_json_manifest_fails(tmp_path):
    root = tmp_path / "product"
    _write_manifests(root, product_text="{not-json", runtime=RUNTIME)
    completed, output = _new_run(tmp_path, "--product-root", str(root))
    assert completed.returncode != 0
    assert "canonical manifest malformed" in completed.stderr
    assert "product-version.json" in completed.stderr
    assert output.exists() is False


def test_non_object_manifest_fails(tmp_path):
    root = tmp_path / "product"
    _write_manifests(root, product_text="[]\n", runtime=RUNTIME)
    completed, output = _new_run(tmp_path, "--product-root", str(root))
    assert completed.returncode != 0
    assert "canonical manifest malformed" in completed.stderr
    assert "expected a JSON object" in completed.stderr
    assert output.exists() is False


@pytest.mark.parametrize(
    ("manifest", "field"),
    [
        ("product", "schema_baseline"),
        ("product", "server_package_version"),
        ("product", "client_package_version"),
        ("runtime", "runtime_version"),
        ("runtime", "windows_zip_sha256"),
    ],
)
def test_manifest_missing_required_field_fails(tmp_path, manifest, field):
    root = tmp_path / "product"
    product = dict(PRODUCT)
    runtime = dict(RUNTIME)
    if manifest == "product":
        product.pop(field)
    else:
        runtime.pop(field)
    _write_manifests(root, product=product, runtime=runtime)
    completed, output = _new_run(tmp_path, "--product-root", str(root))
    assert completed.returncode != 0
    assert "canonical manifest malformed" in completed.stderr
    assert field in completed.stderr
    assert output.exists() is False


def test_malformed_runtime_pin_fails(tmp_path):
    root = tmp_path / "product"
    runtime = dict(RUNTIME)
    runtime["windows_exe_sha256"] = "not-a-sha256"
    _write_manifests(root, product=PRODUCT, runtime=runtime)
    completed, output = _new_run(tmp_path, "--product-root", str(root))
    assert completed.returncode != 0
    assert "canonical manifest malformed" in completed.stderr
    assert "windows_exe_sha256" in completed.stderr
    assert output.exists() is False


def test_release_manifest_uses_stamped_product_instead_of_a_literal():
    qualify = _load_qualify()
    stamped_name = PRODUCT["product"] + " stamped"
    result = {
        "qualification_run_id": "VMS-WIN-20261005-001",
        "overall_result": "NOT_RUN",
        "build": {
            "product": stamped_name,
            "git_sha": "a" * 40,
            "installer": "x.exe",
            "installer_version": PRODUCT["installer_version"],
            "installer_sha256": "b" * 64,
            "product_version": PRODUCT["product_version"],
            "server_version": PRODUCT["server_package_version"],
            "client_version": PRODUCT["client_package_version"],
            "db_schema_revision": PRODUCT["schema_baseline"],
        },
    }
    manifest = qualify.release_manifest(result)
    assert manifest["product"] == stamped_name
    omitted = dict(result)
    omitted["build"] = dict(result["build"])
    omitted["build"].pop("product")
    assert qualify.release_manifest(omitted)["product"] == PRODUCT["product"]
