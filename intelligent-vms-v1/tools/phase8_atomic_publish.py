"""Fail-closed atomic publish for Phase 8 benchmark evidence files.

Each output is written to a private ``tempfile.mkstemp`` file in the
destination directory, forced to mode ``0600``, fsynced, and then moved with
``os.replace``. The destination directory is fsynced after that replace.
Nothing is written to a destination after its replace, so a later CSV publish
cannot truncate a JSON file that already committed.

On failure the helper unlinks only the temporary file it still owns. A cleanup
error is logged and attached to the original exception; it does not replace
that exception.

A process killed with ``SIGKILL`` while the temporary file is open never runs
the cleanup handler. The next invocation does not search the directory or
delete arbitrary files to remove that leftover. The operator can delete the
named temporary file by hand.

A directory, FIFO, socket, or device at either destination is refused before
any file is replaced. A symlink is replaced and is not followed. Unix forces
mode ``0600`` with ``os.fchmod`` on the temporary descriptor. Windows has no
``fchmod``; the temporary file's inherited DACL is replaced with one ACE for
the current user before any payload byte is written.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

# Owner read/write. mkstemp requests this mode. Where os.fchmod exists it is
# applied again on the open descriptor. Windows has no fchmod; a current-user
# DACL is applied instead so the inherited directory ACL cannot widen the file.
EVIDENCE_FILE_MODE = 0o600

# Win32 constants for the no-fchmod permission and directory-flush paths.
_WIN_TOKEN_QUERY = 0x0008
_WIN_TOKEN_USER = 1
_WIN_ERROR_INSUFFICIENT_BUFFER = 122
_WIN_ACL_REVISION = 2
_WIN_SE_FILE_OBJECT = 1
_WIN_DACL_SECURITY_INFORMATION = 0x4
_WIN_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_WIN_FILE_GENERIC_READ = 0x120089
_WIN_FILE_GENERIC_WRITE = 0x120116
_WIN_DELETE = 0x00010000
_WIN_FILE_SHARE_READ_WRITE_DELETE = 0x1 | 0x2 | 0x4
_WIN_OPEN_EXISTING = 3
_WIN_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WIN_GENERIC_READ = 0x80000000
_WIN_INVALID_HANDLE = 0xFFFFFFFFFFFFFFFF

# Stable CSV column order. Dict insertion order is not the contract; writers
# emit these names from left to right on every row.
CSV_COLUMNS: tuple[str, ...] = (
    "benchmark_id",
    "commit_sha",
    "workload_type",
    "duration_seconds",
    "operations_ok",
    "operations_failed",
    "throughput_ops_s",
    "p50_ms",
    "p95_ms",
    "p99_ms",
    "cpu_p95_pct",
    "ram_p95_bytes",
    "net_rx_p95_mbps",
    "net_tx_p95_mbps",
    "disk_read_p95_mbps",
    "disk_write_p95_mbps",
    "gpu_measured",
)


def is_real_directory(path: Path) -> bool:
    """Report whether ``path`` is a directory and not a symlink.

    Args:
        path: Evidence path to inspect. A missing path is not a directory.

    Returns:
        True when ``lstat`` shows a directory. Symlinks are False even when
        their target is a directory, because unlink and ``os.replace`` replace
        the link itself.

    Raises:
        OSError: ``lstat`` failed for a reason other than a missing path.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(info.st_mode)


def _require_replaceable_destination(path: Path) -> None:
    """Refuse a destination that must not be replaced or that can block on open.

    A missing path is replaceable. A symlink is replaceable because
    ``os.replace`` swaps the link and does not follow it. A regular file is
    replaceable. A directory, FIFO, socket, or device is not. Opening a FIFO
    to read an existing CSV can block after the JSON file is already committed,
    and replacing a device would remove that device node.

    Args:
        path: JSON or CSV destination to inspect.

    Returns:
        None when the path is missing, a symlink, or a regular file.

    Raises:
        RuntimeError: The path is a directory or another non-regular file.
            Nothing is removed.
        OSError: ``lstat`` failed for a reason other than a missing path.
    """
    if is_real_directory(path):
        raise RuntimeError(f"benchmark output path is a directory: {path}")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or stat.S_ISREG(info.st_mode):
        return
    raise RuntimeError(f"benchmark output path is not a regular file: {path}")


def benchmark_csv_row(result: dict[str, Any]) -> dict[str, Any]:
    """Build the CSV row for one validated benchmark result.

    Args:
        result: Benchmark result dictionary with the Phase 8 evidence keys.

    Returns:
        A row whose keys are exactly ``CSV_COLUMNS``, in that order.

    Raises:
        KeyError: A required evidence field is missing.
        TypeError: A required container is not a dictionary.
    """
    values = {
        "benchmark_id": result["benchmark_id"],
        "commit_sha": result["environment"]["commit_sha"],
        "workload_type": result["workload"]["type"],
        "duration_seconds": result["workload"]["duration_seconds"],
        "operations_ok": result["result"]["operations_ok"],
        "operations_failed": result["result"]["operations_failed"],
        "throughput_ops_s": result["result"]["throughput_ops_s"],
        "p50_ms": result["result"]["latency"]["p50_ms"],
        "p95_ms": result["result"]["latency"]["p95_ms"],
        "p99_ms": result["result"]["latency"]["p99_ms"],
        "cpu_p95_pct": result["resources"]["cpu_pct"]["p95"],
        "ram_p95_bytes": result["resources"]["ram_used_bytes"]["p95"],
        "net_rx_p95_mbps": result["resources"]["net_rx_mbps"]["p95"],
        "net_tx_p95_mbps": result["resources"]["net_tx_mbps"]["p95"],
        "disk_read_p95_mbps": result["resources"]["disk_read_mbps"]["p95"],
        "disk_write_p95_mbps": result["resources"]["disk_write_mbps"]["p95"],
        "gpu_measured": result["resources"]["gpu_measured"],
    }
    return {column: values[column] for column in CSV_COLUMNS}


def render_appended_csv(existing: str, result: dict[str, Any]) -> str:
    """Return ``existing`` plus one CSV row, without reordering prior lines.

    A new file starts with the ``CSV_COLUMNS`` header and one data row. A file
    that already has bytes keeps those bytes in order, including a non-CSV
    prefix, and the new row is appended after a line break. Columns on the new
    row always follow ``CSV_COLUMNS``.

    Args:
        existing: Current destination text. Empty when the destination is absent.
        result: Benchmark result dictionary to append.

    Returns:
        The full CSV document to write to the staging file.

    Raises:
        KeyError: ``result`` is missing a field required by the CSV row.
        TypeError: ``result`` is not shaped like a benchmark record.
    """
    row = benchmark_csv_row(result)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(CSV_COLUMNS), lineterminator="\r\n")
    if existing == "":
        writer.writeheader()
    writer.writerow(row)
    addition = buffer.getvalue()
    if existing == "":
        return addition
    if existing.endswith(("\n", "\r\n")):
        return existing + addition
    return existing + "\r\n" + addition


def publish_file(destination: Path, write_staging: Callable[[Path], None]) -> None:
    """Publish one file by fsync, ``os.replace``, and a directory fsync.

    ``write_staging`` receives a new mode-``0600`` temporary file in the
    destination directory and must finish writing it before it returns. This
    function then fsyncs that file, replaces ``destination``, and fsyncs the
    directory. It does not write ``destination`` again after the replace.

    Args:
        destination: Final path. A real directory, FIFO, socket, or device is
            refused and left in place. A symlink is replaced and not followed.
        write_staging: Callback that writes the complete payload to the
            temporary path. It must not replace ``destination`` itself.

    Returns:
        None after ``destination`` names the temporary file's inode.

    Raises:
        RuntimeError: ``destination`` is a real directory or another
            non-regular, non-symlink file. Nothing is removed.
        OSError: The temporary file could not be created, written, fsynced, or
            replaced. The original error propagates. When the temporary file
            could not be unlinked, that cleanup error is logged and noted on
            the original exception, and the temporary file is left on disk.
        Exception: Propagates any other error from ``write_staging`` after the
            same temporary-file cleanup.
    """
    _require_replaceable_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        temp_path = _create_private_temp(destination)
        write_staging(temp_path)
        _fsync_file(temp_path)
        os.replace(temp_path, destination)
        # The temporary name is gone. A later failure must not unlink destination.
        temp_path = None
        _fsync_directory(destination.parent)
    except BaseException as exc:
        cleanup_error = _discard_private_temp(temp_path)
        if cleanup_error is not None and temp_path is not None:
            note = f"temporary file remains at {temp_path}: {cleanup_error}"
            LOG.warning("%s", note)
            if hasattr(exc, "add_note"):
                exc.add_note(note)
        raise


def publish_benchmark_evidence(
    result: dict[str, Any],
    json_path: Path,
    csv_path: str | None,
    *,
    write_result: Callable[..., None],
) -> None:
    """Publish benchmark JSON, then the optional CSV, without rewriting JSON.

    Both destinations are inspected before the JSON replace. A directory, FIFO,
    socket, or device at either path is refused and neither file is replaced.
    The JSON document is then written to its own temporary file and replaced
    first. When ``csv_path`` is set, the CSV document is built from the bytes
    already at that path plus one row, written to a second temporary file, and
    replaced second. The existing CSV is opened with ``O_NONBLOCK`` where the
    platform has it, so a FIFO that appears in that window cannot block the
    process. Neither replace is followed by a write to that destination. A CSV
    failure after the JSON replace leaves the JSON file in place.

    Args:
        result: Completed benchmark result dictionary.
        json_path: Destination JSON evidence path.
        csv_path: Optional CSV summary path. None publishes JSON only.
        write_result: JSON writer, normally ``phase8_benchmark_common.write_result``.
            Called with ``csv_path=None`` so it cannot append the CSV or reopen
            the destination JSON.

    Returns:
        None after the requested files have been replaced.

    Raises:
        RuntimeError: A destination is a real directory, FIFO, socket, device,
            or other non-regular file. A symlink is allowed and is not followed.
        OSError: A temporary file could not be published. Cleanup does not mask
            this error and does not unlink a file that ``os.replace`` already
            committed.
        Exception: Validation or writer failures from ``write_result``.
    """
    csv_destination = Path(csv_path) if csv_path else None
    _require_replaceable_destination(json_path)
    if csv_destination is not None:
        _require_replaceable_destination(csv_destination)

    def write_json(staging: Path) -> None:
        write_result(result, json_path=str(staging), csv_path=None)

    publish_file(json_path, write_json)
    if csv_destination is None:
        return

    def write_csv(staging: Path) -> None:
        existing = _read_text_if_file(csv_destination)
        # newline="" keeps the CSV module's \r\n bytes. The default translation
        # would turn those endings into the platform line separator.
        staging.write_text(render_appended_csv(existing, result), encoding="utf-8", newline="")

    publish_file(csv_destination, write_csv)


def _create_private_temp(destination: Path) -> Path:
    """Create an exclusive owner-only temporary file beside ``destination``.

    Unix creates the file at mode ``0600`` and ``fchmod`` repeats that mode.
    Windows replaces the inherited DACL with a current-user ACE before the
    caller writes a payload.

    Args:
        destination: Final path whose parent directory receives the temp file.
            The name is ``<destination>.<random>.partial`` and is not the legacy
            shared ``<destination>.partial`` path.

    Returns:
        The new file path. The caller owns it until ``os.replace`` or unlink.

    Raises:
        OSError: The file could not be created exclusively or its mode or
            Windows DACL could not be set. A file created before that change
            is unlinked when that unlink succeeds.
    """
    descriptor, name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f"{destination.name}.",
        suffix=".partial",
    )
    temp_path = Path(name)
    try:
        _force_private_permissions(descriptor, temp_path)
    except BaseException:
        os.close(descriptor)
        _discard_private_temp(temp_path)
        raise
    os.close(descriptor)
    return temp_path


def _fsync_file(path: Path) -> None:
    """Fsync the bytes of a regular file.

    Args:
        path: Temporary evidence file to flush.

    Returns:
        None after ``os.fsync`` returns.

    Raises:
        OSError: The file could not be opened or fsynced.
    """
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(directory: Path) -> None:
    """Fsync a directory so the replaced directory entry itself is durable.

    Unix opens the directory and calls ``os.fsync``. Windows cannot open a
    directory that way; it opens the directory with backup semantics and
    flushes that handle.

    Args:
        directory: Directory that contains the replaced evidence file.

    Returns:
        None after the directory flush returns.

    Raises:
        OSError: The directory could not be opened or fsynced.
    """
    if os.name == "nt":
        _fsync_directory_windows(directory)
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_text_if_file(path: Path) -> str:
    """Read existing CSV text without following a symlink or opening a directory.

    Args:
        path: CSV destination. A missing path is an empty document.

    Returns:
        The file's UTF-8 text, or an empty string when the path is missing.
        A symlink contributes nothing: ``os.replace`` will swap the link, and
        the link target must stay unchanged.

    Raises:
        RuntimeError: ``path`` is a directory, FIFO, socket, device, or other
            non-regular file, including one that would block a normal open.
        OSError: A regular file could not be read.
        UnicodeError: The file is not valid UTF-8.
    """
    _require_replaceable_destination(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return ""
    if stat.S_ISLNK(info.st_mode):
        return ""
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"benchmark output path is not a regular file: {path}")
    return _read_regular_file_text(path)


def _read_regular_file_text(path: Path) -> str:
    """Read a regular file without blocking on a FIFO that raced into the path.

    Args:
        path: Path that ``lstat`` just reported as a regular file.

    Returns:
        The file's UTF-8 text. Bytes are not newline-translated.

    Raises:
        RuntimeError: The opened inode is no longer a regular file, or a
            non-blocking read would have waited.
        OSError: The file could not be opened or read.
        UnicodeError: The file is not valid UTF-8.
    """
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError(f"benchmark output path is not a regular file: {path}")
        chunks: list[bytes] = []
        while True:
            try:
                block = os.read(descriptor, 1024 * 1024)
            except InterruptedError:
                continue
            except BlockingIOError as exc:
                raise RuntimeError(f"benchmark output path is not a regular file: {path}") from exc
            if not block:
                break
            chunks.append(block)
    finally:
        os.close(descriptor)
    return b"".join(chunks).decode("utf-8")


def _force_private_permissions(descriptor: int, path: Path) -> None:
    """Restrict a temporary evidence file to the current user.

    Unix calls ``os.fchmod`` on the open descriptor. Platforms without
    ``fchmod`` use ``os.chmod`` on the path ``mkstemp`` just created, except
    Windows, which replaces the inherited DACL. The descriptor stays open so
    the caller can close it after this returns.

    Args:
        descriptor: Open descriptor from ``tempfile.mkstemp``.
        path: Path of that same temporary file.

    Returns:
        None after the restriction is applied.

    Raises:
        OSError: The mode or DACL could not be set.
    """
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, EVIDENCE_FILE_MODE)
        return
    if os.name == "nt":
        _restrict_windows_dacl(path)
        return
    os.chmod(path, EVIDENCE_FILE_MODE)


def _restrict_windows_dacl(path: Path) -> None:
    """Replace a Windows file's DACL with one current-user read/write/delete ACE.

    ``mkstemp`` ignores the Unix mode on Windows and leaves the directory's
    inherited ACL. This runs before any payload byte is written. The DACL is
    protected so the parent directory cannot add inherited ACEs back.

    Args:
        path: Temporary file this helper still owns.

    Returns:
        None after ``SetNamedSecurityInfoW`` succeeds.

    Raises:
        OSError: The process token or the file DACL could not be updated.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
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
    advapi32.InitializeAcl.argtypes = (ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD)
    advapi32.InitializeAcl.restype = wintypes.BOOL
    advapi32.AddAccessAllowedAce.argtypes = (
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    )
    advapi32.AddAccessAllowedAce.restype = wintypes.BOOL
    advapi32.SetNamedSecurityInfoW.argtypes = (
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), _WIN_TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, _WIN_TOKEN_USER, None, 0, ctypes.byref(needed))
        if ctypes.get_last_error() != _WIN_ERROR_INSUFFICIENT_BUFFER:
            raise ctypes.WinError(ctypes.get_last_error())
        raw = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(token, _WIN_TOKEN_USER, raw, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_ptr = ctypes.cast(raw, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_length = advapi32.GetLengthSid(sid_ptr)
        sid = ctypes.create_string_buffer(sid_length)
        if not advapi32.CopySid(sid_length, sid, sid_ptr):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(token)

    # ACL header plus one access-allowed ACE. Extra bytes keep the ACE aligned.
    acl_size = 16 + sid_length + 32
    acl = ctypes.create_string_buffer(acl_size)
    if not advapi32.InitializeAcl(acl, acl_size, _WIN_ACL_REVISION):
        raise ctypes.WinError(ctypes.get_last_error())
    access = _WIN_FILE_GENERIC_READ | _WIN_FILE_GENERIC_WRITE | _WIN_DELETE
    if not advapi32.AddAccessAllowedAce(acl, _WIN_ACL_REVISION, access, sid):
        raise ctypes.WinError(ctypes.get_last_error())
    status = advapi32.SetNamedSecurityInfoW(
        os.fspath(path),
        _WIN_SE_FILE_OBJECT,
        _WIN_PROTECTED_DACL_SECURITY_INFORMATION | _WIN_DACL_SECURITY_INFORMATION,
        None,
        None,
        acl,
        None,
    )
    if status != 0:
        raise ctypes.WinError(status)


def _fsync_directory_windows(directory: Path) -> None:
    """Flush a directory handle on Windows after ``os.replace``.

    Args:
        directory: Directory that contains the replaced evidence file.

    Returns:
        None after ``FlushFileBuffers`` returns.

    Raises:
        OSError: The directory could not be opened or flushed.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.FlushFileBuffers.argtypes = (wintypes.HANDLE,)
    kernel32.FlushFileBuffers.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(
        os.fspath(directory),
        _WIN_GENERIC_READ,
        _WIN_FILE_SHARE_READ_WRITE_DELETE,
        None,
        _WIN_OPEN_EXISTING,
        _WIN_FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle is None or handle == _WIN_INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not kernel32.FlushFileBuffers(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)


def _discard_private_temp(path: Path | None) -> OSError | None:
    """Unlink a temporary file this helper created, without raising.

    Args:
        path: Temporary path still owned by this attempt, or None when the
            replace already consumed the name or no file was created.

    Returns:
        None when the path was absent or was removed. The ``OSError`` when the
        unlink failed, so the caller can keep the original publish error.

    Raises:
        None. Unlink failures are returned to the caller.
    """
    if path is None:
        return None
    try:
        os.unlink(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return exc
    return None
