import json
from pathlib import Path
import sys

import pytest

TOOLS = Path(__file__).parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from phase9_release_gate import (
    STATUS_EXTERNAL_READY,
    STATUS_NOT_CANDIDATE,
    STATUS_RELEASE_CANDIDATE,
    evaluate_release,
)

REPOSITORY_ROOT = Path(__file__).parents[2]
POLICY_PATH = (
    REPOSITORY_ROOT
    / "intelligent-vms-v1"
    / "release"
    / "phase9_release_policy.json"
)


def _minimal_policy(
    tmp_path: Path,
    *,
    marker: str = "ready",
    external_status: str = "PENDING",
    evidence_path: str = "",
) -> dict:
    artifact = tmp_path / "artifact.md"
    artifact.write_text("ready\n", encoding="utf-8")
    catalog = tmp_path / "catalog.csv"
    catalog.write_text("feature_id,name\nF01-001,Feature\n", encoding="utf-8")
    if evidence_path:
        evidence = tmp_path / evidence_path
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text("external evidence\n", encoding="utf-8")

    return {
        "schema_version": "phase9-release-policy-v1",
        "release_name": "test",
        "software_artifacts": [
            {
                "id": "artifact",
                "path": "artifact.md",
                "required_text": [marker],
            }
        ],
        "feature_catalog": {
            "path": "catalog.csv",
            "id_column": "feature_id",
            "expected_rows": 1,
        },
        "tracked_release_exceptions": [],
        "external_qualification": [
            {
                "id": "external",
                "status": external_status,
                "evidence_path": evidence_path,
                "description": "test external evidence",
            }
        ],
    }


def test_repository_policy_passes_software_and_stops_before_production():
    """Validate the real repository as RC with external qualification pending."""
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))

    report = evaluate_release(REPOSITORY_ROOT, policy)

    assert report["software_gate"] == "PASS"
    assert report["external_qualification"] == "PENDING"
    assert report["overall_status"] == STATUS_RELEASE_CANDIDATE
    assert report["feature_catalog"]["rows"] == 623
    assert report["feature_catalog"]["unique_ids"] == 623
    assert report["tracked_release_exceptions"][0]["reference"] == "#261"


def test_missing_required_marker_blocks_release_candidate(tmp_path):
    """Fail the software gate when a required evidence marker disappears."""
    policy = _minimal_policy(tmp_path, marker="missing-marker")

    report = evaluate_release(tmp_path, policy)

    assert report["software_gate"] == "FAIL"
    assert report["overall_status"] == STATUS_NOT_CANDIDATE
    assert report["software_artifacts"][0]["status"] == "FAIL"


def test_external_pass_without_evidence_file_is_rejected(tmp_path):
    """Reject an external PASS label that has no attached evidence file."""
    policy = _minimal_policy(tmp_path, external_status="PASS")

    report = evaluate_release(tmp_path, policy)

    assert report["external_qualification"] == "FAIL"
    assert report["overall_status"] == STATUS_NOT_CANDIDATE
    assert report["external_items"][0]["status"] == "FAIL"


def test_external_evidence_ready_still_requires_boss_review(tmp_path):
    """Stop at Boss review even when every external evidence item is attached."""
    policy = _minimal_policy(
        tmp_path,
        external_status="PASS",
        evidence_path="evidence/camera-matrix.txt",
    )

    report = evaluate_release(tmp_path, policy)

    assert report["software_gate"] == "PASS"
    assert report["external_qualification"] == "PASS"
    assert report["overall_status"] == STATUS_EXTERNAL_READY
    assert "sha256" in report["external_items"][0]


def test_release_policy_rejects_path_traversal(tmp_path):
    """Reject evidence paths that escape the repository root."""
    policy = _minimal_policy(tmp_path)
    policy["software_artifacts"][0]["path"] = "../outside.txt"

    with pytest.raises(ValueError, match="unsafe repository evidence path"):
        evaluate_release(tmp_path, policy)
