"""Filesystem path confinement.

Any path derived from user input (export ids, segment names, volume paths,
diagnostic bundle names) is resolved and then verified to be inside an allowed
root. This blocks `../` traversal, absolute-path injection and symlink escape,
which in a VMS would otherwise let an operator export /etc/shadow or let
retention delete arbitrary files.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class PathConfinementError(ValueError):
    def __init__(self, message: str, code: str = "path_not_allowed") -> None:
        super().__init__(message)
        self.code = code


def safe_name(name: str) -> str:
    """Validate a single path component (no separators, no dot-dot)."""
    if not SAFE_NAME.match(name or "") or name in {".", ".."}:
        raise PathConfinementError(f"{name!r} is not a safe file name", "invalid_name")
    return name


def resolve_within(root: Path, *parts: str) -> Path:
    """Join `parts` under `root` and guarantee the result stays inside it."""
    root_resolved = Path(root).resolve()
    candidate = root_resolved
    for part in parts:
        if part is None:
            raise PathConfinementError("path component is None", "invalid_name")
        text = str(part)
        if os.path.isabs(text) or text.startswith("\\\\"):
            raise PathConfinementError("absolute paths are not accepted here", "absolute_path")
        for component in Path(text).parts:
            if component in {"..", "."}:
                raise PathConfinementError("path traversal is not permitted", "traversal")
            candidate = candidate / component
    resolved = candidate.resolve()
    if not _is_relative_to(resolved, root_resolved):
        raise PathConfinementError(
            f"resolved path escapes its root ({root_resolved})", "escaped_root")
    return resolved


def assert_within(root: Path, candidate: Path) -> Path:
    root_resolved = Path(root).resolve()
    resolved = Path(candidate).resolve()
    if not _is_relative_to(resolved, root_resolved):
        raise PathConfinementError(
            f"{resolved} is outside the permitted root {root_resolved}", "escaped_root")
    return resolved


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def ensure_writable_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".vms-write-test"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        raise PathConfinementError(f"{path} is not writable: {exc}", "not_writable") from exc
    return path


def atomic_replace(tmp: Path, final: Path) -> Path:
    """Finalize a file by atomic rename on the same filesystem.

    Used by the recorder so a partially written segment is never indexed, and
    by the export writer so a cancelled export never looks complete.
    """
    tmp, final = Path(tmp), Path(final)
    final.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, final)
    return final
