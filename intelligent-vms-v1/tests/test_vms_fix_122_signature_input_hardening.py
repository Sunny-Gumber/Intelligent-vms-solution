"""Signature verification must fail closed on the FIX-045 input residuals.

REV-045-001: ADR and qualification README name every exit-0 status.
REV-045-002: only a regular file of length 0 is the empty placeholder, and
reads are capped.
REV-045-003: unreadable paths print JSON and exit 1, with no traceback.
REV-045-004: bytes that change before PowerShell returns are not unsigned.

Fixtures are synthetic. Nothing here is a real signed-artifact run.
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "qualification" / "windows" / "scripts" / "qualify.py"
ADR = ROOT / "docs" / "architecture" / "ADR_WINDOWS_CODE_SIGNING_READINESS.md"
README = ROOT / "qualification" / "windows" / "README.md"
spec = importlib.util.spec_from_file_location("qualify_vms_fix_122", SCRIPT)
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)

_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
_MAX_PE_HEADER_BYTES = 1024 * 1024
_EXIT_ZERO = (
    "Exit 0 statuses are exactly SIGNED_VALID, UNSIGNED_EXPECTED, and NOT_RUN."
)
_SIGNED_VALID = (
    "SIGNED_VALID exits 0 with or without --expect-unsigned when "
    "Get-AuthenticodeSignature Status is Valid (numeric 0, \"0\", or Valid)."
)
_NOT_RUN = (
    "NOT_RUN exits 0 with or without --expect-unsigned when the certificate "
    "table is in-file and os.name is not nt. Detail: Authenticode cryptographic "
    "verification requires Windows."
)
_UNSIGNED = (
    "UNSIGNED_EXPECTED exits 0 only with --expect-unsigned, and only for a "
    "regular file of length 0, a well-formed (0, 0) security directory, or NotSigned."
)


def _write_pe(path: Path, cert_size: int, *, length: int = 512, cert_offset: int | None = None) -> None:
    """Write a synthetic PE. This is not a signed artifact."""
    data = bytearray(max(length, 512))
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<H", data, 0x98, 0x10B)
    struct.pack_into("<H", data, 0x94, 224)
    struct.pack_into("<I", data, 0xF4, 16)
    security = 0x98 + 96 + (4 * 8)
    offset = (0x180 if cert_size else 0) if cert_offset is None else cert_offset
    struct.pack_into("<II", data, security, offset, cert_size)
    if cert_size and len(data) < offset + cert_size:
        data.extend(b"\x00" * (offset + cert_size - len(data)))
    path.write_bytes(bytes(data[: max(length, offset + cert_size if cert_size else length)]))


def _cli(monkeypatch, capsys, artifact: Path, *, expect_unsigned: bool = True):
    """Run verify-signature and require a JSON object instead of a traceback."""
    argv = ["qualify.py", "verify-signature", "--artifact", str(artifact)]
    if expect_unsigned:
        argv.append("--expect-unsigned")
    monkeypatch.setattr(sys, "argv", argv)

    def fail_run(*args, **kwargs):
        raise AssertionError("PowerShell must not run")

    monkeypatch.setattr(q.subprocess, "run", fail_run)
    try:
        code = q.main()
    except OSError as exc:
        pytest.fail(f"uncaught {type(exc).__name__}: {exc}")
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Traceback" not in captured.out
    payload = json.loads(captured.out)
    return code, payload


def _isolated(artifact: str, *, limit_bytes: int | None, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run verify-signature in a child so a blocking or huge read cannot hang the parent."""
    script = r"""
import resource, sys
limit = int(sys.argv[3])
if limit > 0:
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location("qualify_vms_fix_122_child", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
sys.argv = ["qualify.py", "verify-signature", "--artifact", sys.argv[2], "--expect-unsigned"]
try:
    code = mod.main()
except Exception as exc:
    sys.stderr.write(type(exc).__name__ + ": " + str(exc))
    raise SystemExit(1)
raise SystemExit(code)
"""
    try:
        return subprocess.run(
            [sys.executable, "-c", script, str(SCRIPT), artifact, str(limit_bytes or 0)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"verify-signature blocked on {artifact}")


def test_docs_name_every_exit_zero_outcome():
    """ADR and README list SIGNED_VALID and Linux NOT_RUN as exit 0, matching the code."""
    assert getattr(q, "_MAX_ARTIFACT_BYTES", None) == _MAX_ARTIFACT_BYTES
    assert getattr(q, "_MAX_PE_HEADER_BYTES", None) == _MAX_PE_HEADER_BYTES
    for path in (ADR, README):
        text = path.read_text(encoding="utf-8")
        assert _EXIT_ZERO in text
        assert _SIGNED_VALID in text
        assert _NOT_RUN in text
        assert _UNSIGNED in text
        assert "ARTIFACT_UNREADABLE:<errno-name>" in text
        assert "ARTIFACT_NOT_REGULAR_FILE" in text
        assert "ARTIFACT_TOO_LARGE" in text
        assert "ARTIFACT_CHANGED" in text
        assert "512 MiB (536870912 bytes)" in text
        assert "1 MiB (1048576 bytes)" in text
        assert "exits 0 in only three cases" not in text
        assert "Anything else exits 1 with status `FAIL`" not in text
        assert "only `Valid` exits 0" not in text


def test_regular_empty_file_is_the_placeholder(tmp_path, monkeypatch, capsys):
    """A regular zero-length file is still UNSIGNED_EXPECTED and does not start PowerShell."""
    artifact = tmp_path / "unsigned.bin"
    artifact.write_bytes(b"")
    assert stat.S_ISREG(artifact.stat().st_mode)
    code, payload = _cli(monkeypatch, capsys, artifact)
    assert code == 0
    assert payload == {
        "status": "UNSIGNED_EXPECTED",
        "detail": "PE has no Authenticode certificate table",
    }


def test_symlink_to_regular_empty_file_is_the_placeholder(tmp_path, monkeypatch, capsys):
    """A symlink to a regular empty file is the placeholder. The symlink itself is not a device."""
    target = tmp_path / "unsigned.bin"
    target.write_bytes(b"")
    link = tmp_path / "link.bin"
    link.symlink_to(target)
    code, payload = _cli(monkeypatch, capsys, link)
    assert code == 0
    assert payload["status"] == "UNSIGNED_EXPECTED"
    assert payload["detail"] == "PE has no Authenticode certificate table"


@pytest.mark.skipif(not Path("/dev/null").exists(), reason="unix null device")
def test_dev_null_is_not_the_empty_placeholder(monkeypatch, capsys):
    """/dev/null is a character device. A zero-length read must not exit 0."""
    code, payload = _cli(monkeypatch, capsys, Path("/dev/null"))
    assert code == 1
    assert payload == {"status": "FAIL", "detail": "ARTIFACT_NOT_REGULAR_FILE"}
    assert q.pe_has_authenticode(Path("/dev/null")) is False


@pytest.mark.skipif(not Path("/dev/null").exists(), reason="unix null device")
def test_symlink_to_dev_null_is_not_the_empty_placeholder(tmp_path, monkeypatch, capsys):
    """A symlink to /dev/null follows to a device and fails closed."""
    link = tmp_path / "null-link"
    link.symlink_to("/dev/null")
    code, payload = _cli(monkeypatch, capsys, link)
    assert code == 1
    assert payload == {"status": "FAIL", "detail": "ARTIFACT_NOT_REGULAR_FILE"}


def test_fifo_fails_closed_without_blocking(tmp_path):
    """A FIFO is not a regular file and must not block the checker."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("mkfifo is not supported")
    fifo = tmp_path / "artifact.fifo"
    os.mkfifo(fifo)
    proc = _isolated(str(fifo), limit_bytes=None, timeout=3)
    assert proc.returncode == 1
    assert "Traceback" not in proc.stderr
    assert json.loads(proc.stdout) == {"status": "FAIL", "detail": "ARTIFACT_NOT_REGULAR_FILE"}


@pytest.mark.skipif(not Path("/dev/zero").exists(), reason="unix zero device")
def test_dev_zero_fails_closed_without_reading():
    """/dev/zero must not be read until memory is exhausted."""
    proc = _isolated("/dev/zero", limit_bytes=256 * 1024 * 1024, timeout=15)
    assert proc.returncode == 1
    assert "Traceback" not in proc.stdout
    assert "MemoryError" not in proc.stderr
    assert json.loads(proc.stdout) == {"status": "FAIL", "detail": "ARTIFACT_NOT_REGULAR_FILE"}


def test_oversize_sparse_file_is_rejected_before_read(tmp_path):
    """A file larger than 512 MiB is rejected from st_size and is not loaded."""
    huge = tmp_path / "huge.bin"
    with huge.open("wb") as handle:
        handle.truncate(_MAX_ARTIFACT_BYTES + 1)
    proc = _isolated(str(huge), limit_bytes=256 * 1024 * 1024, timeout=20)
    assert proc.returncode == 1
    assert "Traceback" not in proc.stdout
    assert "MemoryError" not in proc.stderr
    assert json.loads(proc.stdout) == {"status": "FAIL", "detail": "ARTIFACT_TOO_LARGE"}


def test_non_pe_classification_does_not_read_the_whole_file(tmp_path, monkeypatch):
    """A multi-megabyte non-PE is rejected from its leading bytes."""
    artifact = tmp_path / "zeros.bin"
    with artifact.open("wb") as handle:
        handle.truncate(2 * 1024 * 1024)
    calls = {"n": 0}
    real = q.os.read

    def counting(fd, size):
        calls["n"] += size
        return real(fd, size)

    monkeypatch.setattr(q.os, "read", counting)
    status, detail = q.verify_signature(str(artifact), True)
    assert status == "FAIL"
    assert detail == "NOT_A_PE_FILE"
    assert 0 < calls["n"] <= 64
    assert calls["n"] < artifact.stat().st_size


def test_certificate_tail_is_not_loaded_for_classification(tmp_path, monkeypatch):
    """An in-file certificate past the headers is not copied into the parse buffer."""
    artifact = tmp_path / "tail.exe"
    _write_pe(artifact, 32, length=64 * 1024, cert_offset=64 * 1024 - 32)
    calls = {"n": 0}
    real = q.os.read

    def counting(fd, size):
        calls["n"] += size
        return real(fd, size)

    monkeypatch.setattr(q.os, "read", counting)
    status, detail = q.verify_signature(str(artifact), True)
    assert status == "NOT_RUN"
    assert detail == "Authenticode cryptographic verification requires Windows"
    assert 0 < calls["n"] < 4096
    assert calls["n"] < artifact.stat().st_size


def test_header_budget_fails_closed(tmp_path, monkeypatch):
    """A parse that would materialize more than the header budget exits 1."""
    monkeypatch.setattr(q, "_MAX_PE_HEADER_BYTES", 8, raising=False)
    artifact = tmp_path / "pe.exe"
    _write_pe(artifact, 32)
    status, detail = q.verify_signature(str(artifact), True)
    assert status == "FAIL"
    assert detail == "PE_MALFORMED:header_exceeds_parse_window"


def test_missing_path_is_json_fail(tmp_path, monkeypatch, capsys):
    """A missing path exits 1 with ARTIFACT_UNREADABLE:ENOENT and no traceback."""
    code, payload = _cli(monkeypatch, capsys, tmp_path / "no-such-file")
    assert code == 1
    assert payload == {"status": "FAIL", "detail": "ARTIFACT_UNREADABLE:ENOENT"}


def test_directory_is_json_fail(tmp_path, monkeypatch, capsys):
    """A directory exits 1 with ARTIFACT_UNREADABLE:EISDIR and no traceback."""
    code, payload = _cli(monkeypatch, capsys, tmp_path)
    assert code == 1
    assert payload == {"status": "FAIL", "detail": "ARTIFACT_UNREADABLE:EISDIR"}


def test_symlink_loop_is_json_fail(tmp_path, monkeypatch, capsys):
    """A symlink loop exits 1 with ARTIFACT_UNREADABLE:ELOOP and no traceback."""
    left = tmp_path / "loop-a"
    right = tmp_path / "loop-b"
    left.symlink_to(right)
    right.symlink_to(left)
    code, payload = _cli(monkeypatch, capsys, left)
    assert code == 1
    assert payload == {"status": "FAIL", "detail": "ARTIFACT_UNREADABLE:ELOOP"}


def test_dangling_symlink_is_json_fail(tmp_path, monkeypatch, capsys):
    """A dangling symlink exits 1 with ARTIFACT_UNREADABLE:ENOENT and no traceback."""
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / "missing")
    code, payload = _cli(monkeypatch, capsys, link)
    assert code == 1
    assert payload == {"status": "FAIL", "detail": "ARTIFACT_UNREADABLE:ENOENT"}


def test_permission_denied_is_json_fail(tmp_path, monkeypatch, capsys):
    """A mode-000 file exits 1 with ARTIFACT_UNREADABLE:EACCES and no traceback."""
    if os.geteuid() == 0:
        pytest.skip("root can read a mode-000 file")
    locked = tmp_path / "locked.bin"
    locked.write_bytes(b"MZ")
    locked.chmod(0)
    try:
        code, payload = _cli(monkeypatch, capsys, locked)
    finally:
        locked.chmod(0o644)
    assert code == 1
    assert payload == {"status": "FAIL", "detail": "ARTIFACT_UNREADABLE:EACCES"}


def test_unchanged_not_signed_stays_unsigned(tmp_path, monkeypatch):
    """NotSigned on a stable certificate table is still UNSIGNED_EXPECTED when expected."""
    artifact = tmp_path / "notsigned.exe"
    _write_pe(artifact, 32)
    seen: list = []

    def fake_run(args, **kwargs):
        seen.append(args)
        stdout = json.dumps({"Status": "NotSigned", "StatusMessage": "synthetic"})
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(q, "_authenticode_verification_available", lambda: True, raising=False)
    monkeypatch.setattr(q.subprocess, "run", fake_run)
    status, detail = q.verify_signature(str(artifact), True)
    assert seen
    assert "Get-AuthenticodeSignature -LiteralPath" in seen[0][4]
    assert status == "UNSIGNED_EXPECTED"
    body = json.loads(detail)
    assert body["Status"] == "NotSigned"


def test_powershell_replacement_fails_closed(tmp_path, monkeypatch, capsys):
    """A file replaced under the PowerShell path must not become UNSIGNED_EXPECTED."""
    artifact = tmp_path / "signed.exe"
    _write_pe(artifact, 32)
    seen: list = []

    def fake_run(args, **kwargs):
        seen.append(args)
        command = args[4]
        marker = "-LiteralPath '"
        start = command.index(marker) + len(marker)
        end = command.index("'", start)
        target = Path(command[start:end])
        target.chmod(0o600)
        target.write_bytes(b"this is now text")
        stdout = json.dumps({"Status": "NotSigned"})
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(q, "_authenticode_verification_available", lambda: True, raising=False)
    monkeypatch.setattr(q.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["qualify.py", "verify-signature", "--artifact", str(artifact), "--expect-unsigned"],
    )
    code = q.main()
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    payload = json.loads(captured.out)
    assert seen, "the certificate table must reach the synthetic PowerShell runner"
    assert code == 1
    assert payload == {"status": "FAIL", "detail": "ARTIFACT_CHANGED"}
    assert payload["status"] != "UNSIGNED_EXPECTED"


def _literal_path(command: str) -> Path:
    """Return the single-quoted LiteralPath from a synthetic PowerShell command."""
    marker = "-LiteralPath '"
    start = command.index(marker) + len(marker)
    end = command.index("'", start)
    return Path(command[start:end])


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="procfs status is linux")
def test_proc_status_is_not_the_empty_placeholder(monkeypatch, capsys):
    """A size-0 proc file with content is not the unsigned placeholder."""
    code, payload = _cli(monkeypatch, capsys, Path("/proc/self/status"))
    assert code == 1
    assert payload == {"status": "FAIL", "detail": "NOT_A_PE_FILE"}


@pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="procfs environ is linux")
def test_proc_environ_is_not_the_empty_placeholder(monkeypatch, capsys):
    """Environ content must not become UNSIGNED_EXPECTED and must not be printed."""
    code, payload = _cli(monkeypatch, capsys, Path("/proc/self/environ"))
    assert code == 1
    assert payload == {"status": "FAIL", "detail": "NOT_A_PE_FILE"}
    assert "SHELL=" not in payload["detail"]


@pytest.mark.skipif(not Path("/proc/self/mem").exists(), reason="procfs mem is linux")
def test_proc_mem_is_not_the_empty_placeholder(monkeypatch, capsys):
    """/proc/self/mem reports size 0. A failed or non-PE read must not exit 0."""
    code, payload = _cli(monkeypatch, capsys, Path("/proc/self/mem"))
    assert code == 1
    assert payload["status"] == "FAIL"
    assert payload["detail"] == "ARTIFACT_UNREADABLE:EIO"
    assert payload["status"] != "UNSIGNED_EXPECTED"


def test_unix_socket_is_not_a_regular_file(tmp_path, monkeypatch, capsys):
    """A Unix socket is ARTIFACT_NOT_REGULAR_FILE even when open fails."""
    import socket

    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("AF_UNIX is not supported")
    sock_path = tmp_path / "artifact.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(sock_path))
    except OSError:
        listener.close()
        pytest.skip("cannot bind a unix socket")
    try:
        code, payload = _cli(monkeypatch, capsys, sock_path)
    finally:
        listener.close()
    assert code == 1
    assert payload == {"status": "FAIL", "detail": "ARTIFACT_NOT_REGULAR_FILE"}


def test_restored_private_copy_fails_closed(tmp_path, monkeypatch, capsys):
    """Restoring the private copy before the post-cmdlet hash is still ARTIFACT_CHANGED."""
    artifact = tmp_path / "signed.exe"
    _write_pe(artifact, 32)

    def fake_run(args, **kwargs):
        target = _literal_path(args[4])
        original = target.read_bytes()
        target.chmod(0o600)
        target.write_bytes(b"this is now text")
        target.write_bytes(original)
        stdout = json.dumps({"Status": "NotSigned"})
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(q, "_authenticode_verification_available", lambda: True, raising=False)
    monkeypatch.setattr(q.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["qualify.py", "verify-signature", "--artifact", str(artifact), "--expect-unsigned"],
    )
    code = q.main()
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    payload = json.loads(captured.out)
    assert code == 1
    assert payload == {"status": "FAIL", "detail": "ARTIFACT_CHANGED"}


def test_restored_private_copy_with_mtime_fails_closed(tmp_path, monkeypatch, capsys):
    """Restoring bytes and st_mtime_ns still fails closed when the watch sees the write."""
    artifact = tmp_path / "signed.exe"
    _write_pe(artifact, 32)

    def fake_run(args, **kwargs):
        target = _literal_path(args[4])
        original = target.read_bytes()
        before = target.stat()
        target.chmod(0o600)
        target.write_bytes(b"this is now text")
        target.write_bytes(original)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        stdout = json.dumps({"Status": "NotSigned"})
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(q, "_authenticode_verification_available", lambda: True, raising=False)
    monkeypatch.setattr(q.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["qualify.py", "verify-signature", "--artifact", str(artifact), "--expect-unsigned"],
    )
    code = q.main()
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    payload = json.loads(captured.out)
    assert code == 1
    assert payload == {"status": "FAIL", "detail": "ARTIFACT_CHANGED"}


def test_signed_valid_and_linux_not_run_exit_zero(tmp_path, monkeypatch, capsys):
    """Valid exits 0 as SIGNED_VALID. The same file on Linux exits 0 as NOT_RUN."""
    artifact = tmp_path / "captured.exe"
    _write_pe(artifact, 32)

    def fake_run(args, **kwargs):
        stdout = json.dumps({"Status": "Valid", "StatusMessage": "synthetic"})
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(q, "_authenticode_verification_available", lambda: True, raising=False)
    monkeypatch.setattr(q.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["qualify.py", "verify-signature", "--artifact", str(artifact), "--expect-unsigned"],
    )
    assert q.main() == 0
    valid = json.loads(capsys.readouterr().out)
    assert valid["status"] == "SIGNED_VALID"

    monkeypatch.setattr(q, "_authenticode_verification_available", lambda: False, raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["qualify.py", "verify-signature", "--artifact", str(artifact)],
    )
    assert q.main() == 0
    skipped = json.loads(capsys.readouterr().out)
    assert skipped == {
        "status": "NOT_RUN",
        "detail": "Authenticode cryptographic verification requires Windows",
    }
