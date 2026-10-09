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

# Owner read/write. mkstemp requests this mode, and fchmod applies it again so
# a non-zero umask cannot leave the evidence file group- or world-readable.
EVIDENCE_FILE_MODE = 0o600

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
        destination: Final path. A real directory is refused and left in place.
        write_staging: Callback that writes the complete payload to the
            temporary path. It must not replace ``destination`` itself.

    Returns:
        None after ``destination`` names the temporary file's inode.

    Raises:
        RuntimeError: ``destination`` is a real directory. Nothing is removed.
        OSError: The temporary file could not be created, written, fsynced, or
            replaced. The original error propagates. When the temporary file
            could not be unlinked, that cleanup error is logged and noted on
            the original exception, and the temporary file is left on disk.
        Exception: Propagates any other error from ``write_staging`` after the
            same temporary-file cleanup.
    """
    if is_real_directory(destination):
        raise RuntimeError(f"benchmark output path is a directory: {destination}")
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

    The JSON document is written to its own temporary file and replaced first.
    When ``csv_path`` is set, the CSV document is built from the bytes already
    at that path plus one row, written to a second temporary file, and replaced
    second. Neither replace is followed by a write to that destination. A CSV
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
        RuntimeError: A destination is a real directory.
        OSError: A temporary file could not be published. Cleanup does not mask
            this error and does not unlink a file that ``os.replace`` already
            committed.
        Exception: Validation or writer failures from ``write_result``.
    """

    def write_json(staging: Path) -> None:
        write_result(result, json_path=str(staging), csv_path=None)

    publish_file(json_path, write_json)
    if not csv_path:
        return
    csv_destination = Path(csv_path)

    def write_csv(staging: Path) -> None:
        existing = _read_text_if_file(csv_destination)
        # newline="" keeps the CSV module's \r\n bytes. The default translation
        # would turn those endings into the platform line separator.
        staging.write_text(render_appended_csv(existing, result), encoding="utf-8", newline="")

    publish_file(csv_destination, write_csv)


def _create_private_temp(destination: Path) -> Path:
    """Create an exclusive mode-``0600`` temporary file beside ``destination``.

    Args:
        destination: Final path whose parent directory receives the temp file.
            The name is ``<destination>.<random>.partial`` and is not the legacy
            shared ``<destination>.partial`` path.

    Returns:
        The new file path. The caller owns it until ``os.replace`` or unlink.

    Raises:
        OSError: The file could not be created exclusively or its mode could
            not be set. A file created before the mode change is unlinked when
            that unlink succeeds.
    """
    descriptor, name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f"{destination.name}.",
        suffix=".partial",
    )
    temp_path = Path(name)
    try:
        os.fchmod(descriptor, EVIDENCE_FILE_MODE)
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

    Args:
        directory: Directory that contains the replaced evidence file.

    Returns:
        None after ``os.fsync`` returns.

    Raises:
        OSError: The directory could not be opened or fsynced.
    """
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
        RuntimeError: ``path`` is a real directory.
        OSError: A regular file could not be read.
        UnicodeError: The file is not valid UTF-8.
    """
    if is_real_directory(path):
        raise RuntimeError(f"benchmark output path is a directory: {path}")
    if path.is_symlink() or not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


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
