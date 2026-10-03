#!/usr/bin/env python3
"""Validate Phase 9 release evidence without fabricating production qualification."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

POLICY_SCHEMA = "phase9-release-policy-v1"
REPORT_SCHEMA = "phase9-release-report-v1"
STATUS_NOT_CANDIDATE = "NOT_RELEASE_CANDIDATE"
STATUS_RELEASE_CANDIDATE = "RELEASE_CANDIDATE_EXTERNAL_QUALIFICATION_PENDING"
STATUS_EXTERNAL_READY = "EXTERNAL_EVIDENCE_READY_FOR_BOSS_REVIEW"
_EXTERNAL_STATES = {"PENDING", "PASS"}
_EXCEPTION_STATES = {"TRACKED", "CLOSED"}


def _repo_path(root: Path, raw_path: str) -> Path:
    relative = Path(raw_path)
    if not raw_path or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe repository evidence path: {raw_path!r}")

    candidate = root / relative
    if candidate.is_symlink():
        raise ValueError(f"release evidence path must not be a symlink: {raw_path}")

    resolved_root = root.resolve()
    resolved = candidate.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError(f"release evidence path escapes repository: {raw_path}")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_check(root: Path, item: dict[str, Any]) -> dict[str, Any]:
    artifact_id = str(item.get("id", "")).strip()
    raw_path = str(item.get("path", "")).strip()
    if not artifact_id:
        raise ValueError("software evidence item requires id")

    path = _repo_path(root, raw_path)
    result = {"id": artifact_id, "path": raw_path, "status": "FAIL"}
    if not path.is_file():
        result["reason"] = "missing required evidence file"
        return result

    text = path.read_text(encoding="utf-8")
    missing = [str(marker) for marker in item.get("required_text", []) if str(marker) not in text]
    if missing:
        result["reason"] = "required evidence marker missing"
        result["missing_markers"] = missing
        return result

    result.update({"status": "PASS", "sha256": _sha256(path), "bytes": path.stat().st_size})
    return result


def _catalog_check(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    raw_path = str(config.get("path", "")).strip()
    id_column = str(config.get("id_column", "feature_id")).strip()
    expected_rows = int(config.get("expected_rows", 0))
    path = _repo_path(root, raw_path)
    result = {"path": raw_path, "status": "FAIL"}

    if not path.is_file():
        result["reason"] = "feature catalog missing"
        return result

    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    ids = [str(row.get(id_column, "")).strip() for row in rows]
    unique_ids = {value for value in ids if value}
    result.update({"rows": len(rows), "unique_ids": len(unique_ids)})
    if len(rows) != expected_rows:
        result["reason"] = f"expected {expected_rows} rows"
        return result
    if len(unique_ids) != len(rows):
        result["reason"] = "feature IDs must be non-empty and unique"
        return result

    result.update({"status": "PASS", "sha256": _sha256(path)})
    return result


def _external_check(root: Path, item: dict[str, Any]) -> dict[str, Any]:
    evidence_id = str(item.get("id", "")).strip()
    state = str(item.get("status", "")).strip().upper()
    if not evidence_id:
        raise ValueError("external qualification item requires id")
    if state not in _EXTERNAL_STATES:
        raise ValueError(f"invalid external qualification state for {evidence_id}: {state}")

    result = {
        "id": evidence_id,
        "status": state,
        "description": str(item.get("description", "")).strip(),
    }
    if state == "PENDING":
        return result

    raw_path = str(item.get("evidence_path", "")).strip()
    if not raw_path:
        result.update({"status": "FAIL", "reason": "PASS requires evidence_path"})
        return result

    path = _repo_path(root, raw_path)
    if not path.is_file() or path.stat().st_size == 0:
        result.update({"status": "FAIL", "reason": "external evidence file missing or empty"})
        return result

    result.update({"evidence_path": raw_path, "sha256": _sha256(path), "bytes": path.stat().st_size})
    return result


def _tracked_exceptions(policy: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    normalized: list[dict[str, Any]] = []
    blocking = False
    for item in policy.get("tracked_release_exceptions", []):
        exception_id = str(item.get("id", "")).strip()
        state = str(item.get("status", "")).strip().upper()
        if not exception_id:
            raise ValueError("tracked release exception requires id")
        if state not in _EXCEPTION_STATES:
            raise ValueError(f"invalid tracked exception state for {exception_id}: {state}")
        item_blocking = bool(item.get("blocking", False))
        blocking = blocking or item_blocking
        normalized.append(
            {
                "id": exception_id,
                "status": state,
                "blocking": item_blocking,
                "reference": str(item.get("reference", "")).strip(),
                "description": str(item.get("description", "")).strip(),
            }
        )
    return normalized, blocking


def evaluate_release(repository_root: Path, policy: dict[str, Any]) -> dict[str, Any]:
    """Evaluate repository evidence and return a conservative Phase 9 decision.

    Args:
        repository_root: Root directory of the checked-out Git repository.
        policy: Parsed Phase 9 release policy document.

    Returns:
        Machine-readable report with software, external and overall release status.

    Raises:
        ValueError: If policy schema, paths or evidence states are invalid.
        OSError: If required evidence files cannot be read.
    """
    if policy.get("schema_version") != POLICY_SCHEMA:
        raise ValueError("unsupported Phase 9 release policy schema")

    artifacts = [_artifact_check(repository_root, item) for item in policy.get("software_artifacts", [])]
    if not artifacts:
        raise ValueError("release policy requires software_artifacts")

    catalog = _catalog_check(repository_root, dict(policy.get("feature_catalog", {})))
    exceptions, exception_blocking = _tracked_exceptions(policy)
    external = [_external_check(repository_root, item) for item in policy.get("external_qualification", [])]
    if not external:
        raise ValueError("release policy requires external_qualification items")

    software_pass = all(item["status"] == "PASS" for item in artifacts)
    software_pass = software_pass and catalog["status"] == "PASS" and not exception_blocking
    external_fail = any(item["status"] == "FAIL" for item in external)
    external_pending = any(item["status"] == "PENDING" for item in external)

    if not software_pass or external_fail:
        overall = STATUS_NOT_CANDIDATE
    elif external_pending:
        overall = STATUS_RELEASE_CANDIDATE
    else:
        overall = STATUS_EXTERNAL_READY

    return {
        "schema_version": REPORT_SCHEMA,
        "policy_schema": POLICY_SCHEMA,
        "release_name": str(policy.get("release_name", "Intelligent VMS")),
        "software_gate": "PASS" if software_pass else "FAIL",
        "external_qualification": (
            "FAIL" if external_fail else "PENDING" if external_pending else "PASS"
        ),
        "overall_status": overall,
        "software_artifacts": artifacts,
        "feature_catalog": catalog,
        "tracked_release_exceptions": exceptions,
        "external_items": external,
    }


def _load_policy(path: Path) -> dict[str, Any]:
    policy = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(policy, dict):
        raise ValueError("release policy must be a JSON object")
    return policy


def _default_repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=_default_repository_root())
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path("intelligent-vms-v1/release/phase9_release_policy.json"),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--require-external",
        action="store_true",
        help="Fail unless all external qualification evidence is attached.",
    )
    return parser


def main() -> int:
    """Run the Phase 9 release gate and return a process exit code.

    Returns:
        Zero for a valid software release candidate, or non-zero when the software
        gate fails or external evidence is required but not complete.
    """
    args = _parser().parse_args()
    root = args.repository_root.resolve()
    policy_path = args.policy if args.policy.is_absolute() else root / args.policy
    report = evaluate_release(root, _load_policy(policy_path))
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")

    if report["overall_status"] == STATUS_NOT_CANDIDATE:
        return 1
    if args.require_external and report["external_qualification"] != "PASS":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
