"""Offline artifact checks using Python/YAML syntax and pwsh's nonexecuting parser."""

import ast
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pytest
import yaml

from tools.build_mediamtx import verify_windows_artifact_hashes

ROOT = Path(__file__).resolve().parents[1]


def _artifact_manifest(zip_bytes: bytes, exe_bytes: bytes) -> dict:
    """Return test-only SHA-256 pins for byte-exact fabricated artifacts."""
    return {
        "windows_zip_sha256": hashlib.sha256(zip_bytes).hexdigest(),
        "windows_exe_sha256": hashlib.sha256(exe_bytes).hexdigest(),
    }


def test_windows_artifact_hashes_accept_both_exact_pins(tmp_path: Path) -> None:
    """Accept a ZIP only when both the ZIP and executable match approved hashes."""
    archive = tmp_path / "mediamtx.zip"
    executable = tmp_path / "mediamtx.exe"
    archive.write_bytes(b"sample deterministic zip")
    executable.write_bytes(b"sample deterministic executable")
    manifest = _artifact_manifest(archive.read_bytes(), executable.read_bytes())
    assert verify_windows_artifact_hashes(archive, executable, manifest) == manifest["windows_zip_sha256"]


@pytest.mark.parametrize("modified_file", ["zip", "exe"])
def test_windows_artifact_hashes_reject_modified_input(
    tmp_path: Path, modified_file: str
) -> None:
    """Fail closed for independently modified ZIP or executable artifacts."""
    archive = tmp_path / "mediamtx.zip"
    executable = tmp_path / "mediamtx.exe"
    archive.write_bytes(b"trusted zip")
    executable.write_bytes(b"trusted exe")
    manifest = _artifact_manifest(archive.read_bytes(), executable.read_bytes())
    (archive if modified_file == "zip" else executable).write_bytes(b"modified")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        verify_windows_artifact_hashes(archive, executable, manifest)


def test_mediamtx_go_patch_pin_matches_build_consumers() -> None:
    """Keep the patched Go baseline consistent in Linux and Windows builders."""
    manifest = json.loads((ROOT / "infra/mediamtx/runtime.json").read_text())
    go_version = manifest["go_version"]
    assert manifest["x_net_version"] == "v0.60.0"
    assert manifest["x_net_checksum"] == "h1:79p50tfZlm0J9YfoDsSi639qSXNGVwEzOPLCxM2FsYU="
    assert go_version == "1.26.9"
    consumers = [
        (ROOT / "infra/mediamtx/Dockerfile").read_text(),
        (ROOT.parent / ".github/workflows/intelligent-vms-windows-field-test.yml").read_text(),
        (ROOT.parent / ".github/workflows/intelligent-vms-windows-installer.yml").read_text(),
    ]
    assert f"golang:{go_version}-alpine" in consumers[0]
    assert all(f"go-version: '{go_version}'" in content for content in consumers[1:])

@pytest.mark.parametrize("changed_field", ["Version", "Sum"])
def test_go_module_override_rejects_version_or_checksum_drift(changed_field: str) -> None:
    """Check that transitive x/net cannot roll back or change without detection."""
    from tools.build_mediamtx import verify_go_module_pin

    manifest = {
        "x_net_version": "v0.60.0",
        "x_net_checksum": "h1:79p50tfZlm0J9YfoDsSi639qSXNGVwEzOPLCxM2FsYU=",
    }
    module = {"Version": manifest["x_net_version"], "Sum": manifest["x_net_checksum"]}
    verify_go_module_pin(module, manifest)
    module[changed_field] = "unexpected"
    with pytest.raises(RuntimeError, match="version or checksum mismatch"):
        verify_go_module_pin(module, manifest)


@dataclass(frozen=True)
class _PinConsumer:
    """Identify a required executable checksum location without scanning unrelated text."""

    path: str
    hash_field: str
    kind: str
    selector: str
    functions: tuple[str, ...] = ()
    job: str = ""
    step: str = ""


_CONSUMER_PINS = (
    pytest.param(
        _PinConsumer("deploy/windows/Install-WindowsFieldTest.ps1", "windows_zip_sha256", "assignment", "mediaSha"),
        id="field-test-install-zip",
    ),
    pytest.param(
        _PinConsumer("deploy/windows/Install-WindowsFieldTest.ps1", "windows_exe_sha256", "assignment", "mediaExeSha"),
        id="field-test-install-executable",
    ),
    pytest.param(
        _PinConsumer("deploy/windows/installer/Build-Installer.ps1", "windows_zip_sha256", "assignment", "mediaSha"),
        id="unified-installer-build-zip",
    ),
    pytest.param(
        _PinConsumer(
            "tests/test_windows_unified_installer.py", "windows_zip_sha256", "python", "BUILD",
            functions=("test_v021_mediamtx_is_bundled_and_install_path_is_offline",),
        ),
        id="unified-installer-contract-zip",
    ),
    pytest.param(
        _PinConsumer(
            "tests/test_windows_field_test_release.py", "windows_zip_sha256", "python", "INSTALL",
            functions=(
                "test_windows_installer_is_non_docker_pins_media_and_protects_paths",
                "test_windows_mediamtx_integrity_does_not_depend_on_powershell_module_autoload",
            ),
        ),
        id="field-test-contract-zip",
    ),
    pytest.param(
        _PinConsumer(
            ".github/workflows/intelligent-vms-windows-field-test.yml", "windows_zip_sha256", "workflow", "$hash",
            job="windows-native-field-test", step="Prepare pinned MediaMTX direct-script fixture",
        ),
        id="field-test-ci-zip",
    ),
    pytest.param(
        _PinConsumer(
            ".github/workflows/intelligent-vms-windows-installer.yml", "windows_exe_sha256", "workflow",
            "(Get-FileHash -Algorithm SHA256 $installedMedia).Hash.ToLowerInvariant()",
            job="upgrade-and-negative", step="Accepted-baseline upgrade simulation",
        ),
        id="installer-upgrade-ci-executable",
    ),
)


def _consumer_file(relative_path: str) -> Path:
    """Map a product- or repo-relative pin consumer to its checked-in path."""
    return (ROOT.parent if relative_path.startswith(".github/") else ROOT) / relative_path


@lru_cache(maxsize=128)
def _powershell_references(source: str, kind: str, selector: str) -> list[dict]:
    """Ask the native parser for literal assignments/throwing guards, without executing source."""
    executable = shutil.which("pwsh")
    assert executable, "MediaMTX consumer integrity tests require pwsh on PATH; do not skip this gate"
    result = subprocess.run(
        [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
         str(ROOT / "tests/mediamtx_consumer_ast.ps1")],
        input=json.dumps({"source": source, "kind": kind, "selector": selector}),
        text=True, encoding="utf-8", capture_output=True, timeout=30, check=True,
    )
    return json.loads(result.stdout)


def _python_references(source: str, consumer: _PinConsumer) -> list[dict]:
    """Inspect SHA-256 assertions in named pytest functions, excluding strings and nested definitions."""
    references = []
    for function in ast.parse(source).body:
        if not isinstance(function, ast.FunctionDef) or function.name not in consumer.functions:
            continue
        parents = {child: parent for parent in ast.walk(function) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(function):
            if not isinstance(node, ast.Assert) or not isinstance(node.test, ast.Compare):
                continue
            comparison = node.test
            if (len(comparison.ops) != 1 or not isinstance(comparison.ops[0], ast.In)
                    or not isinstance(comparison.comparators[0], ast.Name)
                    or comparison.comparators[0].id != consumer.selector
                    or not isinstance(comparison.left, ast.Constant)
                    or not isinstance(comparison.left.value, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", comparison.left.value)):
                continue
            parent = parents[node]
            while parent is not function and not isinstance(
                parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                parent = parents[parent]
            if parent is function:
                references.append({
                    "value": comparison.left.value, "statement": ast.get_source_segment(source, node),
                    "direct": node in function.body, "scope": function.name,
                })
    return references


def _workflow_script(source: str, consumer: _PinConsumer) -> str:
    """Select the required unconditional pwsh run step; unrelated YAML scalars cannot satisfy it."""
    job = yaml.safe_load(source)["jobs"][consumer.job]
    steps = [step for step in job["steps"] if step.get("name") == consumer.step]
    assert len(steps) == 1, "Missing or duplicate MediaMTX checksum workflow step"
    step = steps[0]
    assert step.get("shell") == "pwsh" and "if" not in step and "if" not in job
    return step["run"]


def _consumer_references(source: str, consumer: _PinConsumer) -> list[dict]:
    """Dispatch to actual language syntax, rather than searching literal source text."""
    if consumer.kind == "python":
        return _python_references(source, consumer)
    if consumer.kind == "workflow":
        return _powershell_references(_workflow_script(source, consumer), "guard", consumer.selector)
    return _powershell_references(source, "assignment", consumer.selector)


def _assert_consumer_pin(path: Path, expected_sha256: str, consumer: _PinConsumer) -> None:
    """Require an unconditional consumer in each required scope and reject conflicting reassignments."""
    references = _consumer_references(path.read_text(encoding="utf-8"), consumer)
    required_scopes = set(consumer.functions) if consumer.kind == "python" else {""}
    direct_scopes = {reference["scope"] for reference in references if reference["direct"]}
    assert required_scopes <= direct_scopes, f"Missing MediaMTX checksum assignment in {path}"
    assert all(reference["value"] == expected_sha256 for reference in references), (
        f"Stale or conflicting MediaMTX checksum assignment in {path}"
    )


@pytest.mark.parametrize("consumer", _CONSUMER_PINS)
def test_verified_runtime_digests_match_all_offline_consumers(consumer: _PinConsumer) -> None:
    """Match each required executable ZIP/EXE consumer, excluding comments and literal content."""
    manifest = json.loads((ROOT / "infra/mediamtx/runtime.json").read_text())
    expected = manifest[consumer.hash_field]
    assert re.fullmatch(r"[0-9a-f]{64}", expected)
    _assert_consumer_pin(_consumer_file(consumer.path), expected, consumer)


@pytest.mark.parametrize("consumer", _CONSUMER_PINS)
def test_every_consumer_rejects_stale_and_missing_hash_assignment(
    tmp_path: Path, consumer: _PinConsumer
) -> None:
    """Reject one altered or absent consumer even while all other pins remain valid."""
    manifest = json.loads((ROOT / "infra/mediamtx/runtime.json").read_text())
    expected = manifest[consumer.hash_field]
    source = _consumer_file(consumer.path).read_text(encoding="utf-8")
    assert expected in source, f"Expected source pin missing: {consumer.path}"

    # The original test incorrectly passed if any other file had a good digest.
    # Keep the correct digest in a comment to reject comment-only false positives.
    corrupted = source.replace(expected, "0" * 64, 1)
    assert corrupted != source
    mutated_consumer = tmp_path / "mutated-consumer.txt"
    mutated_consumer.write_text(
        corrupted + f"\n# Historical checksum (not active): {expected}\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="Stale or conflicting"):
        _assert_consumer_pin(mutated_consumer, expected, consumer)
    with pytest.raises(FileNotFoundError):
        _assert_consumer_pin(tmp_path / "missing-consumer.txt", expected, consumer)


def _mutate_consumer(source: str, consumer: _PinConsumer, mutation: str, expected: str) -> str:
    """Alter parsed consumer statements while preserving every unrelated consumer and valid syntax."""
    statements = {reference["statement"] for reference in _consumer_references(source, consumer)}
    assert statements, "Mutation must first locate a real checksum consumer"
    for statement in statements:
        start = source.index(statement)
        indent = source[source.rfind("\n", 0, start) + 1:start]
        assert not indent.strip(), "Mutation expects a standalone consumer statement"
        if mutation == "missing-assignment":
            replacement = "pass" if consumer.kind == "python" else "# checksum consumer removed"
        elif mutation == "changed-hash":
            replacement = statement.replace(expected, "0" * 64)
        elif mutation in {"duplicate-identical", "duplicate-conflicting", "conditional-conflicting"}:
            duplicate = statement if mutation == "duplicate-identical" else statement.replace(expected, "0" * 64)
            if mutation == "conditional-conflicting":
                branch = "if True:" if consumer.kind == "python" else "if ($true) {"
                duplicate = branch + "\n" + indent + "    " + duplicate
                if consumer.kind != "python":
                    duplicate += "\n" + indent + "}"
            replacement = statement + "\n" + indent + duplicate
        elif mutation == "invoked-conflicting":
            duplicate = statement.replace(expected, "0" * 64)
            replacement = statement + "\n" + indent + "& {\n" + indent + duplicate + "\n" + indent + "}"
        elif mutation == "line-comment":
            replacement = "# " + statement
        elif consumer.kind == "python":
            replacement = _inactive_python(statement, indent, mutation)
        else:
            replacement = _inactive_powershell(statement, indent, mutation)
        source = source.replace(statement, replacement)
    if consumer.kind == "python":
        ast.parse(source)  # A syntax error must not masquerade as successful inactive-content rejection.
    return source


def _inactive_python(statement: str, indent: str, context: str) -> str:
    """Create syntactically valid inert strings, nested definitions, or unreachable statements."""
    if context == "unreachable":
        return "if False:\n" + indent + "    " + statement
    if context == "unused-function":
        return "def unused_checksum():\n" + indent + "    " + statement
    delimiters = {"block-literal": '"""', "single-triple": "'''", "raw-triple": 'r"""'}
    if context in delimiters:
        opener = delimiters[context]
        closer = opener.removeprefix("r")
        return opener + "\n" + indent + statement + "\n" + indent + closer
    assert context == "ordinary-string"
    return repr(statement)


def _inactive_powershell(statement: str, indent: str, context: str) -> str:
    """Create native PowerShell comments, here-strings, and captured/nonexecuting scopes."""
    if context == "block-literal":
        return "<#\n" + indent + statement + "\n" + indent + "#>"
    if context in {"single-here-string", "double-here-string"}:
        quote = "'" if context == "single-here-string" else '"'
        return "$unused = @" + quote + "\n" + indent + statement + "\n" + indent + quote + "@"
    if context == "ordinary-string":
        return "$unused = '" + statement.replace("'", "''") + "'"
    prefix = {"captured-scriptblock": "$unused = {", "unused-function": "function Unused-Checksum {",
              "unreachable": "if ($false) {"}[context]
    return prefix + "\n" + indent + "    " + statement + "\n" + indent + "}"


_INACTIVE_CASES = [
    pytest.param(consumer, context, id=f"{parameter.id}-{context}")
    for parameter in _CONSUMER_PINS
    for consumer in parameter.values
    for context in (
        ("block-literal", "single-triple", "raw-triple", "ordinary-string", "unused-function")
        if consumer.kind == "python" else
        ("block-literal", "single-here-string", "double-here-string", "ordinary-string",
         "captured-scriptblock", "unused-function")
    )
]


@pytest.mark.parametrize("consumer,context", _INACTIVE_CASES)
def test_actual_consumer_entry_rejects_inactive_checksum_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumer: _PinConsumer, context: str
) -> None:
    """Reject every reviewer false-pass through the real entry point, including workflow run bodies."""
    expected = json.loads((ROOT / "infra/mediamtx/runtime.json").read_text())[consumer.hash_field]
    source = _consumer_file(consumer.path).read_text()
    mutated = tmp_path / "consumer.txt"
    mutated.write_text(_mutate_consumer(source, consumer, context, expected), encoding="utf-8")
    original_file = _consumer_file
    monkeypatch.setattr(
        sys.modules[__name__], "_consumer_file",
        lambda relative: mutated if relative == consumer.path else original_file(relative),
    )
    with pytest.raises(AssertionError, match="Missing MediaMTX checksum assignment"):
        test_verified_runtime_digests_match_all_offline_consumers(consumer)


@pytest.mark.parametrize("consumer", _CONSUMER_PINS)
@pytest.mark.parametrize("mutation", [
    "missing-assignment", "changed-hash", "duplicate-identical", "duplicate-conflicting",
    "conditional-conflicting", "unreachable",
])
def test_each_consumer_requires_present_consistent_executable_pins(
    tmp_path: Path, consumer: _PinConsumer, mutation: str
) -> None:
    """Keep duplicate correct pins valid, but reject missing, conditional-only and conflicting pins."""
    expected = json.loads((ROOT / "infra/mediamtx/runtime.json").read_text())[consumer.hash_field]
    source = _consumer_file(consumer.path).read_text()
    mutated = tmp_path / "consumer.txt"
    mutated.write_text(_mutate_consumer(source, consumer, mutation, expected), encoding="utf-8")
    if mutation == "duplicate-identical":
        _assert_consumer_pin(mutated, expected, consumer)
    else:
        with pytest.raises(AssertionError, match="Missing|Stale or conflicting"):
            _assert_consumer_pin(mutated, expected, consumer)


@pytest.mark.parametrize("consumer", [parameter for parameter in _CONSUMER_PINS
                                     if parameter.values[0].kind != "python"])
def test_invoked_scriptblock_cannot_hide_conflicting_checksum(tmp_path: Path, consumer: _PinConsumer) -> None:
    """A called script block is executable; it must not be mistaken for an inert captured literal."""
    expected = json.loads((ROOT / "infra/mediamtx/runtime.json").read_text())[consumer.hash_field]
    source = _consumer_file(consumer.path).read_text()
    mutated = tmp_path / "consumer.txt"
    mutated.write_text(_mutate_consumer(source, consumer, "invoked-conflicting", expected), encoding="utf-8")
    with pytest.raises(AssertionError, match="Stale or conflicting"):
        _assert_consumer_pin(mutated, expected, consumer)


@pytest.mark.parametrize("consumer", _CONSUMER_PINS)
def test_removed_consumer_fails_actual_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumer: _PinConsumer
) -> None:
    """A removed consumer must fail even while the other six mappings remain unchanged."""
    original_file = _consumer_file
    monkeypatch.setattr(
        sys.modules[__name__], "_consumer_file",
        lambda relative: tmp_path / "missing.txt" if relative == consumer.path else original_file(relative),
    )
    with pytest.raises(FileNotFoundError):
        test_verified_runtime_digests_match_all_offline_consumers(consumer)


@pytest.mark.parametrize("consumer", [parameter for parameter in _CONSUMER_PINS
                                     if parameter.values[0].kind == "workflow"])
@pytest.mark.parametrize("mutation", ["inactive-yaml", "disabled-step", "removed-throw", "wrong-operator"])
def test_workflow_pin_requires_executable_run_step_and_throwing_mismatch_guard(
    tmp_path: Path, consumer: _PinConsumer, mutation: str
) -> None:
    """Reject YAML metadata-only pins, disabled steps, and comparisons that no longer enforce integrity."""
    expected = json.loads((ROOT / "infra/mediamtx/runtime.json").read_text())[consumer.hash_field]
    source = _consumer_file(consumer.path).read_text()
    workflow = yaml.safe_load(source)
    step = next(step for step in workflow["jobs"][consumer.job]["steps"] if step.get("name") == consumer.step)
    statement = _consumer_references(source, consumer)[0]["statement"]
    if mutation == "disabled-step":
        step["if"] = "false"
    elif mutation == "inactive-yaml":
        step["env"] = {"HISTORICAL_CHECKSUM_GUARD": statement}
        step["run"] = step["run"].replace(statement, "# removed checksum guard")
    else:
        changed = statement.replace("throw ", "Write-Host ") if mutation == "removed-throw" else (
            statement.replace(" -ne ", " -eq ")
        )
        step["run"] = step["run"].replace(statement, changed)
    mutated = tmp_path / "workflow.yaml"
    mutated.write_text(yaml.safe_dump(workflow), encoding="utf-8")
    with pytest.raises(AssertionError):
        _assert_consumer_pin(mutated, expected, consumer)


def test_powershell_parser_never_executes_consumer_source(tmp_path: Path) -> None:
    """Parsing a valid consumer must not run its commands or create its proposed side effects."""
    marker = str(tmp_path / "must-not-exist").replace("'", "''")
    source = '$mediaSha = "' + "a" * 64 + f'"\n[IO.File]::WriteAllText(\'{marker}\', \'executed\')'
    references = _powershell_references(source, "assignment", "mediaSha")
    assert references[0]["value"] == "a" * 64 and references[0]["direct"]
    assert not (tmp_path / "must-not-exist").exists()


def test_missing_powershell_parser_fails_without_skipping(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unavailable parser is a failed prerequisite, never a silently weakened checksum gate."""
    monkeypatch.setattr(shutil, "which", lambda executable: None)
    with pytest.raises(AssertionError, match="require pwsh on PATH"):
        _powershell_references("# unavailable native parser fixture", "assignment", "mediaSha")
