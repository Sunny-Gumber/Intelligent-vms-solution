"""--expect-unsigned must fail closed unless SignatureStatus is NotSigned.

Boss default pending signing ADR owner confirmation (issue #105 item d).
UnknownError is an unreadable or invalid signature, not an unsigned file.

These cases use synthetic captured ConvertTo-Json output. They do not run
Get-AuthenticodeSignature and they are not a real signed-artifact run.
"""
from __future__ import annotations

import importlib.util
import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "qualification" / "windows" / "scripts" / "qualify.py"
spec = importlib.util.spec_from_file_location("qualify_vms_fix_045", SCRIPT)
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)

_MISSING = object()
_SUCCESS = {"SIGNED_VALID", "UNSIGNED_EXPECTED", "NOT_RUN"}
_DOCUMENTED = (
    (0, "Valid"),
    (1, "UnknownError"),
    (2, "NotSigned"),
    (3, "HashMismatch"),
    (4, "NotTrusted"),
    (5, "NotSupportedFileFormat"),
    (6, "Incompatible"),
)


def _write_pe(path: Path, cert_size: int) -> None:
    """Write a minimal PE whose certificate table is present only when requested."""
    data = bytearray(512)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<H", data, 0x98, 0x10B)
    security = 0x98 + 96 + (4 * 8)
    cert_offset = 0x180 if cert_size else 0
    struct.pack_into("<II", data, security, cert_offset, cert_size)
    if cert_size and len(data) < cert_offset + cert_size:
        data.extend(b"\x00" * (cert_offset + cert_size - len(data)))
    path.write_bytes(data)


def _completed(args, stdout: str) -> subprocess.CompletedProcess[str]:
    """Build a fake PowerShell result without running Windows."""
    return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")


def _expected(name: str, expect_unsigned: bool) -> tuple[str, str | None]:
    """Return the fail-closed verdict and reason for one documented status."""
    if name == "Valid":
        return "SIGNED_VALID", None
    if name == "NotSigned" and expect_unsigned:
        return "UNSIGNED_EXPECTED", None
    return "FAIL", "SIGNATURE_STATUS_REJECTED:" + name


def _cli(monkeypatch, capsys, artifact: Path, expect_unsigned: bool, stdout: str):
    """Run verify-signature against synthetic Authenticode JSON."""
    seen: list = []

    def fake_run(args, **kwargs):
        seen.append(args)
        return _completed(args, stdout)

    monkeypatch.setattr(q, "_authenticode_verification_available", lambda: True, raising=False)
    monkeypatch.setattr(q.subprocess, "run", fake_run)
    argv = ["qualify.py", "verify-signature", "--artifact", str(artifact)]
    if expect_unsigned:
        argv.append("--expect-unsigned")
    monkeypatch.setattr(sys, "argv", argv)
    code = q.main()
    payload = json.loads(capsys.readouterr().out)
    return code, payload, seen


def _assert_outcome(code: int, payload: dict, seen: list, verdict: str, reason: str | None, raw) -> None:
    """Check exit code, status, and the reason prefix from one synthetic run."""
    assert seen, "a certificate table must be checked with synthetic Authenticode output"
    assert "Get-AuthenticodeSignature -LiteralPath" in seen[0][4]
    assert payload["status"] == verdict
    assert code == (0 if verdict in _SUCCESS else 1)
    if reason is None:
        assert not str(payload["detail"]).startswith("SIGNATURE_STATUS_")
        return
    prefix, _, rest = payload["detail"].partition(" ")
    assert prefix == reason
    body = json.loads(rest)
    if raw is _MISSING:
        assert "Status" not in body
    else:
        assert body["Status"] == raw
    assert code != 0
    assert verdict == "FAIL"


@pytest.mark.parametrize(("code_value", "name"), _DOCUMENTED)
@pytest.mark.parametrize("form", ["number", "decimal", "name"])
@pytest.mark.parametrize("expect_unsigned", [False, True])
def test_documented_status_forms_fail_closed_except_not_signed(
    tmp_path, monkeypatch, capsys, code_value, name, form, expect_unsigned
):
    """Numeric, decimal, and named statuses exit 0 only for Valid, or NotSigned when expected unsigned."""
    raw = {"number": code_value, "decimal": str(code_value), "name": name}[form]
    signed = tmp_path / "captured.exe"
    _write_pe(signed, 32)
    stdout = json.dumps({"Status": raw, "StatusMessage": "synthetic"})
    code, payload, seen = _cli(monkeypatch, capsys, signed, expect_unsigned, stdout)
    verdict, reason = _expected(name, expect_unsigned)
    _assert_outcome(code, payload, seen, verdict, reason, raw)
    if name == "UnknownError":
        assert payload["status"] != "UNSIGNED_EXPECTED"
        assert code != 0


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("bogus", "SIGNATURE_STATUS_UNPARSED"),
        ("valid", "SIGNATURE_STATUS_UNPARSED"),
        ("VALID", "SIGNATURE_STATUS_UNPARSED"),
        ("UNKNOWNERROR", "SIGNATURE_STATUS_UNPARSED"),
        (" NotSigned", "SIGNATURE_STATUS_UNPARSED"),
        ("2 ", "SIGNATURE_STATUS_UNPARSED"),
        (7, "SIGNATURE_STATUS_UNPARSED"),
        (-1, "SIGNATURE_STATUS_UNPARSED"),
        (True, "SIGNATURE_STATUS_UNPARSED"),
        (False, "SIGNATURE_STATUS_UNPARSED"),
        (0.0, "SIGNATURE_STATUS_UNPARSED"),
        ("", "SIGNATURE_STATUS_EMPTY"),
        ("   ", "SIGNATURE_STATUS_EMPTY"),
        (None, "SIGNATURE_STATUS_MISSING"),
        (_MISSING, "SIGNATURE_STATUS_MISSING"),
    ],
)
@pytest.mark.parametrize("expect_unsigned", [False, True])
def test_garbage_empty_and_missing_status_exit_nonzero(
    tmp_path, monkeypatch, capsys, raw, reason, expect_unsigned
):
    """Unparsable, empty, and missing statuses fail closed with or without --expect-unsigned."""
    signed = tmp_path / "captured.exe"
    _write_pe(signed, 32)
    body = {"StatusMessage": "synthetic"}
    if raw is not _MISSING:
        body["Status"] = raw
    code, payload, seen = _cli(monkeypatch, capsys, signed, expect_unsigned, json.dumps(body))
    _assert_outcome(code, payload, seen, "FAIL", reason, raw)


def test_unsigned_field_test_file_does_not_use_unknown_error(tmp_path, monkeypatch, capsys):
    """The empty field-test artifact stays UNSIGNED_EXPECTED and never consults a signature status."""
    artifact = tmp_path / "unsigned.bin"
    artifact.write_bytes(b"")

    def fail_run(*args, **kwargs):
        raise AssertionError("PowerShell must not run for an artifact with no certificate table")

    monkeypatch.setattr(q.subprocess, "run", fail_run)
    monkeypatch.setattr(
        sys, "argv", ["qualify.py", "verify-signature", "--artifact", str(artifact), "--expect-unsigned"]
    )
    assert q.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "UNSIGNED_EXPECTED"
    assert payload["detail"] == "PE has no Authenticode certificate table"
    assert "UnknownError" not in payload["detail"]
