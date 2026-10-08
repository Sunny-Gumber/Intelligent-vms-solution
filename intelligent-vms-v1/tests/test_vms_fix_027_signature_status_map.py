"""Authenticode Status must follow Microsoft's SignatureStatus enum.

Windows PowerShell ConvertTo-Json can emit SignatureStatus as a number
(Valid = 0). A numeric 0 must not be rejected once the PE certificate table
says the file is signed. String names and numeric values share one table.
Anything outside that table fails closed. With --expect-unsigned, only
NotSigned is UNSIGNED_EXPECTED. UnknownError fails closed in every form.

The values are documented at
https://learn.microsoft.com/en-us/dotnet/api/system.management.automation.signaturestatus?view=powershellsdk-7.4.0
"""
from __future__ import annotations

import importlib.util
import json
import struct
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "qualification" / "windows" / "scripts" / "qualify.py"
spec = importlib.util.spec_from_file_location("qualify_vms_fix_027", SCRIPT)
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)

DOCUMENTED_SIGNATURE_STATUS = {
    0: "Valid",
    1: "UnknownError",
    2: "NotSigned",
    3: "HashMismatch",
    4: "NotTrusted",
    5: "NotSupportedFileFormat",
    6: "Incompatible",
}


def _write_pe(path: Path, cert_size: int) -> None:
    """Write a minimal PE whose certificate table is present only when requested."""
    data = bytearray(512)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<H", data, 0x98, 0x10B)
    struct.pack_into("<H", data, 0x94, 224)
    struct.pack_into("<I", data, 0xF4, 16)
    security = 0x98 + 96 + (4 * 8)
    cert_offset = 0x180 if cert_size else 0
    struct.pack_into("<II", data, security, cert_offset, cert_size)
    if cert_size and len(data) < cert_offset + cert_size:
        data.extend(b"\x00" * (cert_offset + cert_size - len(data)))
    path.write_bytes(data)


def _completed(args, stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    """Build a fake PowerShell result without running Windows."""
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


def _verify(monkeypatch, artifact: Path, expect_unsigned: bool, stdout: str, *, windows: bool = True,
            returncode: int = 0, stderr: str = "") -> tuple[str, str, list]:
    """Run verify_signature with a faked host and a faked PowerShell process.

    The host gate is patched by name. os.name stays unchanged so pathlib does
    not reinterpret the fixture path as a Windows path.
    """
    seen: list = []

    def fake_run(args, **kwargs):
        seen.append(args)
        return _completed(args, stdout, returncode=returncode, stderr=stderr)

    monkeypatch.setattr(q, "_authenticode_verification_available", lambda: windows, raising=False)
    monkeypatch.setattr(q.subprocess, "run", fake_run)
    status, detail = q.verify_signature(str(artifact), expect_unsigned)
    return status, detail, seen


def test_signature_status_table_matches_microsoft_enum():
    """The single status table is the documented SignatureStatus enum."""
    assert q.SIGNATURE_STATUS_BY_VALUE == DOCUMENTED_SIGNATURE_STATUS


@pytest.mark.parametrize(("raw", "expected"), [
    (0, "Valid"),
    ("0", "Valid"),
    ("Valid", "Valid"),
    (1, "UnknownError"),
    ("1", "UnknownError"),
    ("UnknownError", "UnknownError"),
    (2, "NotSigned"),
    ("2", "NotSigned"),
    ("NotSigned", "NotSigned"),
    (3, "HashMismatch"),
    ("3", "HashMismatch"),
    ("HashMismatch", "HashMismatch"),
    (4, "NotTrusted"),
    ("4", "NotTrusted"),
    ("NotTrusted", "NotTrusted"),
    (5, "NotSupportedFileFormat"),
    ("5", "NotSupportedFileFormat"),
    ("NotSupportedFileFormat", "NotSupportedFileFormat"),
    (6, "Incompatible"),
    ("6", "Incompatible"),
    ("Incompatible", "Incompatible"),
])
def test_documented_status_forms_share_one_table(raw, expected):
    """Numeric values and names resolve through the same SignatureStatus table."""
    assert q.signature_status_name(raw) == expected


@pytest.mark.parametrize("raw", [
    7, -1, 99, 8, "7", "-1", "99", "Signed", "valid", "VALID", " NotSigned", "2 ", "",
    None, True, False, 0.0, 1.0, 2.5, [], {}, object(),
])
def test_unexpected_status_values_are_not_signed_names(raw):
    """Values outside the enum have no status name and cannot be treated as Valid."""
    assert q.signature_status_name(raw) is None
    assert q.signature_status_name(raw) != "Valid"


@pytest.mark.parametrize(("status_value", "expect_unsigned", "expected"), [
    (0, False, "SIGNED_VALID"),
    (0, True, "SIGNED_VALID"),
    ("0", True, "SIGNED_VALID"),
    ("Valid", False, "SIGNED_VALID"),
    ("Valid", True, "SIGNED_VALID"),
    (2, True, "UNSIGNED_EXPECTED"),
    ("2", True, "UNSIGNED_EXPECTED"),
    ("NotSigned", True, "UNSIGNED_EXPECTED"),
    (2, False, "FAIL"),
    ("2", False, "FAIL"),
    ("NotSigned", False, "FAIL"),
    (3, True, "FAIL"),
    ("3", True, "FAIL"),
    ("HashMismatch", True, "FAIL"),
    (3, False, "FAIL"),
    ("HashMismatch", False, "FAIL"),
    (4, True, "FAIL"),
    ("4", True, "FAIL"),
    ("NotTrusted", True, "FAIL"),
    (4, False, "FAIL"),
    ("NotTrusted", False, "FAIL"),
    (1, True, "FAIL"),
    ("1", True, "FAIL"),
    ("UnknownError", True, "FAIL"),
    (1, False, "FAIL"),
    ("UnknownError", False, "FAIL"),
    (5, True, "FAIL"),
    ("NotSupportedFileFormat", False, "FAIL"),
    (6, True, "FAIL"),
    ("Incompatible", False, "FAIL"),
    (7, True, "FAIL"),
    (-1, False, "FAIL"),
    (99, True, "FAIL"),
    ("bogus", True, "FAIL"),
    ("valid", False, "FAIL"),
    ("", True, "FAIL"),
    (None, True, "FAIL"),
    (True, True, "FAIL"),
    (False, True, "FAIL"),
    (False, False, "FAIL"),
    (0.0, True, "FAIL"),
    (True, False, "FAIL"),
])
def test_powershell_status_json_follows_signature_status_map(tmp_path, monkeypatch, status_value, expect_unsigned, expected):
    """Faked ConvertTo-Json Status values use the documented map and fail closed."""
    signed = tmp_path / "signed.exe"
    _write_pe(signed, 32)
    stdout = json.dumps({"Status": status_value, "StatusMessage": "synthetic"})
    status, _detail, seen = _verify(monkeypatch, signed, expect_unsigned, stdout)
    assert seen, "signed certificate table must consult PowerShell"
    assert status == expected
    if status_value in (0, "0", "Valid"):
        assert status != "UNSIGNED_EXPECTED"


@pytest.mark.parametrize("stdout", [
    "not-json",
    "",
    "{",
    "null",
    "[]",
    "0",
    "true",
    "false",
    '"Valid"',
])
def test_malformed_powershell_output_fails_closed(tmp_path, monkeypatch, stdout):
    """Malformed PowerShell output is unsigned-rejected and never signed."""
    signed = tmp_path / "signed.exe"
    _write_pe(signed, 32)
    status, _detail, _seen = _verify(monkeypatch, signed, True, stdout)
    assert status == "FAIL"


def test_missing_status_key_fails_closed(tmp_path, monkeypatch):
    """A JSON object without Status is not a valid signature."""
    signed = tmp_path / "signed.exe"
    _write_pe(signed, 32)
    status, _detail, _seen = _verify(monkeypatch, signed, True, json.dumps({"StatusMessage": "missing"}))
    assert status == "FAIL"


def test_powershell_failure_fails_closed(tmp_path, monkeypatch):
    """A non-zero PowerShell exit is not a valid signature."""
    signed = tmp_path / "signed.exe"
    _write_pe(signed, 32)
    status, detail, _seen = _verify(
        monkeypatch, signed, False, '{"Status":0}', returncode=1, stderr="cmdlet failed"
    )
    assert status == "FAIL"
    assert "cmdlet failed" in detail
    assert status != "SIGNED_VALID"


def test_numeric_zero_is_valid_only_when_certificate_table_is_present(tmp_path, monkeypatch):
    """Status 0 cannot promote an artifact that has no Authenticode certificate table."""
    unsigned = tmp_path / "unsigned.exe"
    _write_pe(unsigned, 0)

    def fail_run(*args, **kwargs):
        raise AssertionError("PowerShell must not run when the certificate table is absent")

    monkeypatch.setattr(q.subprocess, "run", fail_run)
    status, detail = q.verify_signature(str(unsigned), True)
    assert status == "UNSIGNED_EXPECTED"
    assert detail == "PE has no Authenticode certificate table"


def test_unsigned_field_test_artifact_stays_unsigned(tmp_path, monkeypatch):
    """The hosted unsigned.bin contract stays UNSIGNED_EXPECTED and does not call PowerShell."""
    artifact = tmp_path / "unsigned.bin"
    artifact.write_bytes(b"")

    def fail_run(*args, **kwargs):
        raise AssertionError("PowerShell must not run for an unsigned field-test artifact")

    monkeypatch.setattr(q.subprocess, "run", fail_run)
    status, detail = q.verify_signature(str(artifact), True)
    assert status == "UNSIGNED_EXPECTED"
    assert detail == "PE has no Authenticode certificate table"
    refused, refused_detail = q.verify_signature(str(artifact), False)
    assert refused == "FAIL"
    assert refused_detail == "PE has no Authenticode certificate table"


def test_powershell_requests_status_text_and_compressed_json(tmp_path, monkeypatch):
    """Status.ToString() and -Compress make the cmdlet output deterministic."""
    signed = tmp_path / "signed.exe"
    _write_pe(signed, 32)
    status, _detail, seen = _verify(monkeypatch, signed, False, '{"Status":"Valid"}')
    assert status == "SIGNED_VALID"
    command = seen[0][4]
    assert seen[0][1:4] == ["-NoProfile", "-NonInteractive", "-Command"]
    assert "Get-AuthenticodeSignature -LiteralPath" in command
    assert "$_.Status.ToString()" in command
    assert "ConvertTo-Json -Compress -Depth 4" in command


def test_non_windows_host_does_not_invent_a_signature(tmp_path, monkeypatch):
    """A certificate table on a non-Windows host stays NOT_RUN and does not call PowerShell."""
    signed = tmp_path / "signed.exe"
    _write_pe(signed, 32)
    status, detail, seen = _verify(monkeypatch, signed, False, '{"Status":0}', windows=False)
    assert status == "NOT_RUN"
    assert seen == []
    assert "Windows" in detail
