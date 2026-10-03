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


def _service_exe() -> str:
    """Locate pywin32's service host using its supported environment-aware resolver."""
    candidate = Path(win32serviceutil.LocatePythonServiceExe())
    if not candidate.is_file():
        raise RuntimeError(f"pythonservice.exe missing: {candidate}")
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


def remove() -> None:
    """Remove VMS services only; persistent product data remains untouched."""
    for name in reversed(SERVICES):
        try:
            win32serviceutil.RemoveService(name)
        except Exception:
            continue
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            try:
                win32serviceutil.QueryServiceStatus(name)
            except Exception:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError(f"service removal did not complete: {name}")


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
    """Stop media then control service and wait for each to stop."""
    for name in reversed(SERVICES):
        try:
            current = win32serviceutil.QueryServiceStatus(name)[1]
            if current != win32service.SERVICE_STOPPED:
                win32serviceutil.StopService(name)
                win32serviceutil.WaitForServiceStatus(name, win32service.SERVICE_STOPPED, 30)
        except Exception:
            pass


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
