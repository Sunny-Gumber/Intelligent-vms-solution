"""Windows Service Control Manager hosts for the native field-test VMS."""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.request import urlopen

import servicemanager
import win32event
import win32service
import win32serviceutil


def _root() -> Path:
    """Return the protected Windows product-data root."""
    return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "IntelligentVMS"


def _load_env() -> dict[str, str]:
    """Load protected VMS runtime settings into the service process environment."""
    path = _root() / "config" / "vms.env"
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"')
        os.environ[key.strip()] = values[key.strip()]
    return values


def _configure_file_logging(name: str) -> None:
    """Configure one bounded-location service log under ProgramData."""
    import logging

    path = _root() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=path / f"{name}.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _new_scm_uvicorn_server(config):
    """Create a Uvicorn server without process-signal ownership under SCM."""
    import uvicorn

    class ScmUvicornServer(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):
            # SCM owns service control delivery. pywin32 invokes SvcDoRun on a
            # worker thread where Python signal handlers are not permitted.
            yield

    return ScmUvicornServer(config)


def _wait_control_jwks(timeout_seconds: float = 60.0) -> None:
    """Wait until the control API can serve MediaMTX signing trust."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urlopen("http://127.0.0.1:8000/internal/v1/media/jwks", timeout=2.0) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(1.0)
    raise RuntimeError("control API JWKS did not become ready")


class ControlService(win32serviceutil.ServiceFramework):
    """Run the shared FastAPI control plane as a Windows service."""

    _svc_name_ = "IntelligentVMSControl"
    _svc_display_name_ = "Intelligent VMS Control API"
    _svc_description_ = "Intelligent VMS small-site control API and browser UI."

    def __init__(self, args):
        """Initialize the SCM service object and cooperative stop event."""
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.server = None

    def SvcStop(self):
        """Request graceful Uvicorn shutdown."""
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        if self.server is not None:
            self.server.should_exit = True
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        """Load protected configuration and run the shared FastAPI application."""
        _load_env()
        _configure_file_logging("control-api")
        control_root = _root() / "app" / "services" / "control-api"
        sys.path.insert(0, str(control_root))
        os.chdir(control_root)
        import uvicorn

        self.server = _new_scm_uvicorn_server(
            uvicorn.Config(
                "app.main:app",
                host="127.0.0.1",
                port=8000,
                log_config=None,
                access_log=False,
            )
        )
        servicemanager.LogInfoMsg("Intelligent VMS control API starting")
        # pywin32 invokes SvcDoRun on a service worker thread. On Windows,
        # ProactorEventLoop construction calls signal.set_wakeup_fd(), which is
        # restricted to the main interpreter thread. The control plane is
        # socket-based, so use the selector loop explicitly for SCM hosting.
        asyncio.run(self.server.serve(), loop_factory=asyncio.SelectorEventLoop)
        servicemanager.LogInfoMsg("Intelligent VMS control API stopped")


class MediaService(win32serviceutil.ServiceFramework):
    """Run the native MediaMTX Windows binary as a supervised service."""

    _svc_name_ = "IntelligentVMSMedia"
    _svc_display_name_ = "Intelligent VMS Media"
    _svc_description_ = "Intelligent VMS MediaMTX live, recording and playback service."

    def __init__(self, args):
        """Initialize the MediaMTX process supervisor."""
        super().__init__(args)
        self.stop_event = threading.Event()
        self.process: subprocess.Popen | None = None
        self.log_handle = None

    def _stop_process(self) -> None:
        """Stop the MediaMTX child with a bounded graceful-to-forceful sequence."""
        process = self.process
        if not process or process.poll() is not None:
            return
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
            process.wait(timeout=10)
        except Exception:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                process.kill()
                process.wait(timeout=5)

    def SvcStop(self):
        """Request a bounded MediaMTX shutdown."""
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.stop_event.set()
        self._stop_process()

    def SvcDoRun(self):
        """Wait for control trust, then supervise native MediaMTX until SCM stop."""
        _load_env()
        _wait_control_jwks()
        root = _root()
        logs = root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        executable = root / "runtime" / "mediamtx" / "mediamtx.exe"
        config = root / "config" / "mediamtx.yml"
        self.log_handle = (logs / "mediamtx.log").open("ab", buffering=0)
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess,
            "CREATE_NO_WINDOW",
            0,
        )
        self.process = subprocess.Popen(
            [str(executable), str(config)],
            cwd=executable.parent,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        servicemanager.LogInfoMsg("Intelligent VMS MediaMTX starting")
        try:
            while not self.stop_event.wait(1.0):
                code = self.process.poll()
                if code is not None:
                    raise RuntimeError(f"MediaMTX exited unexpectedly with code {code}")
        finally:
            self._stop_process()
            self.log_handle.close()
        servicemanager.LogInfoMsg("Intelligent VMS MediaMTX stopped")
