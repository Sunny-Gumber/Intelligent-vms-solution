"""Field-test PostgreSQL restore must not run when writers are not stopped.

The docker and curl programs on PATH are stubs. Nothing in this module contacts
a daemon or restores a database.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
RESTORE_SCRIPT = ROOT / "deploy" / "field-test" / "field-test-restore-postgres.sh"
WRITER_SERVICES = (
    "control-api",
    "onvif-event-worker",
    "alarm-worker",
    "placement-controller",
    "event-writer",
)

_DOCKER_STUB = """#!/usr/bin/env bash
set -euo pipefail
log="${DOCKER_STUB_LOG:?}"
printf '%s\\n' "$(printf '%q ' "$@")" >> "$log"

if [[ "${1:-}" != "compose" ]]; then
  echo "stub docker: expected 'compose', received: $*" >&2
  exit 97
fi
shift
if [[ "${1:-}" == "--env-file" ]]; then
  shift 2
fi
compose_command="${1:-}"
shift || true

case "$compose_command" in
  stop)
    exit "${DOCKER_STUB_STOP_EXIT:-0}"
    ;;
  ps)
    if [[ "${DOCKER_STUB_PS_EXIT:-0}" != "0" ]]; then
      echo "stub docker: ps failed" >&2
      exit "${DOCKER_STUB_PS_EXIT}"
    fi
    quiet=0
    statuses=()
    services=()
    while [[ $# -gt 0 ]]; do
      case "$1" in
        -q|--quiet) quiet=1; shift ;;
        --status)
          statuses+=("${2:-}")
          shift 2
          ;;
        --format) shift 2 ;;
        --) shift ;;
        -*) shift ;;
        *) services+=("$1"); shift ;;
      esac
    done
    IFS=',' read -ra active_specs <<< "${DOCKER_STUB_ACTIVE:-}"
    for spec in "${active_specs[@]}"; do
      [[ -z "$spec" ]] && continue
      service="${spec%%:*}"
      state="${spec#*:}"
      requested=0
      if [[ ${#services[@]} -eq 0 ]]; then
        requested=1
      else
        for candidate in "${services[@]}"; do
          [[ "$candidate" == "$service" ]] && requested=1
        done
      fi
      status_match=0
      if [[ ${#statuses[@]} -eq 0 ]]; then
        status_match=1
      else
        for status in "${statuses[@]}"; do
          [[ "$status" == "$state" ]] && status_match=1
        done
      fi
      if [[ "$requested" -eq 1 && "$status_match" -eq 1 ]]; then
        if [[ "$quiet" -eq 1 ]]; then
          printf 'stub-container-%s\\n' "$service"
        else
          printf '%s %s\\n' "$service" "$state"
        fi
      fi
    done
    ;;
  exec)
    cat >/dev/null
    ;;
  up|run)
    ;;
  *)
    echo "stub docker: unhandled compose command: ${compose_command}" >&2
    exit 96
    ;;
esac
"""

_CURL_STUB = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$(printf '%q ' "$@")" >> "${DOCKER_STUB_LOG:?}"
exit 0
"""


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _invocations(log_path: Path) -> list[str]:
    if not log_path.exists():
        return []
    return log_path.read_text(encoding="utf-8").splitlines()


def _assert_pg_restore_absent(invocations: list[str]) -> None:
    for line in invocations:
        assert "pg_restore" not in line, line


def _run_restore(tmp_path: Path, **stub_env: str) -> subprocess.CompletedProcess[str]:
    """Run the field-test restore script against the PATH stub."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "docker", _DOCKER_STUB)
    _write_executable(bin_dir / "curl", _CURL_STUB)
    backup = tmp_path / "database.dump"
    backup.write_bytes(b"synthetic-field-test-dump")
    env_file = tmp_path / "field.env"
    env_file.write_text("POSTGRES_PASSWORD=synthetic\\n", encoding="utf-8")
    log_path = tmp_path / "docker-invocations.log"
    environment = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "HOME": str(tmp_path),
        "CONFIRM_RESTORE": "YES",
        "BACKUP_FILE": str(backup),
        "VMS_ENV_FILE": str(env_file),
        "DOCKER_STUB_LOG": str(log_path),
        "DOCKER_STUB_STOP_EXIT": stub_env.get("DOCKER_STUB_STOP_EXIT", "0"),
        "DOCKER_STUB_PS_EXIT": stub_env.get("DOCKER_STUB_PS_EXIT", "0"),
        "DOCKER_STUB_ACTIVE": stub_env.get("DOCKER_STUB_ACTIVE", ""),
    }
    completed = subprocess.run(
        [str(RESTORE_SCRIPT)],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    completed.stub_invocations = _invocations(log_path)  # type: ignore[attr-defined]
    return completed


def test_failed_writer_stop_never_invokes_pg_restore(tmp_path: Path) -> None:
    """A non-zero compose stop aborts before pg_restore and says why."""
    completed = _run_restore(tmp_path, DOCKER_STUB_STOP_EXIT="1")
    invocations = completed.stub_invocations  # type: ignore[attr-defined]

    assert completed.returncode != 0
    assert "failed to stop writer services" in completed.stderr
    assert "pg_restore was not run" in completed.stderr
    _assert_pg_restore_absent(invocations)
    assert invocations, "writer stop was not attempted"
    assert " stop " in f" {invocations[0]} "
    for service in WRITER_SERVICES:
        assert service in invocations[0]


@pytest.mark.parametrize(
    "active_writer",
    [
        "alarm-worker:running",
        "event-writer:restarting",
        "placement-controller:paused",
    ],
)
def test_active_writer_blocks_restore_after_stop_returns_zero(tmp_path: Path, active_writer: str) -> None:
    """Stop exiting zero is not enough when a writer container is still active."""
    completed = _run_restore(tmp_path, DOCKER_STUB_ACTIVE=active_writer)
    invocations = completed.stub_invocations  # type: ignore[attr-defined]
    service = active_writer.split(":", 1)[0]

    assert completed.returncode != 0
    assert "writer containers still running:" in completed.stderr
    assert service in completed.stderr
    assert "pg_restore was not run" in completed.stderr
    _assert_pg_restore_absent(invocations)
    assert any(" stop " in f" {line} " for line in invocations)


def test_writer_state_check_failure_blocks_restore(tmp_path: Path) -> None:
    """A failed stopped-state query is fail-closed and does not restore."""
    completed = _run_restore(tmp_path, DOCKER_STUB_PS_EXIT="1")
    invocations = completed.stub_invocations  # type: ignore[attr-defined]

    assert completed.returncode != 0
    assert "unable to verify writer service" in completed.stderr
    assert "pg_restore was not run" in completed.stderr
    _assert_pg_restore_absent(invocations)


def test_stopped_writers_allow_restore_without_touching_real_data(tmp_path: Path) -> None:
    """Exited or dead containers are stopped, so the stubbed restore may continue."""
    completed = _run_restore(
        tmp_path,
        DOCKER_STUB_ACTIVE="alarm-worker:exited,control-api:dead",
    )
    invocations = completed.stub_invocations  # type: ignore[attr-defined]

    assert completed.returncode == 0, completed.stderr
    assert "field_test_postgres_restore_ok" in completed.stdout
    assert any("pg_restore" in line for line in invocations)
    stop_at = next(index for index, line in enumerate(invocations) if " stop " in f" {line} ")
    state_at = next(index for index, line in enumerate(invocations) if " ps " in f" {line} ")
    restore_at = next(index for index, line in enumerate(invocations) if "pg_restore" in line)
    assert stop_at < state_at < restore_at
    for service in WRITER_SERVICES:
        assert any(service in line and " ps " in f" {line} " for line in invocations)
