"""VMS-FIX-150 residuals for the shared atomic publish helper.

These tests cover the three lows left after PR #138: a non-regular CSV
destination must be refused before JSON is replaced, a ``sample()`` that
never returns must stay inside the reconnect shutdown bound, and publishing
must succeed when ``os.fchmod`` is absent. They use temporary directories,
monkeypatches, and a child interpreter. They do not open a network connection
and they do not report a hardware qualification.
"""

from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

TOOLS = Path(__file__).parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))


SHUTDOWN_BOUND_SECONDS = 0.25
CHILD_TIMEOUT_SECONDS = 8.0
FIFO_JOIN_SECONDS = 1.5


def _helper():
    """Import the shared publisher, failing the test when it is absent.

    Returns:
        The ``phase8_atomic_publish`` module.

    Raises:
        pytest.fail: The helper module cannot be imported.
    """
    try:
        import phase8_atomic_publish as helper
    except ImportError as exc:
        pytest.fail(f"phase8_atomic_publish is missing: {exc}")
    return helper


def _write_json(_result: object, json_path: str, csv_path: object) -> None:
    """Write a marker payload so a committed JSON file is obvious.

    Args:
        _result: Unused benchmark record.
        json_path: Staging or destination path the helper asked to write.
        csv_path: Must stay None. The helper publishes CSV itself.

    Returns:
        None after the marker text is on disk.

    Raises:
        AssertionError: The helper asked this callback to write the CSV.
    """
    assert csv_path is None
    Path(json_path).write_text("PUBLISHED_JSON", encoding="utf-8")


def _run_tool(script: str, *args: str, timeout: float = CHILD_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    """Run a child interpreter with the tools directory on its path.

    Args:
        script: Python source. ``sys.argv[1]`` is the tools directory.
        *args: Extra arguments after the tools directory.
        timeout: Seconds before the child is killed.

    Returns:
        The completed process.

    Raises:
        AssertionError: The child did not exit within ``timeout``.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(TOOLS), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        raise AssertionError(f"child hung\nstdout={stdout}\nstderr={stderr}")
    return subprocess.CompletedProcess(proc.args, proc.returncode or 0, stdout, stderr)


def test_publish_file_leaves_a_fifo_destination_in_place(tmp_path: Path) -> None:
    """Replacing onto a FIFO is refused and the FIFO inode stays put."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("mkfifo is unavailable; FIFO refusal is verified on Linux CI")
    helper = _helper()
    destination = tmp_path / "evidence.json"
    os.mkfifo(destination)
    with pytest.raises(RuntimeError, match="not a regular file"):
        helper.publish_file(destination, lambda staging: staging.write_text("NEW", encoding="utf-8"))
    assert stat.S_ISFIFO(destination.lstat().st_mode)
    assert list(tmp_path.glob("*.partial")) == []


def test_fifo_csv_is_refused_before_json_is_published(tmp_path: Path) -> None:
    """A FIFO at the CSV path must not commit JSON and must not block the read."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("mkfifo is unavailable; FIFO refusal is verified on Linux CI")
    helper = _helper()
    json_path = tmp_path / "evidence.json"
    csv_path = tmp_path / "evidence.csv"
    json_path.write_text("ORIGINAL_JSON", encoding="utf-8")
    os.mkfifo(csv_path)
    outcome: dict[str, BaseException] = {}

    def attempt() -> None:
        try:
            helper.publish_benchmark_evidence(
                {},
                json_path,
                str(csv_path),
                write_result=_write_json,
            )
        except BaseException as exc:
            outcome["exc"] = exc

    worker = threading.Thread(target=attempt, name="vms-fifo-csv-publish", daemon=True)
    worker.start()
    worker.join(FIFO_JOIN_SECONDS)
    assert not worker.is_alive(), json_path.read_text(encoding="utf-8")
    assert isinstance(outcome["exc"], RuntimeError)
    assert "not a regular file" in str(outcome["exc"])
    assert json_path.read_text(encoding="utf-8") == "ORIGINAL_JSON"
    assert stat.S_ISFIFO(csv_path.lstat().st_mode)
    assert list(tmp_path.glob("*.partial")) == []


def test_socket_csv_is_refused_before_json_is_published(tmp_path: Path) -> None:
    """A Unix socket at the CSV path is refused before the JSON replace."""
    if os.name != "posix" or not hasattr(socket, "AF_UNIX"):
        pytest.skip("Unix-socket destinations are verified on Linux CI")
    helper = _helper()
    json_path = tmp_path / "evidence.json"
    csv_path = tmp_path / "evidence.csv"
    json_path.write_text("ORIGINAL_JSON", encoding="utf-8")
    held = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        held.bind(os.fspath(csv_path))
        with pytest.raises(RuntimeError, match="not a regular file"):
            helper.publish_benchmark_evidence(
                {},
                json_path,
                os.fspath(csv_path),
                write_result=_write_json,
            )
        assert json_path.read_text(encoding="utf-8") == "ORIGINAL_JSON"
        assert stat.S_ISSOCK(csv_path.lstat().st_mode)
        assert list(tmp_path.glob("*.partial")) == []
    finally:
        held.close()


def test_blocking_sample_fails_inside_shutdown_bound(tmp_path: Path) -> None:
    """A sample() that never returns must fail inside the reconnect shutdown bound."""
    script = (
        "import asyncio\n"
        "import logging\n"
        "import sys\n"
        "import threading\n"
        "from types import SimpleNamespace\n"
        "logging.basicConfig(level=logging.ERROR)\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import phase8_reconnect_benchmark as bench\n"
        "bench._SHUTDOWN_JOIN_SECONDS = "
        + str(SHUTDOWN_BOUND_SECONDS)
        + "\n"
        "def sample(self):\n"
        "    threading.Event().wait()\n"
        "    return {}\n"
        "bench.SystemSampler.sample = sample\n"
        "async def healthy(*_args, **_kwargs):\n"
        "    return True, 0.0\n"
        "bench.attempt = healthy\n"
        "async def body():\n"
        "    args = SimpleNamespace(host='127.0.0.1', port=9, attempts=1, warmup_attempts=0,\n"
        "                           concurrency=1, timeout=0.2, sample_interval=30.0)\n"
        "    await bench.run(args)\n"
        "runner = getattr(bench, '_run_benchmark', None)\n"
        "if runner is None:\n"
        "    runner = asyncio.run\n"
        "runner(body())\n"
    )
    output = tmp_path / "reconnect.json"
    output.write_text("ORIGINAL_JSON", encoding="utf-8")
    started = time.monotonic()
    completed = _run_tool(script, timeout=CHILD_TIMEOUT_SECONDS)
    elapsed = time.monotonic() - started
    assert elapsed < CHILD_TIMEOUT_SECONDS
    assert completed.returncode != 0
    assert "ignored cancellation" in completed.stderr
    assert "ok=" not in completed.stdout
    assert output.read_text(encoding="utf-8") == "ORIGINAL_JSON"


def test_publish_succeeds_when_fchmod_is_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Publishing must replace the destination when the platform has no fchmod."""
    helper = _helper()
    if hasattr(helper.os, "fchmod"):
        monkeypatch.delattr(helper.os, "fchmod")
    destination = tmp_path / "evidence.json"
    destination.write_text("ORIGINAL", encoding="utf-8")
    helper.publish_file(destination, lambda staging: staging.write_text("NEW", encoding="utf-8"))
    assert destination.read_text(encoding="utf-8") == "NEW"
    assert list(tmp_path.glob("*.partial")) == []
    if os.name == "nt":
        _assert_current_user_only_dacl(destination)
    else:
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def _assert_current_user_only_dacl(path: Path) -> None:
    """Require a protected DACL whose only ACE is the current user.

    Args:
        path: Evidence file published on Windows.

    Returns:
        None when the DACL matches that owner-only shape.

    Raises:
        AssertionError: The DACL is missing, inherited, or grants another SID.
        OSError: The security descriptor or process token could not be read.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_ulong,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetLengthSid.argtypes = (ctypes.c_void_p,)
    advapi32.GetLengthSid.restype = wintypes.DWORD
    advapi32.CopySid.argtypes = (wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p)
    advapi32.CopySid.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    advapi32.EqualSid.restype = wintypes.BOOL
    advapi32.GetNamedSecurityInfoW.argtypes = (
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    )
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetAclInformation.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_ulong,
    )
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = (ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
    advapi32.GetAce.restype = wintypes.BOOL

    class _AclSize(ctypes.Structure):
        _fields_ = (
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        )

    class _AceHeader(ctypes.Structure):
        _fields_ = (
            ("AceType", ctypes.c_ubyte),
            ("AceFlags", ctypes.c_ubyte),
            ("AceSize", ctypes.c_ushort),
        )

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if ctypes.get_last_error() != 122:
            raise ctypes.WinError(ctypes.get_last_error())
        raw = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(token, 1, raw, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_ptr = ctypes.cast(raw, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_len = advapi32.GetLengthSid(sid_ptr)
        current = ctypes.create_string_buffer(sid_len)
        if not advapi32.CopySid(sid_len, current, sid_ptr):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(token)

    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    status = advapi32.GetNamedSecurityInfoW(
        os.fspath(path),
        1,
        0x4,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if status != 0:
        raise ctypes.WinError(status)
    try:
        info = _AclSize()
        if not advapi32.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2):
            raise ctypes.WinError(ctypes.get_last_error())
        detail = _icacls_text(path)
        assert info.AceCount == 1, detail
        ace = ctypes.c_void_p()
        if not advapi32.GetAce(dacl, 0, ctypes.byref(ace)):
            raise ctypes.WinError(ctypes.get_last_error())
        header = _AceHeader.from_address(ace.value)
        assert header.AceType == 0, detail
        assert header.AceFlags & 0x10 == 0, detail
        ace_sid = ctypes.c_void_p(ace.value + 8)
        assert advapi32.EqualSid(ace_sid, current), detail
    finally:
        if descriptor.value:
            kernel32.LocalFree(descriptor)


def _icacls_text(path: Path) -> str:
    """Return ``icacls`` output for a failed Windows DACL assertion.

    Args:
        path: File whose DACL was rejected.

    Returns:
        Combined stdout and stderr. Empty when ``icacls`` cannot be started.
    """
    try:
        completed = subprocess.run(
            ["icacls", os.fspath(path)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return str(exc)
    return (completed.stdout or "") + (completed.stderr or "")
