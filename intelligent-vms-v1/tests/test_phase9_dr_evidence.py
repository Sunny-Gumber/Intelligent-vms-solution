from datetime import datetime, timezone
from pathlib import Path
import json
import sys

import pytest

TOOLS = Path(__file__).parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from phase9_dr_evidence import (
    EXERCISE_QUALIFICATION,
    build_exercise_evidence,
    build_manifest,
    verify_manifest,
)


def test_backup_manifest_round_trip_is_deterministic(tmp_path):
    """Build and verify a checksum manifest without relying on wall-clock time."""
    root = tmp_path / "backup"
    root.mkdir()
    (root / "postgres.dump").write_bytes(b"postgres-evidence")
    (root / "nested").mkdir()
    (root / "nested" / "spool.db").write_bytes(b"regional-evidence")

    generated_at = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    manifest = build_manifest(root, generated_at=generated_at)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert manifest["generated_at"] == "2026-01-01T00:00:00+00:00"
    assert [item["path"] for item in manifest["files"]] == [
        "nested/spool.db",
        "postgres.dump",
    ]
    assert verify_manifest(manifest_path, root) == []


def test_backup_manifest_detects_tampering(tmp_path):
    """Report a checksum/size mismatch when backup content changes."""
    root = tmp_path / "backup"
    root.mkdir()
    file_path = root / "database.dump"
    file_path.write_bytes(b"before")

    manifest = build_manifest(
        root,
        generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    file_path.write_bytes(b"after-tamper")

    assert verify_manifest(manifest_path, root) == ["size:database.dump"]


def test_backup_manifest_detects_unexpected_file(tmp_path):
    """Report files added after the backup manifest was generated."""
    root = tmp_path / "backup"
    root.mkdir()
    (root / "database.dump").write_bytes(b"before")

    manifest = build_manifest(
        root,
        generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (root / "unexpected.txt").write_text("extra", encoding="utf-8")

    assert verify_manifest(manifest_path, root) == ["unexpected:unexpected.txt"]


def test_backup_manifest_rejects_symlink(tmp_path):
    """Reject symlinks so backup manifests cannot escape the declared root."""
    root = tmp_path / "backup"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    (root / "link").symlink_to(outside)

    with pytest.raises(ValueError, match="refuses symlink"):
        build_manifest(
            root,
            generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )


def test_dr_exercise_calculates_measured_rpo_rto_without_guarantee():
    """Calculate exercise RPO/RTO and retain the non-production qualification."""
    evidence = build_exercise_evidence(
        exercise_id="dr-001",
        exercise_type="region-loss",
        failure_at="2026-01-01T00:05:00Z",
        service_restored_at="2026-01-01T00:10:00Z",
        restore_point_at="2026-01-01T00:04:00Z",
        validation="pass",
        target_rpo_seconds=120,
        target_rto_seconds=600,
        notes="controlled exercise",
    )

    assert evidence["observed_rpo_seconds"] == 60.0
    assert evidence["observed_rto_seconds"] == 300.0
    assert evidence["rpo_target_met"] is True
    assert evidence["rto_target_met"] is True
    assert evidence["qualification"] == EXERCISE_QUALIFICATION
    assert "PRODUCTION" in evidence["qualification"]


def test_dr_exercise_rejects_temporally_invalid_restore_point():
    """Reject a restore point newer than the declared failure instant."""
    with pytest.raises(ValueError, match="restore_point_at cannot be after"):
        build_exercise_evidence(
            exercise_id="dr-002",
            exercise_type="restore",
            failure_at="2026-01-01T00:05:00Z",
            service_restored_at="2026-01-01T00:06:00Z",
            restore_point_at="2026-01-01T00:05:30Z",
            validation="pending",
        )
