#!/usr/bin/env python3
"""Fail the hosted PostgreSQL job when its tests were skipped or not collected.

The unit job still runs without a database URL, so the PostgreSQL fixtures skip
there. The vms-postgres job writes JUnit for the race modules and for the lease
cases collected through the 127.0.0.1 probe. Pytest exits 0 when those fixtures
skip, so this gate reads the reports and rejects a green run that did not
execute them.
"""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path


# Synthetic disposable login hardcoded by the lease probe. Not a production secret.
LEASE_PROBE_URL = "postgresql+asyncpg://vms:vms@127.0.0.1:5432/vms_fix_014"

# (test name, minimum passed cases, substring required in the JUnit name)
# Race modules do not put the URL in the node id. Lease cases do, via the probe.
REQUIRED_POSTGRES_CASES: tuple[tuple[str, int, str], ...] = (
    ("test_alarm_close_is_not_overwritten_by_stale_acknowledge", 1, ""),
    ("test_out_of_order_heartbeat_does_not_regress", 1, ""),
    ("test_recording_health_completion_is_monotonic", 1, ""),
    ("test_camera_delete_leaves_no_active_manual_recording", 1, ""),
    ("test_closed_alarm_survives_stale_acknowledge", 1, ""),
    ("test_older_or_equal_heartbeat_does_not_overwrite_newer_node_state", 1, ""),
    ("test_first_insert_older_completion_does_not_overwrite_newer", 1, ""),
    ("test_first_insert_newer_completion_advances_older_row", 1, ""),
    ("test_recording_health_keeps_newer_equal_and_null_boundaries", 1, ""),
    ("test_delete_during_manual_start_leaves_no_active_session", 1, ""),
    ("test_off_page_site_move_committed_by_a_second_session_is_not_renewed", 1, LEASE_PROBE_URL),
    ("test_recording_disabled_by_a_second_session_is_not_renewed", 1, LEASE_PROBE_URL),
    ("test_off_page_recording_and_ai_disabled_by_a_second_session_are_not_renewed", 1, LEASE_PROBE_URL),
    ("test_failover_does_not_overwrite_a_newer_generation", 1, LEASE_PROBE_URL),
    ("test_max_run_overrun_rolls_the_lease_back", 1, LEASE_PROBE_URL),
    ("test_later_chunk_invalidation_rolls_the_earlier_chunk_back", 2, LEASE_PROBE_URL),
    ("test_recheck_window_cannot_commit_changed_authority", 2, LEASE_PROBE_URL),
    ("test_recheck_window_cannot_commit_node_region", 1, LEASE_PROBE_URL),
)


class PostgresCiReport:
    """JUnit check result for the hosted PostgreSQL job.

    Attributes:
        violations: Reasons a required PostgreSQL test did not pass.
        summary: One-line counts printed into the CI log.
    """

    def __init__(self, violations: list[str], summary: str) -> None:
        self.violations = violations
        self.summary = summary


def _outcome(case: ET.Element) -> str:
    if case.find("skipped") is not None:
        return "skipped"
    if case.find("failure") is not None:
        return "failed"
    if case.find("error") is not None:
        return "error"
    return "passed"


def _matches(case_name: str, required: str, probe: str) -> bool:
    if case_name != required and not case_name.startswith(required + "["):
        return False
    return not probe or probe in case_name


def _load_cases(path: Path) -> tuple[list[ET.Element], str | None]:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return [], f"junit report unreadable: {path}: {exc}"
    return list(root.iter("testcase")), None


def postgres_ci_report(junit_paths: Sequence[Path]) -> PostgresCiReport:
    """Check JUnit reports from the hosted PostgreSQL job.

    Args:
        junit_paths: Pytest JUnit XML files for the race modules and lease probe.

    Returns:
        Violations plus a one-line count summary. An empty violation list means
        every required PostgreSQL case passed and nothing was skipped.
    """
    violations: list[str] = []
    cases: list[tuple[Path, ET.Element]] = []
    if not junit_paths:
        violations.append("no junit reports were provided")
    for path in junit_paths:
        if not path.is_file():
            violations.append(f"junit report missing: {path}")
            continue
        loaded, error = _load_cases(path)
        if error:
            violations.append(error)
            continue
        cases.extend((path, case) for case in loaded)

    skipped = 0
    failed = 0
    errors = 0
    passed_names: list[str] = []
    for path, case in cases:
        name = case.get("name") or ""
        outcome = _outcome(case)
        if outcome == "passed":
            passed_names.append(name)
            continue
        if outcome == "skipped":
            skipped += 1
            message = (case.find("skipped").get("message") or "").strip()
            violations.append(f"skipped {path}::{name}: {message}")
            continue
        if outcome == "failed":
            failed += 1
        else:
            errors += 1
        violations.append(f"{outcome} {path}::{name}")

    race_passed = 0
    lease_passed = 0
    for required, minimum, probe in REQUIRED_POSTGRES_CASES:
        matched = [name for name in passed_names if _matches(name, required, probe)]
        if probe:
            lease_passed += len(matched)
        else:
            race_passed += len(matched)
        if len(matched) < minimum:
            where = f" containing {probe}" if probe else ""
            violations.append(
                f"missing passed cases for {required}{where}: "
                f"need {minimum}, found {len(matched)}"
            )

    summary = (
        "POSTGRES_CI_SUMMARY "
        f"race_passed={race_passed} "
        f"lease_postgres_passed={lease_passed} "
        f"skipped={skipped} failed={failed} errors={errors}"
    )
    return PostgresCiReport(violations, summary)


def main(argv: Sequence[str] | None = None) -> int:
    """Check hosted PostgreSQL JUnit reports and print the count summary.

    Args:
        argv: Command-line arguments, excluding the program name. Defaults to
            the process arguments.

    Returns:
        Zero when every required PostgreSQL case passed. One otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit", nargs="+", type=Path, help="Pytest JUnit XML reports")
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = postgres_ci_report(args.junit)
    print(report.summary)
    if report.violations:
        print("PostgreSQL CI gate failed:")
        for violation in report.violations:
            print(f"- {violation}")
        return 1
    print("PostgreSQL CI gate OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
