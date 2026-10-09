"""The hosted PostgreSQL job must fail when its JUnit report hides a skip."""

from pathlib import Path
from xml.sax.saxutils import escape

from tools.check_postgres_ci_junit import (
    LEASE_PROBE_URL,
    REQUIRED_POSTGRES_CASES,
    postgres_ci_report,
)


def _junit(cases: list[str]) -> str:
    body = "\n".join(cases)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuites><testsuite tests="{len(cases)}">{body}</testsuite></testsuites>\n'
    )


def _passed(name: str) -> str:
    return f'<testcase classname="tests.sample" name="{escape(name)}" time="0.1"/>'


def _write(tmp_path: Path, name: str, cases: list[str]) -> Path:
    path = tmp_path / name
    path.write_text(_junit(cases), encoding="utf-8")
    return path


def _required_passed_names() -> list[str]:
    names: list[str] = []
    for required, minimum, probe in REQUIRED_POSTGRES_CASES:
        for index in range(minimum):
            if probe:
                suffix = "site" if index == 0 else "recording"
                names.append(f"{required}[{probe}-{suffix}]")
            else:
                names.append(required)
    return names


def test_required_cases_name_the_tests_the_unit_job_skips():
    """Lock the race fixtures and the lease probe ids the CI job must execute."""
    names = [item[0] for item in REQUIRED_POSTGRES_CASES]
    assert names.count("test_delete_during_manual_start_leaves_no_active_session") == 1
    assert names.count("test_recording_health_completion_is_monotonic") == 1
    assert names.count("test_closed_alarm_survives_stale_acknowledge") == 1
    assert names.count("test_recheck_window_cannot_commit_changed_authority") == 1
    assert names.count("test_later_chunk_invalidation_rolls_the_earlier_chunk_back") == 1
    lease = [item for item in REQUIRED_POSTGRES_CASES if item[2] == LEASE_PROBE_URL]
    assert sum(item[1] for item in lease) == 10
    assert sum(item[1] for item in REQUIRED_POSTGRES_CASES if not item[2]) == 10
    assert "vms:vms@127.0.0.1:5432/vms_fix_014" in LEASE_PROBE_URL


def test_complete_reports_pass_with_zero_skips(tmp_path: Path):
    """A report that passed every required case is a clean gate."""
    race = [name for name in _required_passed_names() if LEASE_PROBE_URL not in name]
    lease = [name for name in _required_passed_names() if LEASE_PROBE_URL in name]
    report = postgres_ci_report(
        [
            _write(tmp_path, "race.xml", [_passed(name) for name in race]),
            _write(tmp_path, "lease.xml", [_passed(name) for name in lease]),
        ]
    )
    assert report.violations == []
    assert report.summary == (
        "POSTGRES_CI_SUMMARY race_passed=10 lease_postgres_passed=10 "
        "skipped=0 failed=0 errors=0"
    )


def test_skip_fails_even_when_the_name_was_collected(tmp_path: Path):
    """Pytest's exit code stays 0 for a skipped fixture, so the report must not."""
    cases = [_passed(name) for name in _required_passed_names()]
    cases[0] = (
        '<testcase classname="tests.sample" '
        'name="test_alarm_close_is_not_overwritten_by_stale_acknowledge">'
        '<skipped type="pytest.skip" message="PostgreSQL race reproductions need '
        'VMS_TEST_POSTGRES_URL"/>'
        "</testcase>"
    )
    report = postgres_ci_report([_write(tmp_path, "skipped.xml", cases)])
    assert any(item.startswith("skipped ") for item in report.violations)
    assert any("missing passed cases for test_alarm_close" in item for item in report.violations)
    assert "skipped=1" in report.summary


def test_sqlite_lease_case_does_not_satisfy_the_probe(tmp_path: Path):
    """A SQLite parameter is not the 127.0.0.1 lease probe."""
    names = [
        name
        for name in _required_passed_names()
        if not name.startswith("test_recheck_window_cannot_commit_node_region")
    ]
    names.append("test_recheck_window_cannot_commit_node_region[sqlite]")
    report = postgres_ci_report([_write(tmp_path, "sqlite.xml", [_passed(name) for name in names])])
    assert any(
        "missing passed cases for test_recheck_window_cannot_commit_node_region" in item
        for item in report.violations
    )


def test_missing_report_and_failed_case_are_violations(tmp_path: Path):
    """A missing file or a failure cannot be reported as a passed PostgreSQL run."""
    failed = (
        '<testcase classname="tests.sample" '
        'name="test_delete_during_manual_start_leaves_no_active_session">'
        '<failure message="assert False">trace</failure>'
        "</testcase>"
    )
    report = postgres_ci_report(
        [
            tmp_path / "absent.xml",
            _write(tmp_path, "failed.xml", [failed]),
        ]
    )
    assert any(item.startswith("junit report missing:") for item in report.violations)
    assert any(item.startswith("failed ") for item in report.violations)
    assert "failed=1" in report.summary
