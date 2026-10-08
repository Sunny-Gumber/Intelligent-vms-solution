"""Regression tests for VMS-FIX-004.

Windows SCM access-denied, query failure, and timeout must stop restore, purge,
upgrade, and uninstall before their destructive steps. Only a missing service
(Win32 error 1060) is a tolerated absence. The win32 modules are fakes so these
tests run on Linux CI without a Windows service controller.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
ERROR_ACCESS_DENIED = 5
ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_SERVICE_REQUEST_TIMEOUT = 1053
ERROR_SERVICE_SPECIFIC = 1066
MEDIA = "IntelligentVMSMedia"
CONTROL = "IntelligentVMSControl"
OPERATIONS = ("restore", "purge", "upgrade", "uninstall")
DESTRUCTIVE_STEP = {
    "restore": "pg_restore",
    "purge": "dropdb",
    "upgrade": "replace_application",
    "uninstall": "uninstall",
}


class ScmFailure(Exception):
    """pywin32-shaped SCM error: args[0] and winerror are the Win32 code."""

    def __init__(self, winerror: int, function: str, message: str) -> None:
        super().__init__(winerror, function, message)
        self.winerror = winerror


def _install_win32_fakes() -> None:
    win32service = types.ModuleType("win32service")
    win32service.SERVICE_STOPPED = 1
    win32service.SERVICE_START_PENDING = 2
    win32service.SERVICE_STOP_PENDING = 3
    win32service.SERVICE_RUNNING = 4
    win32service.SERVICE_AUTO_START = 2
    win32serviceutil = types.ModuleType("win32serviceutil")
    win32serviceutil.QueryServiceStatus = None
    win32serviceutil.StopService = None
    win32serviceutil.WaitForServiceStatus = None
    win32serviceutil.RemoveService = None
    win32serviceutil.StartService = None
    win32serviceutil.InstallService = None
    sys.modules["win32service"] = win32service
    sys.modules["win32serviceutil"] = win32serviceutil


_install_win32_fakes()

from deploy.windows import service_manager  # noqa: E402


class FakeScm:
    """In-memory service controller used in place of pywin32."""

    def __init__(self) -> None:
        running = service_manager.win32service.SERVICE_RUNNING
        self.stopped = service_manager.win32service.SERVICE_STOPPED
        self.status = {MEDIA: running, CONTROL: running}
        self.query_error: dict[str, BaseException] = {}
        self.stop_error: dict[str, BaseException] = {}
        self.wait_error: dict[str, BaseException] = {}
        self.remove_error: dict[str, BaseException] = {}
        self.query_error_during_removal: dict[str, BaseException] = {}
        self.linger_after_remove = False
        self.removed: set[str] = set()
        self.calls: list[tuple[object, ...]] = []

    def bind(self) -> None:
        util = service_manager.win32serviceutil
        util.QueryServiceStatus = self.query_service_status
        util.StopService = self.stop_service
        util.WaitForServiceStatus = self.wait_for_service_status
        util.RemoveService = self.remove_service

    def query_service_status(self, name: str):
        self.calls.append(("query", name))
        if name in self.removed and name in self.query_error_during_removal:
            raise self.query_error_during_removal[name]
        if name in self.query_error:
            raise self.query_error[name]
        if name not in self.status:
            raise ScmFailure(
                ERROR_SERVICE_DOES_NOT_EXIST,
                "QueryServiceStatus",
                "The specified service does not exist as an installed service.",
            )
        return (0, self.status[name], 0, 0, 0, 0, 0)

    def stop_service(self, name: str) -> None:
        self.calls.append(("stop", name))
        if name in self.stop_error:
            raise self.stop_error[name]
        self.status[name] = service_manager.win32service.SERVICE_STOP_PENDING

    def wait_for_service_status(self, name: str, status: int, wait_secs: int) -> None:
        self.calls.append(("wait", name, status, wait_secs))
        if name in self.wait_error:
            raise self.wait_error[name]
        if name not in self.status:
            raise ScmFailure(
                ERROR_SERVICE_DOES_NOT_EXIST,
                "WaitForServiceStatus",
                "The specified service does not exist as an installed service.",
            )
        self.status[name] = status

    def remove_service(self, name: str) -> None:
        self.calls.append(("remove", name))
        if name in self.remove_error:
            raise self.remove_error[name]
        if name not in self.status:
            raise ScmFailure(
                ERROR_SERVICE_DOES_NOT_EXIST,
                "RemoveService",
                "The specified service does not exist as an installed service.",
            )
        self.removed.add(name)
        if not self.linger_after_remove:
            self.status.pop(name, None)


def _bind_running_pair() -> FakeScm:
    scm = FakeScm()
    scm.bind()
    return scm


def _fail_control_stop(scm: FakeScm, failure: str) -> None:
    """Leave media stoppable and make control fail while it is still present."""
    if failure == "access_denied":
        scm.stop_error[CONTROL] = ScmFailure(ERROR_ACCESS_DENIED, "StopService", "Access is denied.")
    elif failure == "query_failure":
        scm.query_error[CONTROL] = OSError("service controller query failed")
    elif failure == "timeout":
        scm.wait_error[CONTROL] = ScmFailure(
            ERROR_SERVICE_REQUEST_TIMEOUT,
            "WaitForServiceStatus",
            "The service did not respond to the start or control request in a timely fashion.",
        )
    else:
        raise AssertionError(f"unknown SCM failure {failure}")


def _steps_started(operation: str) -> list[str]:
    """Model a maintenance caller: destructive work runs only if stop/remove return."""
    started: list[str] = []
    try:
        service_manager.stop()
    except service_manager.ServiceMaintenanceError:
        return started
    except Exception as exc:
        return [f"unexpected:{type(exc).__name__}:{exc}"]
    if operation == "uninstall":
        try:
            service_manager.remove()
        except service_manager.ServiceMaintenanceError:
            return started
        except Exception as exc:
            return [f"unexpected:{type(exc).__name__}:{exc}"]
    started.append(DESTRUCTIVE_STEP[operation])
    return started


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize("failure", ["access_denied", "query_failure", "timeout"])
@pytest.mark.parametrize("operation", OPERATIONS)
def test_scm_failure_does_not_start_destructive_maintenance(operation: str, failure: str) -> None:
    scm = _bind_running_pair()
    _fail_control_stop(scm, failure)
    started = _steps_started(operation)
    assert started == [], (
        f"{operation} started destructive steps during SCM {failure}: {started}; "
        f"calls={scm.calls}; status={scm.status}"
    )
    assert scm.status.get(CONTROL) != scm.stopped
    if operation == "uninstall":
        assert not any(call[0] == "remove" for call in scm.calls)


@pytest.mark.parametrize("operation", OPERATIONS)
def test_missing_service_is_the_only_tolerated_absence(operation: str) -> None:
    scm = _bind_running_pair()
    scm.status.clear()
    assert _steps_started(operation) == [DESTRUCTIVE_STEP[operation]]


@pytest.mark.parametrize("operation", OPERATIONS)
def test_already_stopped_service_allows_maintenance(operation: str) -> None:
    scm = _bind_running_pair()
    scm.status = {MEDIA: scm.stopped, CONTROL: scm.stopped}
    assert _steps_started(operation) == [DESTRUCTIVE_STEP[operation]]


@pytest.mark.parametrize("operation", OPERATIONS)
def test_missing_media_does_not_hide_control_scm_failure(operation: str) -> None:
    scm = _bind_running_pair()
    scm.status.pop(MEDIA)
    scm.stop_error[CONTROL] = ScmFailure(ERROR_ACCESS_DENIED, "StopService", "Access is denied.")
    started = _steps_started(operation)
    assert started == [], f"{operation} continued after control access denied: {started}"


def test_absence_message_without_winerror_1060_blocks_restore() -> None:
    scm = _bind_running_pair()
    scm.query_error[MEDIA] = RuntimeError("The specified service does not exist as an installed service.")
    started = _steps_started("restore")
    assert started == [], f"restore treated a non-1060 error as a missing service: {started}"


def test_winerror_1060_without_winerror_attribute_is_tolerated() -> None:
    class ArgsOnlyMissing(Exception):
        def __init__(self) -> None:
            super().__init__(ERROR_SERVICE_DOES_NOT_EXIST, "QueryServiceStatus", "missing")

    scm = _bind_running_pair()
    scm.status.clear()

    def missing() -> ArgsOnlyMissing:
        return ArgsOnlyMissing()

    scm.query_error[MEDIA] = missing()
    scm.query_error[CONTROL] = missing()
    scm.remove_error[MEDIA] = missing()
    scm.remove_error[CONTROL] = missing()
    assert _steps_started("purge") == ["dropdb"]


def test_remove_access_denied_does_not_finish_uninstall() -> None:
    scm = _bind_running_pair()
    scm.status = {MEDIA: scm.stopped, CONTROL: scm.stopped}
    scm.remove_error[MEDIA] = ScmFailure(ERROR_ACCESS_DENIED, "RemoveService", "Access is denied.")
    started = _steps_started("uninstall")
    assert started == [], f"uninstall continued after RemoveService access denied: {started}"
    assert MEDIA in scm.status
    assert CONTROL in scm.status


def test_query_failure_during_removal_does_not_finish_uninstall() -> None:
    scm = _bind_running_pair()
    scm.status = {MEDIA: scm.stopped, CONTROL: scm.stopped}
    scm.linger_after_remove = True
    removal_query_failure = ScmFailure(ERROR_SERVICE_SPECIFIC, "QueryServiceStatus", "service query failed")
    scm.query_error_during_removal[MEDIA] = removal_query_failure
    scm.query_error_during_removal[CONTROL] = ScmFailure(
        ERROR_SERVICE_SPECIFIC,
        "QueryServiceStatus",
        "service query failed",
    )
    started = _steps_started("uninstall")
    assert started == [], f"uninstall treated a removal query failure as success: {started}"
    assert not any(call[0] == "remove" and call[1] == CONTROL for call in scm.calls)


def test_removal_timeout_still_blocks_uninstall(monkeypatch: pytest.MonkeyPatch) -> None:
    scm = _bind_running_pair()
    scm.status = {MEDIA: scm.stopped, CONTROL: scm.stopped}
    scm.linger_after_remove = True
    clock = {"now": 0.0}

    def monotonic() -> float:
        clock["now"] += 31.0
        return clock["now"]

    monkeypatch.setattr(service_manager.time, "monotonic", monotonic)
    monkeypatch.setattr(service_manager.time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match="service removal did not complete: IntelligentVMSMedia"):
        service_manager.remove()
    assert CONTROL in scm.status


def test_cli_stop_does_not_return_success_after_access_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    scm = _bind_running_pair()
    _fail_control_stop(scm, "access_denied")
    monkeypatch.setattr(sys, "argv", ["service_manager.py", "stop"])
    try:
        service_manager.main()
    except Exception as exc:
        assert type(exc).__name__ == "ServiceMaintenanceError"
        return
    pytest.fail("stop CLI returned success after SCM access denied")


def test_restore_aborts_before_pg_restore() -> None:
    restore = _read("deploy/windows/Vms-Windows.ps1").split('"Restore" {', 1)[1].split('"Diagnostics" {', 1)[0]
    marker = "VMS service stop failed; restore aborted before database changes"
    assert marker in restore
    assert restore.index(marker) < restore.index("pg_restore.exe")


def test_purge_aborts_before_dropdb() -> None:
    purge = _read("deploy/windows/Vms-Windows.ps1").split('"PurgeDatabase" {', 1)[1]
    marker = "VMS service stop failed; database purge aborted"
    assert marker in purge
    assert purge.index(marker) < purge.index("dropdb.exe")


def test_uninstall_aborts_before_reporting_success() -> None:
    uninstall = _read("deploy/windows/Vms-Windows.ps1").split('"Uninstall" {', 1)[1].split('"PurgeDatabase" {', 1)[0]
    stop_marker = "VMS service stop failed; uninstall aborted"
    remove_marker = "VMS service removal failed; uninstall aborted"
    assert stop_marker in uninstall
    assert remove_marker in uninstall
    assert uninstall.index(stop_marker) < uninstall.index(" remove ")
    assert uninstall.index(remove_marker) < uninstall.index("windows_field_test_uninstall_ok")


def test_upgrade_aborts_before_application_replacement() -> None:
    installer = _read("deploy/windows/Install-WindowsFieldTest.ps1")
    marker = "VMS service stop failed; upgrade aborted before application replacement"
    assert marker in installer
    assert installer.index(marker) < installer.index("Remove-Item -LiteralPath $AppRoot")


def test_setup_uninstall_aborts_before_binary_removal() -> None:
    setup = _read("deploy/windows/installer/Invoke-Setup.ps1")
    function = setup.split("function Uninstall-Managed", 1)[1].split("\ntry{", 1)[0]
    marker = "uninstall aborted before binary removal"
    assert marker in function
    assert function.index(marker) < function.index("Remove-Item")
