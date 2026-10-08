"""VMS-FIX-034 POSIX regressions for field-test secret file creation.

The pre-fix generator writes the secret env file and only then chmod 0600.
Under umask 022 that leaves a group/world-readable file until chmod runs.
These tests observe mode at the first write and check interruption behavior.
They redirect OUTPUT into pytest's tmp dir and stub secret generators.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PATH = ROOT / "deploy" / "field-test" / "generate_env.py"
REAL_ENV = ROOT / ".env"
_STUB_SECRET = "stub-not-a-secret"
_STUB_RSA = "stub-rsa-material"

pytestmark = pytest.mark.skipif(
    os.name != "posix" or not os.path.isdir("/proc/self/fd"),
    reason=(
        "VMS-FIX-034 proves POSIX mode 0600 at the first secret write under umask 022 "
        "by observing /proc/self/fd. The Windows field-test env generator is a different "
        "file and is out of scope. This skip does not apply to existing tests."
    ),
)


class _FieldEnv:
    """Paths and loaded generator used by one tmp-dir case."""

    def __init__(self, module: object, output: Path, root: Path) -> None:
        self.module = module
        self.output = output
        self.root = root


def _load_generator():
    """Load the field-test env generator from its script path."""
    spec = importlib.util.spec_from_file_location("vms_fix_034_generate_env", GENERATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {GENERATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def permissive_umask():
    """Force umask 022 so a naive create would be mode 0644."""
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture
def field_env(monkeypatch, tmp_path, permissive_umask):
    """Point the generator at tmp and stub openssl/secret generation."""
    module = _load_generator()
    output = tmp_path / ".env"
    monkeypatch.setattr(module, "OUTPUT", output)
    monkeypatch.setattr(module, "_rsa", lambda: _STUB_RSA)
    monkeypatch.setattr(module, "_secret", lambda: _STUB_SECRET)
    before = _env_fingerprint(REAL_ENV)
    env = _FieldEnv(module, output, tmp_path)
    yield env
    assert _env_fingerprint(REAL_ENV) == before, "test wrote the repository-root .env"


def _env_fingerprint(path: Path) -> tuple[int, int] | None:
    """Return size and mtime, or None when the repository env file is absent."""
    if not path.exists():
        return None
    info = path.stat()
    return (info.st_size, info.st_mtime_ns)


def _generate(env: _FieldEnv, force: bool = False) -> Path:
    """Run generation against the tmp recordings directory."""
    return env.module.generate(env.root / "recordings", ["127.0.0.1/32"], force)


def _is_secret_env_file(path: Path, directory: Path) -> bool:
    """True for the env file and same-directory temp files, not the example template."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return (
        resolved.parent == directory.resolve()
        and resolved.name.startswith(".env")
        and resolved.name != ".env.example"
    )


def _install_first_write_probe(monkeypatch, directory: Path, events: list[tuple[str, int, int]]) -> None:
    """Record mode and size when secret-file bytes are about to be written.

    Path.write_text is what unmodified generate_env.py uses. os.write is what a
    private os.open publisher uses. Either observation is enough; both must be 0600.
    """
    real_open = Path.open
    real_write = os.write

    def wrapped_open(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        handle = real_open(self, mode, buffering, encoding, errors, newline)
        if any(flag in mode for flag in ("w", "a", "x", "+")) and _is_secret_env_file(self, directory):
            original_write = handle.write

            def wrapped_handle_write(data):
                info = os.fstat(handle.fileno())
                events.append((self.name, stat.S_IMODE(info.st_mode), info.st_size))
                return original_write(data)

            handle.write = wrapped_handle_write
        return handle

    def wrapped_os_write(fd, data):
        try:
            info = os.fstat(fd)
        except OSError:
            return real_write(fd, data)
        if stat.S_ISREG(info.st_mode):
            try:
                linked = Path(os.readlink(f"/proc/self/fd/{fd}"))
            except OSError:
                linked = None
            if linked is not None and _is_secret_env_file(linked, directory):
                events.append((linked.name, stat.S_IMODE(info.st_mode), info.st_size))
        return real_write(fd, data)

    monkeypatch.setattr(Path, "open", wrapped_open)
    monkeypatch.setattr(os, "write", wrapped_os_write)


def _assert_private_at_first_write(events: list[tuple[str, int, int]]) -> None:
    """Fail when the first stored secret byte is not on a mode-0600 file."""
    assert events, "no secret-file write was observed; cannot prove mode before the first byte"
    name, mode, size = events[0]
    assert mode == 0o600 and size == 0, (
        f"first secret write of {name} observed mode {oct(mode)} with {size} bytes already stored"
    )
    for event_name, event_mode, event_size in events:
        assert event_mode == 0o600, (
            f"secret write of {event_name} observed mode {oct(event_mode)} "
            f"with {event_size} bytes already stored"
        )


def _group_readable_secret_files(root: Path) -> list[str]:
    """Return name:mode for secret-bearing files that group or other can read."""
    found: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077 == 0:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "POSTGRES_PASSWORD=" in text or "VMS_SECRET_KEY=" in text:
            found.append(f"{path.name}:{oct(mode)}")
    return found


def _part_files(root: Path) -> list[str]:
    """Return leftover same-directory temp names."""
    return sorted(path.name for path in root.iterdir() if path.is_file() and ".part" in path.name)


def _assert_no_exposed_partials(root: Path) -> None:
    """Secret bytes must not sit in a group-readable file or a leftover temp."""
    assert _group_readable_secret_files(root) == []
    assert _part_files(root) == []


def test_fresh_secret_file_is_mode_0600_before_first_byte_under_umask_022(monkeypatch, field_env):
    """A new env file is mode 0600 when the first secret byte is written."""
    events: list[tuple[str, int, int]] = []
    _install_first_write_probe(monkeypatch, field_env.root, events)
    _generate(field_env)
    _assert_private_at_first_write(events)
    assert stat.S_IMODE(field_env.output.stat().st_mode) == 0o600
    assert _part_files(field_env.root) == []
    text = field_env.output.read_text(encoding="utf-8")
    assert f"POSTGRES_PASSWORD={_STUB_SECRET}" in text
    assert f"LIVE_VIEW_TOKEN_PRIVATE_KEY_B64={_STUB_RSA}" in text


def test_force_secret_file_is_mode_0600_before_first_byte_under_umask_022(monkeypatch, field_env):
    """--force must not inherit a group-readable mode while writing new secrets."""
    field_env.output.write_text("OLD_MARKER=1\n", encoding="utf-8")
    os.chmod(field_env.output, 0o644)
    events: list[tuple[str, int, int]] = []
    _install_first_write_probe(monkeypatch, field_env.root, events)
    _generate(field_env, force=True)
    _assert_private_at_first_write(events)
    assert stat.S_IMODE(field_env.output.stat().st_mode) == 0o600
    assert _part_files(field_env.root) == []
    text = field_env.output.read_text(encoding="utf-8")
    assert "OLD_MARKER=1" not in text
    assert f"POSTGRES_PASSWORD={_STUB_SECRET}" in text


def test_chmod_failure_leaves_no_group_readable_secret_file(monkeypatch, field_env):
    """A failed chmod must not leave the secret file group- or world-readable."""
    real_chmod = os.chmod

    def chmod_fail_on_output(path, mode):
        if Path(path).resolve() == field_env.output.resolve():
            raise OSError("chmod failed")
        return real_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", chmod_fail_on_output)
    with pytest.raises(OSError, match="chmod failed"):
        _generate(field_env)
    _assert_no_exposed_partials(field_env.root)


def test_force_interruption_before_replace_preserves_previous_env(monkeypatch, field_env):
    """--force must not replace or expose the previous env when publish is interrupted."""
    previous = b"PREVIOUS_MARKER=kept\n"
    field_env.output.write_bytes(previous)
    os.chmod(field_env.output, 0o600)
    real_replace = os.replace

    def replace_fail_on_output(src, dst):
        if Path(dst).resolve() == field_env.output.resolve():
            raise OSError("replace interrupted")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace_fail_on_output)
    with pytest.raises(OSError, match="replace interrupted"):
        _generate(field_env, force=True)
    assert field_env.output.read_bytes() == previous
    assert stat.S_IMODE(field_env.output.stat().st_mode) == 0o600
    _assert_no_exposed_partials(field_env.root)


def test_force_write_failure_removes_partial_and_preserves_previous_env(monkeypatch, field_env):
    """A failed write must delete the temp file and leave the previous env unchanged."""
    previous = b"PREVIOUS_MARKER=kept\n"
    field_env.output.write_bytes(previous)
    os.chmod(field_env.output, 0o600)
    real_write = os.write

    def fail_part_write(fd, data):
        try:
            info = os.fstat(fd)
            linked = Path(os.readlink(f"/proc/self/fd/{fd}")) if stat.S_ISREG(info.st_mode) else None
        except OSError:
            linked = None
        if linked is not None and ".part" in linked.name:
            raise OSError("write interrupted")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", fail_part_write)
    with pytest.raises(OSError, match="write interrupted"):
        _generate(field_env, force=True)
    assert field_env.output.read_bytes() == previous
    assert stat.S_IMODE(field_env.output.stat().st_mode) == 0o600
    _assert_no_exposed_partials(field_env.root)


def test_fresh_interruption_before_replace_leaves_no_secret_file(monkeypatch, field_env):
    """A failed publish of a new env file leaves no secret file behind."""
    real_replace = os.replace

    def replace_fail_on_output(src, dst):
        if Path(dst).resolve() == field_env.output.resolve():
            info = Path(src).stat()
            assert stat.S_IMODE(info.st_mode) == 0o600, (
                f"temp file mode before replace was {oct(stat.S_IMODE(info.st_mode))}"
            )
            raise OSError("replace interrupted")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace_fail_on_output)
    with pytest.raises(OSError, match="replace interrupted"):
        _generate(field_env)
    assert not field_env.output.exists()
    _assert_no_exposed_partials(field_env.root)


def test_refuse_overwrite_without_force_keeps_existing_env(field_env):
    """Without --force an existing env file is left unchanged."""
    original = b"KEEP_MARKER=1\n"
    field_env.output.write_bytes(original)
    os.chmod(field_env.output, 0o600)
    with pytest.raises(FileExistsError, match="refuse to overwrite"):
        _generate(field_env, force=False)
    assert field_env.output.read_bytes() == original
    assert stat.S_IMODE(field_env.output.stat().st_mode) == 0o600
