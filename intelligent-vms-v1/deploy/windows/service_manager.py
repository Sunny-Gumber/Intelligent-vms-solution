#!/usr/bin/env python3
"""Install and manage the Intelligent VMS native Windows services."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import win32service
import win32serviceutil


SERVICES = ("IntelligentVMSControl", "IntelligentVMSMedia")
# Win32 ERROR_SERVICE_DOES_NOT_EXIST. Message text is not trusted: only this
# code means the service is absent. Access denied, query failure, and timeout
# leave the service state unknown and must abort destructive maintenance.
ERROR_SERVICE_DOES_NOT_EXIST = 1060
_SERVICE_CONTROL_TIMEOUT_SECONDS = 30
_SERVICE_REMOVAL_POLL_SECONDS = 0.25


class ServiceMaintenanceError(RuntimeError):
    """Raised when Windows SCM cannot prove a VMS service is stopped or removed.

    Restore, purge, upgrade, and uninstall must abort on this error. A missing
    service is not this error: Win32 error 1060 is a tolerated absence.
    """


def _service_exe() -> str:
    """Return the VMS-owned pywin32 service host prepared by the installer."""
    candidate = Path(sys.prefix) / "pythonservice.exe"
    if not candidate.is_file():
        raise RuntimeError(f"VMS-owned pythonservice.exe missing: {candidate}")
    return str(candidate)


def install(postgres_service: str) -> None:
    """Install both automatic Windows services with explicit dependencies."""
    executable = _service_exe()
    win32serviceutil.InstallService(
        "deploy.windows.service_host.ControlService",
        "IntelligentVMSControl",
        "Intelligent VMS Control API",
        startType=win32service.SERVICE_AUTO_START,
        serviceDeps=[postgres_service],
        exeName=executable,
        description="Intelligent VMS small-site control API and browser UI.",
        delayedstart=True,
    )
    win32serviceutil.InstallService(
        "deploy.windows.service_host.MediaService",
        "IntelligentVMSMedia",
        "Intelligent VMS Media",
        startType=win32service.SERVICE_AUTO_START,
        serviceDeps=["IntelligentVMSControl"],
        exeName=executable,
        description="Intelligent VMS native MediaMTX live, recording and playback service.",
        delayedstart=True,
    )


def _winerror(exc: BaseException) -> int | None:
    code = getattr(exc, "winerror", None)
    if type(code) is int:
        return code
    args = getattr(exc, "args", ())
    if args and type(args[0]) is int:
        return args[0]
    return None


def _is_missing_service(exc: BaseException) -> bool:
    return _winerror(exc) == ERROR_SERVICE_DOES_NOT_EXIST


def _stop_service(name: str) -> None:
    try:
        current = win32serviceutil.QueryServiceStatus(name)[1]
    except Exception as exc:
        if _is_missing_service(exc):
            return
        raise ServiceMaintenanceError(f"service query failed: {name}") from exc
    if current == win32service.SERVICE_STOPPED:
        return
    try:
        win32serviceutil.StopService(name)
    except Exception as exc:
        if _is_missing_service(exc):
            return
        raise ServiceMaintenanceError(f"service stop failed: {name}") from exc
    try:
        win32serviceutil.WaitForServiceStatus(name, win32service.SERVICE_STOPPED, _SERVICE_CONTROL_TIMEOUT_SECONDS)
    except Exception as exc:
        if _is_missing_service(exc):
            return
        raise ServiceMaintenanceError(f"service stop timed out: {name}") from exc
    try:
        observed = win32serviceutil.QueryServiceStatus(name)[1]
    except Exception as exc:
        if _is_missing_service(exc):
            return
        raise ServiceMaintenanceError(f"service query failed after stop: {name}") from exc
    if observed != win32service.SERVICE_STOPPED:
        raise ServiceMaintenanceError(f"service still running after stop: {name}")


def _remove_service(name: str) -> None:
    try:
        win32serviceutil.RemoveService(name)
    except Exception as exc:
        if _is_missing_service(exc):
            return
        raise ServiceMaintenanceError(f"service removal failed: {name}") from exc
    deadline = time.monotonic() + _SERVICE_CONTROL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            win32serviceutil.QueryServiceStatus(name)
        except Exception as exc:
            if _is_missing_service(exc):
                return
            raise ServiceMaintenanceError(f"service query failed during removal: {name}") from exc
        time.sleep(_SERVICE_REMOVAL_POLL_SECONDS)
    raise ServiceMaintenanceError(f"service removal did not complete: {name}")


def remove() -> None:
    """Remove VMS services only; persistent product data remains untouched.

    A missing service is ignored. Access denied, query failure, and timeout raise
    ServiceMaintenanceError so uninstall cannot continue while a service remains.
    """
    for name in reversed(SERVICES):
        _remove_service(name)


def start() -> None:
    """Start control then media service and wait for each to run."""
    for name in SERVICES:
        try:
            current = win32serviceutil.QueryServiceStatus(name)[1]
        except Exception:
            current = win32service.SERVICE_STOPPED
        if current != win32service.SERVICE_RUNNING:
            win32serviceutil.StartService(name)
            win32serviceutil.WaitForServiceStatus(name, win32service.SERVICE_RUNNING, 30)


def stop() -> None:
    """Stop media then control and wait until each is stopped.

    A missing service is ignored. Access denied, query failure, and timeout raise
    ServiceMaintenanceError so restore, purge, upgrade, and uninstall cannot
    continue while a service is still running.
    """
    for name in reversed(SERVICES):
        _stop_service(name)


def status() -> dict[str, int]:
    """Return current SCM status codes for both VMS services."""
    return {name: win32serviceutil.QueryServiceStatus(name)[1] for name in SERVICES}


def main() -> None:
    """Dispatch service lifecycle operations."""
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["install", "remove", "start", "stop", "restart", "status"])
    parser.add_argument("--postgres-service", default="postgresql-x64-17")
    args = parser.parse_args()
    if args.action == "install":
        install(args.postgres_service)
    elif args.action == "remove":
        remove()
    elif args.action == "start":
        start()
    elif args.action == "stop":
        stop()
    elif args.action == "restart":
        stop()
        start()
    else:
        for name, value in status().items():
            print(f"{name}={value}")


if __name__ == "__main__":
    main()
