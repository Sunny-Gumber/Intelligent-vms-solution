"""--expect-unsigned must fail closed unless the file is actually unsigned.

Boss default pending signing ADR owner confirmation (issue #105 item d).
UnknownError is an unreadable or invalid signature, not an unsigned file.
A pre-PowerShell UNSIGNED_EXPECTED result is only a zero-length file or a
well-formed PE32/PE32+ whose security directory is exactly (0, 0).

These cases use synthetic bytes and synthetic captured ConvertTo-Json output.
They do not run Get-AuthenticodeSignature and they are not a real signed-artifact run.
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
    struct.pack_into("<H", data, 0x94, 224)
    struct.pack_into("<I", data, 0xF4, 16)
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


def _qa_pe(
    length: int,
    *,
    magic: int = 0x10B,
    cert_offset: int = 0x180,
    cert_size: int = 32,
    e_lfanew: int = 0x80,
    signature: bytes = b"PE\x00\x00",
    optional_size: int | None = None,
    rva_count: int = 16,
) -> bytes:
    """Build a synthetic PE and truncate it to length. This is not a signed artifact."""
    if magic == 0x20B:
        rva_rel, dir_rel, default_optional = 108, 112, 240
    else:
        rva_rel, dir_rel, default_optional = 92, 96, 224
    if optional_size is None:
        optional_size = default_optional
    security = e_lfanew + 24 + dir_rel + (4 * 8)
    buf = bytearray(max(length, security + 8, e_lfanew + 26, 0x40))
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 0x3C, e_lfanew)
    if _in_range(buf, e_lfanew, 24):
        buf[e_lfanew : e_lfanew + 4] = signature
        struct.pack_into("<H", buf, e_lfanew + 20, optional_size)
    if _in_range(buf, e_lfanew + 24, 2):
        struct.pack_into("<H", buf, e_lfanew + 24, magic)
    opt = e_lfanew + 24
    if _in_range(buf, opt + rva_rel, 4):
        struct.pack_into("<I", buf, opt + rva_rel, rva_count)
    if _in_range(buf, security, 8):
        struct.pack_into("<II", buf, security, cert_offset, cert_size)
    return bytes(buf[:length])


def _in_range(buf: bytearray, offset: int, size: int) -> bool:
    """Return whether a header field fits in the synthetic buffer before truncation."""
    return offset >= 0 and size >= 0 and offset + size <= len(buf)


def _container(monkeypatch, capsys, artifact: Path, expect_unsigned: bool):
    """Run verify-signature. A NotSigned stub must not turn a bad container into a pass."""
    seen: list = []

    def fake_run(args, **kwargs):
        seen.append(args)
        stdout = json.dumps({"Status": "NotSigned", "StatusMessage": "synthetic"})
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


@pytest.mark.parametrize("expect_unsigned", [False, True])
@pytest.mark.parametrize(
    ("label", "payload", "reason"),
    [
        ("pe32-415-out-of-range", _qa_pe(415), "PE_SECURITY_DIR_OUT_OF_RANGE"),
        ("pe32-512-past-eof", _qa_pe(512, cert_offset=0x300, cert_size=16), "PE_SECURITY_DIR_OUT_OF_RANGE"),
        (
            "pe32plus-559-one-byte-short",
            _qa_pe(559, magic=0x20B, cert_offset=0x200, cert_size=48),
            "PE_SECURITY_DIR_OUT_OF_RANGE",
        ),
        ("overflow-u32-end", _qa_pe(512, cert_offset=0xFFFFFFFF, cert_size=16), "PE_SECURITY_DIR_OUT_OF_RANGE"),
        ("overflow-u32-sum", _qa_pe(512, cert_offset=0xFFFFFFF0, cert_size=32), "PE_SECURITY_DIR_OUT_OF_RANGE"),
        ("incomplete-size-zero", _qa_pe(512, cert_offset=0x180, cert_size=0), "PE_MALFORMED:security_directory_incomplete"),
        ("incomplete-offset-zero", _qa_pe(512, cert_offset=0, cert_size=32), "PE_MALFORMED:security_directory_incomplete"),
        ("misaligned", _qa_pe(0x194, cert_offset=0x184, cert_size=16), "PE_MALFORMED:security_directory_misaligned"),
        ("cut-before-security-slot", _qa_pe(272), "PE_MALFORMED:truncated_security_directory"),
        ("truncated-dos", _qa_pe(16), "PE_MALFORMED:truncated_dos_header"),
        ("e_lfanew-past-eof", _qa_pe(0x100, e_lfanew=0x200), "PE_MALFORMED:e_lfanew_out_of_range"),
        ("missing-pe-signature", _qa_pe(512, signature=b"XX\x00\x00"), "PE_MALFORMED:missing_pe_signature"),
        ("unknown-magic", _qa_pe(512, magic=0x107), "PE_MALFORMED:unknown_optional_header_magic"),
        ("text", b"this is not a pe file" + b"." * 29, "NOT_A_PE_FILE"),
        ("zip", b"PK\x03\x04" + b"\x00" * 20, "NOT_A_PE_FILE"),
    ],
)
def test_bad_containers_fail_closed_before_powershell(
    tmp_path, monkeypatch, capsys, expect_unsigned, label, payload, reason
):
    """Truncated, malformed, and non-PE bytes exit 1 and do not become unsigned."""
    if label == "pe32-415-out-of-range":
        assert len(payload) == 415
    artifact = tmp_path / label
    artifact.write_bytes(payload)
    code, body, seen = _container(monkeypatch, capsys, artifact, expect_unsigned)
    assert seen == []
    assert code == 1
    assert body["status"] == "FAIL"
    assert body["detail"] == reason
    assert body["status"] != "UNSIGNED_EXPECTED"


def test_qa_416_byte_pe_reaches_signature_status(tmp_path, monkeypatch, capsys):
    """The same PE at 416 bytes has an in-file table, so UnknownError exits 1 and NotSigned exits 0."""
    blob = _qa_pe(416)
    assert len(blob) == 416
    assert blob[:415] == _qa_pe(415)
    artifact = tmp_path / "pe416.exe"
    artifact.write_bytes(blob)
    unknown = json.dumps({"Status": "UnknownError", "StatusMessage": "synthetic"})
    code, body, seen = _cli(monkeypatch, capsys, artifact, True, unknown)
    assert seen
    assert code == 1
    assert body["status"] == "FAIL"
    assert body["detail"].startswith("SIGNATURE_STATUS_REJECTED:UnknownError")
    signed = tmp_path / "notsigned.exe"
    signed.write_bytes(blob)
    code, body, seen = _cli(monkeypatch, capsys, signed, True, json.dumps({"Status": "NotSigned"}))
    assert seen
    assert code == 0
    assert body["status"] == "UNSIGNED_EXPECTED"


@pytest.mark.parametrize("expect_unsigned", [False, True])
def test_well_formed_zero_security_directory_is_the_only_pe_short_circuit(tmp_path, monkeypatch, capsys, expect_unsigned):
    """A PE32 whose security directory is exactly (0, 0) is unsigned only when that was expected."""
    artifact = tmp_path / "unsigned.exe"
    artifact.write_bytes(_qa_pe(512, cert_offset=0, cert_size=0))
    code, body, seen = _container(monkeypatch, capsys, artifact, expect_unsigned)
    assert seen == []
    assert body["detail"] == "PE has no Authenticode certificate table"
    if expect_unsigned:
        assert code == 0
        assert body["status"] == "UNSIGNED_EXPECTED"
    else:
        assert code == 1
        assert body["status"] == "FAIL"


@pytest.mark.parametrize("expect_unsigned", [False, True])
def test_zero_length_placeholder_is_the_only_non_pe_short_circuit(tmp_path, monkeypatch, capsys, expect_unsigned):
    """The documented empty field-test file is the only non-PE that can exit 0."""
    artifact = tmp_path / "unsigned.bin"
    artifact.write_bytes(b"")
    code, body, seen = _container(monkeypatch, capsys, artifact, expect_unsigned)
    assert seen == []
    assert body["detail"] == "PE has no Authenticode certificate table"
    if expect_unsigned:
        assert code == 0
        assert body["status"] == "UNSIGNED_EXPECTED"
    else:
        assert code == 1
        assert body["status"] == "FAIL"


@pytest.mark.parametrize(
    "stdout",
    [
        '{"Status":"UnknownError","Status":"NotSigned"}',
        '{"Status":"UnknownError","Status":2}',
        '{"Status":"NotSigned","Status":"UnknownError"}',
        '{"Status":"NotSigned","StatusMessage":"synthetic","Status":"NotSigned"}',
    ],
)
@pytest.mark.parametrize("expect_unsigned", [False, True])
def test_duplicate_status_keys_are_ambiguous(tmp_path, monkeypatch, capsys, stdout, expect_unsigned):
    """A repeated Status key exits 1. The last key must not choose the verdict."""
    artifact = tmp_path / "captured.exe"
    _write_pe(artifact, 32)
    code, body, seen = _cli(monkeypatch, capsys, artifact, expect_unsigned, stdout)
    assert seen
    assert code == 1
    assert body["status"] == "FAIL"
    assert body["detail"] == "SIGNATURE_STATUS_AMBIGUOUS"
    assert body["status"] != "UNSIGNED_EXPECTED"
