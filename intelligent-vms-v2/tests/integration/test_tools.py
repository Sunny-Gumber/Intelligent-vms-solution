"""Tests for the operational tools.

The diagnostic-bundle test is the important one: it plants known secrets into
configuration, the database and the logs, builds a real bundle, then reads
every byte of the resulting archive and asserts none of those secrets appear.
A support bundle that leaks camera passwords would be worse than no bundle.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"


def run_tool(script: str, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    import os

    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(TOOLS / script), *args],
                          capture_output=True, text=True, env=env, cwd=str(ROOT),
                          check=False)


# ==========================================================================
# Diagnostic bundle
# ==========================================================================
def test_diagnostic_bundle_contains_no_secrets(seeded, settings, db_session, tmp_path):
    """Plant real secrets everywhere, then prove the bundle leaks none of them."""

    camera_password = "Sup3rSecret-CameraPw-ZZZ"
    jwt_secret = settings.secret_key
    smtp_password = "smtp-secret-QQQ"

    from app.core.crypto import CredentialVault, load_or_create_master_key
    from app.db.models import Camera
    from app.devices.credentials import CredentialStore

    vault = CredentialVault(load_or_create_master_key(settings.keys_dir))
    store = CredentialStore(vault)

    camera = Camera(name="Secret Cam", host="192.168.80.10", port=80)
    db_session.add(camera)
    db_session.flush()
    store.set(db_session, camera, "admin", camera_password)
    db_session.commit()

    settings.smtp_password = smtp_password
    settings.smtp_host = "smtp.example.com"

    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    (settings.logs_dir / "control-api.log").write_text(
        json.dumps({"event": "camera.probe",
                    "url": f"rtsp://admin:{camera_password}@192.168.80.10/s1"}) + "\n",
        encoding="utf-8")

    sys.path.insert(0, str(TOOLS))
    import diag_bundle

    archive_path = diag_bundle.build(settings, tmp_path, log_lines=500)
    assert archive_path.is_file()

    with zipfile.ZipFile(archive_path) as archive:
        blob = b"".join(archive.read(name) for name in archive.namelist())
    text = blob.decode("utf-8", errors="replace")

    assert camera_password not in text, "camera password leaked into the support bundle"
    assert jwt_secret not in text, "JWT signing secret leaked into the support bundle"
    assert smtp_password not in text, "SMTP password leaked into the support bundle"

    master_key = (settings.keys_dir / "vms-master.key").read_bytes()
    assert master_key not in blob, "master encryption key leaked into the support bundle"

    # It must still be useful.
    with zipfile.ZipFile(archive_path) as archive:
        diagnostics = json.loads(archive.read("diagnostics.json"))
    assert diagnostics["version"]["product"] == "Intelligent VMS"
    assert diagnostics["database"]["counts"]["cameras"] >= 1
    assert diagnostics["configuration"]["secret_key"] == "***redacted***"
    assert any(c["name"] == "Secret Cam" for c in diagnostics["database"]["cameras"])


def test_diagnostic_bundle_redacts_credentials_in_log_tails(seeded, settings, tmp_path):
    sys.path.insert(0, str(TOOLS))
    import diag_bundle

    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    (settings.logs_dir / "recorder.log").write_text(
        "connecting to rtsp://operator:LeakMe123@10.1.1.5:554/stream1\n", encoding="utf-8")

    tails = diag_bundle.tail_logs(settings)
    assert "LeakMe123" not in tails["recorder.log"]
    assert "***redacted***" in tails["recorder.log"]


# ==========================================================================
# Backup and restore
# ==========================================================================
def test_backup_excludes_the_master_key_by_default(seeded, settings, tmp_path):
    sys.path.insert(0, str(TOOLS))
    import backup_restore

    archive_path = backup_restore.backup(settings, tmp_path)
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))

    assert "keys/vms-master.key" not in names
    assert manifest["includes_master_key"] is False
    assert manifest["includes_recordings"] is False
    assert any("Master key NOT included" in note for note in manifest["restore_notes"])


def test_backup_can_include_the_key_when_explicitly_requested(seeded, settings, tmp_path):
    sys.path.insert(0, str(TOOLS))
    import backup_restore

    archive_path = backup_restore.backup(settings, tmp_path, include_keys=True)
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert "keys/vms-master.key" in archive.namelist()

    assert manifest["includes_master_key"] is True
    assert any("MASTER ENCRYPTION KEY" in note for note in manifest["restore_notes"])


def test_backup_records_an_accurate_inventory(seeded, settings, db_session, tmp_path):
    sys.path.insert(0, str(TOOLS))
    import backup_restore
    from app.db.models import Camera

    db_session.add(Camera(name="Inventory Cam", host="192.168.81.10"))
    db_session.commit()

    archive_path = backup_restore.backup(settings, tmp_path)
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))

    inventory = manifest["inventory"]
    assert inventory["counts"]["cameras"] >= 1
    assert any(c["name"] == "Inventory Cam" for c in inventory["cameras"])
    # Inventory must never carry credentials, only whether one is stored.
    assert all("password" not in json.dumps(c).lower() for c in inventory["cameras"])


def test_backup_restore_round_trip(seeded, settings, db_session, tmp_path):
    """A restored database must contain the data that was backed up."""
    sys.path.insert(0, str(TOOLS))
    import backup_restore
    from app.db import session as db
    from app.db.models import Camera

    db_session.add(Camera(name="Round Trip", host="192.168.82.10"))
    db_session.commit()

    archive_path = backup_restore.backup(settings, tmp_path)

    # Destroy the live data, then restore.
    db_session.query(Camera).delete()
    db_session.commit()
    assert db_session.query(Camera).count() == 0
    db_session.close()
    db.dispose_engine()

    assert backup_restore.restore(settings, archive_path) == 0

    db.get_engine(settings)
    with db.session_scope() as session:
        restored = session.query(Camera).filter_by(name="Round Trip").one_or_none()
        assert restored is not None
        assert restored.host == "192.168.82.10"


def test_restore_refuses_a_schema_mismatch(seeded, settings, tmp_path):
    sys.path.insert(0, str(TOOLS))
    import backup_restore

    archive_path = backup_restore.backup(settings, tmp_path)

    # Rewrite the manifest to claim a different schema revision.
    rewritten = tmp_path / "mismatched.zip"
    with zipfile.ZipFile(archive_path) as source, \
            zipfile.ZipFile(rewritten, "w", zipfile.ZIP_DEFLATED) as target:
        for name in source.namelist():
            data = source.read(name)
            if name == "manifest.json":
                manifest = json.loads(data)
                manifest["schema_revision"] = "9999_from_the_future"
                data = json.dumps(manifest).encode("utf-8")
            target.writestr(name, data)

    assert backup_restore.restore(settings, rewritten) == 2


# ==========================================================================
# SBOM
# ==========================================================================
def test_sbom_is_valid_cyclonedx_and_lists_external_processes(tmp_path):
    result = run_tool("generate_sbom.py", "--output", str(tmp_path))
    assert result.returncode == 0, result.stderr

    sbom = json.loads((tmp_path / "intelligent-vms-sbom.cdx.json").read_text())
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.5"
    assert sbom["metadata"]["component"]["name"] == "Intelligent VMS"

    names = {component["name"] for component in sbom["components"]}
    # Python packages
    assert "fastapi" in names
    assert "SQLAlchemy" in names
    # External processes must not be forgotten just because they are not pip packages
    assert "ffmpeg" in names
    assert "mediamtx" in names
    assert "postgresql" in names

    ffmpeg = next(c for c in sbom["components"] if c["name"] == "ffmpeg")
    note = " ".join(p["value"] for p in ffmpeg.get("properties", []))
    assert "separate process" in note
    assert "patent" in note.lower()


# ==========================================================================
# Feature matrix generator
# ==========================================================================
def test_feature_matrix_is_up_to_date():
    """CI gate: the published matrix must match requirements.yaml."""
    result = run_tool("generate_feature_matrix.py", "--check")
    assert result.returncode == 0, (
        "feature-matrix.md is stale or requirements.yaml is invalid:\n"
        f"{result.stdout}\n{result.stderr}")


def test_requirements_cover_all_twenty_modules():
    import yaml

    document = yaml.safe_load(
        (ROOT / "docs" / "requirements" / "requirements.yaml").read_text(encoding="utf-8"))
    module_ids = [module["id"] for module in document["modules"]]
    assert module_ids == [f"M{index}" for index in range(1, 21)]

    requirements = [r for m in document["modules"] for r in m["requirements"]]
    assert len(requirements) > 250

    # Nothing may claim automated verification without naming its evidence.
    for requirement in requirements:
        if requirement["verification"] == "automated":
            assert len(requirement.get("evidence", "")) >= 10, requirement["id"]


def test_matrix_does_not_claim_production_readiness():
    import yaml

    document = yaml.safe_load(
        (ROOT / "docs" / "requirements" / "requirements.yaml").read_text(encoding="utf-8"))
    assert document["meta"]["production_ready"] is False

    from app.version import PRODUCTION_READY

    assert PRODUCTION_READY is False, (
        "app.version.PRODUCTION_READY must stay False until the release gates in "
        "docs/verification/release-gates.md have passed")


# ==========================================================================
# Storage migration
# ==========================================================================
def test_storage_migration_verifies_before_deleting(seeded, settings, db_session,
                                                    tmp_path):
    """The source file must survive until the copy is checksum-verified."""
    sys.path.insert(0, str(TOOLS))
    import storage_migrate
    from app.db.models import RecordingSegment, StorageVolume
    from app.storage import volumes as volume_service

    source = db_session.query(StorageVolume).first()
    destination = volume_service.create_volume(
        db_session, name="archive", path=str(tmp_path / "archive"))
    db_session.commit()

    relative = "recordings/cam-mig/2026/01/01/main/clip.mp4"
    source_file = Path(source.path) / relative
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_bytes(b"\x00\x01\x02" * 1000)

    started = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    db_session.add(RecordingSegment(
        camera_id="cam-mig", stream_role="main", volume_id=source.id,
        relative_path=relative, started_at=started,
        ended_at=started + dt.timedelta(minutes=5), duration_s=300,
        size_bytes=source_file.stat().st_size, state="finalized"))
    db_session.commit()

    preview = storage_migrate.migrate(db_session, source, destination,
                                      older_than_days=None, camera_id=None,
                                      limit=10, dry_run=True)
    assert preview.migrated == 1
    assert source_file.is_file(), "a dry run must not move anything"

    result = storage_migrate.migrate(db_session, source, destination,
                                     older_than_days=None, camera_id=None,
                                     limit=10, dry_run=False)
    assert result.migrated == 1
    assert result.failed == 0

    moved = Path(destination.path) / relative
    assert moved.is_file()
    assert not source_file.exists()

    segment = db_session.query(RecordingSegment).filter_by(camera_id="cam-mig").one()
    assert segment.volume_id == destination.id
    assert segment.checksum and len(segment.checksum) == 64


# ==========================================================================
# Build stamping
# ==========================================================================
def test_build_stamp_never_invents_a_commit():
    result = run_tool("stamp_build.py", "--build-number", "42")
    assert result.returncode == 0
    values = dict(line.split("=", 1) for line in result.stdout.splitlines()
                  if "=" in line and not line.startswith("#"))
    assert values["VMS_BUILD_NUMBER"] == "42"
    assert values["VMS_BUILD_COMMIT"]  # either a real short hash or "dev"
    assert values["VMS_BUILD_DATE"].endswith("Z")
