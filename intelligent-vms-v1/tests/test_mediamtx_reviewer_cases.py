"""Independent PR-174 fixtures and isolated pytest witnesses for executable checksum enforcement."""

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

# Load the exact checker by path, including with pytest's importlib collection mode.
_SPEC = importlib.util.spec_from_file_location(
    "_mediamtx_review_guard", Path(__file__).with_name("test_mediamtx_build_integrity.py")
)
integrity = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = integrity
_SPEC.loader.exec_module(integrity)


@dataclass(frozen=True)
class _ReviewerCase:
    """Retain the published case identity, injected source and expected guard outcome."""

    case_id: str
    consumer: integrity._PinConsumer
    source: str | None
    mutation: str
    function: str = ""
    accepts: bool = False


def _reviewer_cases() -> list[_ReviewerCase]:
    """Construct C01–C60 independently from the published reviewer algorithm, without developer mutators."""
    cases = []
    manifest = json.loads((integrity.ROOT / "infra/mediamtx/runtime.json").read_text())

    def add(consumer, source, label, function="", accepts=False):
        cases.append(_ReviewerCase(f"C{len(cases) + 1:02}", consumer, source, label, function, accepts))

    for parameter in integrity._CONSUMER_PINS:
        consumer = parameter.values[0]
        if consumer.kind != "python":
            add(consumer, None, "missing file")
            continue
        source = integrity._consumer_file(consumer.path).read_text()
        pin = manifest[consumer.hash_field]
        add(consumer, source, "positive", accepts=True)
        add(consumer, None, "missing file")
        functions = {node.name: node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}
        for name in consumer.functions:
            function = functions[name]
            node = next(node for node in function.body if isinstance(node, ast.Assert)
                        and pin in ast.get_source_segment(source, node))
            lines = source.splitlines(True)
            statement = ast.get_source_segment(source, node)
            indent = " " * node.col_offset
            variants = {
                "line comment": "# " + statement,
                "multiline double": '"""\n' + indent + statement + '\n' + indent + '"""',
                "multiline single": "'''\n" + indent + statement + '\n' + indent + "'''",
                "multiline raw": 'r"""\n' + indent + statement + '\n' + indent + '"""',
                "ordinary string": repr(statement),
                "unreachable if": "if False:\n" + indent + "    " + statement,
                "unused nested function": "def unused():\n" + indent + "    " + statement,
                "missing assertion": "pass",
                "stale hex with correct comment": statement.replace(pin, "0" * 64) + " # " + pin,
                "conflicting valid hex": statement + "\n" + indent + statement.replace(pin, "0" * 64),
                "duplicate identical": statement + "\n" + indent + statement,
                "conditional conflicting": statement + "\n" + indent + "if True:\n" + indent + "    " +
                    statement.replace(pin, "0" * 64),
                "conflicting nonhex": statement + "\n" + indent + statement.replace(pin, "g" * 64),
                "conflicting expression": statement + '\n' + indent + 'assert "0" * 64 in ' + consumer.selector,
                "early return": "return\n" + indent + statement,
            }
            for label, replacement in variants.items():
                changed = ("".join(lines[:node.lineno - 1]) + indent + replacement + "\n" +
                           "".join(lines[node.end_lineno:]))
                add(consumer, changed, label, name, label == "duplicate identical")
            add(consumer, source + "\ndef " + name + "():\n    pass\n", "shadowed test function", name)
            decorator = '@pytest.mark.skip(reason="independent bypass probe")\n'
            add(consumer, "".join(lines[:function.lineno - 1]) + decorator +
                "".join(lines[function.lineno - 1:]), "skipped test", name)
    assert len(cases) == 60 and sum(case.accepts for case in cases) == 5
    return cases


_REVIEWER_CASES = _reviewer_cases()
_FORMER_FALSE_PASSES = [case for case in _REVIEWER_CASES if case.mutation in {
    "conflicting nonhex", "conflicting expression", "early return", "shadowed test function", "skipped test",
}]
_PYTHON_TARGETS = [(parameter.values[0], name) for parameter in integrity._CONSUMER_PINS
                   for name in parameter.values[0].functions]


def _check_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumer, source: str | None) -> None:
    """Redirect only the chosen consumer; invoke the unchanged public test entry point and real parsers."""
    fixture = tmp_path / "consumer.txt"
    if source is not None:
        fixture.write_text(source, encoding="utf-8")
    original_file = integrity._consumer_file
    monkeypatch.setattr(integrity, "_consumer_file",
                        lambda relative: fixture if relative == consumer.path else original_file(relative))
    integrity.test_verified_runtime_digests_match_all_offline_consumers(consumer)


@pytest.mark.parametrize("case", _REVIEWER_CASES, ids=lambda case: case.case_id)
def test_published_reviewer_matrix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: _ReviewerCase) -> None:
    """Preserve all 45 correct original outcomes and reject all 15 former false acceptances."""
    if case.source is not None:
        ast.parse(case.source)
    if case.accepts:
        _check_source(tmp_path, monkeypatch, case.consumer, case.source)
    else:
        with pytest.raises((AssertionError, FileNotFoundError)):
            _check_source(tmp_path, monkeypatch, case.consumer, case.source)


@pytest.mark.parametrize("consumer,name", _PYTHON_TARGETS, ids=["U", "W1", "W2"])
@pytest.mark.parametrize("mutation", [
    "binding", "import-binding", "attribute-disable", "module-disable", "module-skip", "skipif", "handler",
    "initializer-rebind", "definition-default-rebind", "definition-annotation-rebind",
])
def test_python_binding_and_flow_ambiguity_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumer, name: str, mutation: str
) -> None:
    """Reject ambiguous effective definitions and disabling metadata without evaluating consumer code."""
    source = integrity._consumer_file(consumer.path).read_text()
    if mutation == "binding":
        source += f"\n{name} = lambda: None\n"
    elif mutation == "import-binding":
        source += f"\nfrom builtins import print as {name}\n"
    elif mutation == "attribute-disable":
        source += f"\n{name}.__test__ = False\n"
    elif mutation == "initializer-rebind":
        source += f"\nignored = setattr({name}, '__test__', False)\n"
    elif mutation == "definition-default-rebind":
        source += f"\ndef unused(value=setattr({name}, '__test__', False)):\n    pass\n"
    elif mutation == "definition-annotation-rebind":
        source += f"\ndef unused(value: setattr({name}, '__test__', False)):\n    pass\n"
    elif mutation in {"module-disable", "module-skip"}:
        source += "\n__test__ = False\n" if mutation == "module-disable" else (
            '\nimport pytest\npytestmark = pytest.mark.skip(reason="probe")\n'
        )
    elif mutation == "skipif":
        source = source.replace(f"def {name}(", '@pytest.mark.skipif(False, reason="probe")\ndef ' + name + "(")
    else:
        function = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)
                        and node.name == name)
        statement = next(node for node in function.body if isinstance(node, ast.Assert))
        text = ast.get_source_segment(source, statement)
        source = source.replace(text, "try:\n        " + text + "\n    except AssertionError:\n        pass", 1)
    ast.parse(source)
    with pytest.raises(AssertionError):
        _check_source(tmp_path, monkeypatch, consumer, source)


@pytest.mark.parametrize("consumer", [parameter for parameter in integrity._CONSUMER_PINS
                                     if parameter.values[0].kind == "workflow"])
@pytest.mark.parametrize("level", ["step", "job"])
@pytest.mark.parametrize("setting", [True, "false", "${{ false }}"])
def test_failure_tolerant_workflow_cannot_satisfy_checksum_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumer, level: str, setting
) -> None:
    """Reject all four published failure-tolerance challenges, including ambiguous nonboolean variants."""
    workflow = yaml.safe_load(integrity._consumer_file(consumer.path).read_text())
    job = workflow["jobs"][consumer.job]
    step = next(step for step in job["steps"] if step.get("name") == consumer.step)
    (step if level == "step" else job)["continue-on-error"] = setting
    with pytest.raises(AssertionError, match="Failure-tolerant"):
        _check_source(tmp_path, monkeypatch, consumer, yaml.safe_dump(workflow))


_PROBE_PLUGIN = '''import json, os
from pathlib import Path
state = {"collected": [], "reports": []}
def pytest_collection_finish(session):
    for item in session.items:
        state["collected"].append({"nodeid": item.nodeid,
            "firstlineno": item.obj.__code__.co_firstlineno,
            "markers": [mark.name for mark in item.iter_markers()]})
def pytest_runtest_logreport(report):
    state["reports"].append({"when": report.when, "outcome": report.outcome,
        "detail": report.longreprtext if report.failed else ""})
def pytest_sessionfinish(session, exitstatus):
    state["exitstatus"] = int(exitstatus)
    Path(os.environ["VMS_PIN_PROBE_RESULT"]).write_text(json.dumps(state))
'''


def _run_selected_pytest(tmp_path: Path, consumer, source: str, name: str, stale: bool = False) -> dict:
    """Execute only a trusted static checksum test in a bounded isolated pytest process and retain its reports."""
    original = integrity._consumer_file(consumer.path)
    fixture = tmp_path / "test_pin_probe.py"
    observed = ""
    if stale:
        pin = json.loads((integrity.ROOT / "infra/mediamtx/runtime.json").read_text())[consumer.hash_field]
        observed = f"\n{consumer.selector} = {consumer.selector}.replace({pin!r}, '0' * 64)\n"
    fixture.write_text(f"__file__ = {str(original)!r}\n" + source + observed, encoding="utf-8")
    (tmp_path / "pin_probe_plugin.py").write_text(_PROBE_PLUGIN, encoding="utf-8")
    result = tmp_path / "outcomes.json"
    environment = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "VMS_PIN_PROBE_RESULT": str(result),
                   "PYTHONPATH": os.pathsep.join([str(tmp_path), str(integrity.ROOT),
                                                  str(integrity.ROOT / "services/control-api")])}
    process = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rA", "-c", os.devnull, "--confcutdir", str(tmp_path),
         "--import-mode=importlib", "-p", "no:cacheprovider", "-p", "pin_probe_plugin", f"{fixture}::{name}"],
        env=environment, cwd=tmp_path, capture_output=True, text=True, timeout=45,
    )
    (tmp_path / "pytest.log").write_text(process.stdout + process.stderr, encoding="utf-8")
    evidence = json.loads(result.read_text())
    assert evidence["exitstatus"] == process.returncode
    return evidence


@pytest.mark.parametrize("consumer,name", _PYTHON_TARGETS, ids=["U", "W1", "W2"])
@pytest.mark.parametrize("stale", [False, True], ids=["live-pin", "stale-observed-pin"])
def test_actual_pytest_collects_and_executes_required_live_assertion(
    tmp_path: Path, consumer, name: str, stale: bool
) -> None:
    """Require exactly one unskipped live test; stale observed bytes must cause an executed assertion failure."""
    source = integrity._consumer_file(consumer.path).read_text()
    evidence = _run_selected_pytest(tmp_path, consumer, source, name, stale)
    function = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == name)
    assert len(evidence["collected"]) == 1
    assert evidence["collected"][0]["firstlineno"] == function.lineno + 1
    assert evidence["collected"][0]["markers"] == []
    calls = [report for report in evidence["reports"] if report["when"] == "call"]
    assert len(calls) == 1 and not any(report["outcome"] == "skipped" for report in evidence["reports"])
    assert calls[0]["outcome"] == ("failed" if stale else "passed")
    assert evidence["exitstatus"] == (1 if stale else 0)
    if stale:
        assert "AssertionError" in calls[0]["detail"]


@pytest.mark.parametrize("case", _FORMER_FALSE_PASSES, ids=lambda case: case.case_id)
def test_actual_pytest_outcomes_for_original_false_pass_fixtures(tmp_path: Path, case: _ReviewerCase) -> None:
    """Qualify original negatives with real execution: assertion failures, no-op passes, skips and collection errors."""
    evidence = _run_selected_pytest(tmp_path, case.consumer, case.source, case.function)
    calls = [report for report in evidence["reports"] if report["when"] == "call"]
    if case.case_id == "C22":
        assert evidence["exitstatus"] == 4 and evidence["collected"] == [] and calls == []
        assert "NameError" in (tmp_path / "pytest.log").read_text()
    elif case.mutation == "skipped test":
        assert evidence["exitstatus"] == 0 and calls == []
        assert len(evidence["collected"]) == 1 and "skip" in evidence["collected"][0]["markers"]
        assert any(report["when"] == "setup" and report["outcome"] == "skipped" for report in evidence["reports"])
    else:
        assert len(evidence["collected"]) == 1 and len(calls) == 1
        definitions = [node for node in ast.parse(case.source).body if isinstance(node, ast.FunctionDef)
                       and node.name == case.function]
        assert len(definitions) == (2 if case.mutation == "shadowed test function" else 1)
        assert evidence["collected"][0]["firstlineno"] == definitions[-1].lineno + 1
        fails = case.mutation in {"conflicting nonhex", "conflicting expression"}
        assert calls[0]["outcome"] == ("failed" if fails else "passed")
        assert evidence["exitstatus"] == (1 if fails else 0)
        if fails:
            assert "AssertionError" in calls[0]["detail"]


@pytest.mark.parametrize("case", [case for case in _FORMER_FALSE_PASSES
                                  if case.mutation in {"early return", "shadowed test function"}],
                         ids=lambda case: case.case_id)
def test_inactive_negative_still_passes_pytest_with_wrong_observed_pin_but_guard_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: _ReviewerCase
) -> None:
    """Demonstrate the six original false-green stale-observation executions without accepting them as valid guards."""
    with pytest.raises(AssertionError):
        _check_source(tmp_path, monkeypatch, case.consumer, case.source)
    monkeypatch.undo()
    evidence = _run_selected_pytest(tmp_path, case.consumer, case.source, case.function, stale=True)
    assert evidence["exitstatus"] == 0 and len(evidence["collected"]) == 1
    calls = [report for report in evidence["reports"] if report["when"] == "call"]
    assert len(calls) == 1 and calls[0]["outcome"] == "passed"


def test_import_corrected_c22_skip_is_rejected_and_actually_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the supplemental import-corrected skip separate from original C22's collection error."""
    case = next(case for case in _REVIEWER_CASES if case.case_id == "C22")
    source = "import pytest\n" + case.source
    with pytest.raises(AssertionError):
        _check_source(tmp_path, monkeypatch, case.consumer, source)
    monkeypatch.undo()
    evidence = _run_selected_pytest(tmp_path, case.consumer, source, case.function)
    assert len(evidence["collected"]) == 1 and evidence["exitstatus"] == 0
    assert not any(report["when"] == "call" for report in evidence["reports"])
    assert any(report["outcome"] == "skipped" for report in evidence["reports"])


@pytest.mark.parametrize("consumer", [parameter for parameter in integrity._CONSUMER_PINS
                                     if parameter.values[0].kind == "assignment"])
@pytest.mark.parametrize("mutation", [
    "return", "exit", "script-scope", "global-scope", "set-variable", "variable-alias", "expandable-write",
])
def test_native_powershell_dead_pins_and_unsupported_writes_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumer, mutation: str
) -> None:
    """Run native pwsh on the reviewer's previously unexecuted scope, flow and indirect-write challenges."""
    expected = json.loads((integrity.ROOT / "infra/mediamtx/runtime.json").read_text())[consumer.hash_field]
    base = f'${consumer.selector} = "{expected}"\n'
    if mutation in {"return", "exit"}:
        source = mutation + "\n" + base
    elif mutation in {"script-scope", "global-scope"}:
        scope = mutation.split("-")[0]
        source = base + f'${scope}:{consumer.selector} = "' + "0" * 64 + '"\n'
    elif mutation in {"set-variable", "variable-alias"}:
        command = "Set-Variable" if mutation == "set-variable" else "sv"
        source = base + f'{command} -Name {consumer.selector} -Value "' + "0" * 64 + '"\n'
    else:
        source = base + '$unused = "$($' + consumer.selector + " = '" + "0" * 64 + "')\"\n"
    with pytest.raises(AssertionError, match="Missing|Stale or conflicting"):
        _check_source(tmp_path, monkeypatch, consumer, source)


@pytest.mark.parametrize("consumer", [parameter for parameter in integrity._CONSUMER_PINS
                                     if parameter.values[0].kind == "workflow"])
@pytest.mark.parametrize("mutation", ["return-before-guard", "return-before-throw", "exit-before-throw"])
def test_native_powershell_guard_must_reach_its_throw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumer, mutation: str
) -> None:
    """A literal guard after return, or a return/exit before throw, cannot enforce a workflow checksum."""
    expected = json.loads((integrity.ROOT / "infra/mediamtx/runtime.json").read_text())[consumer.hash_field]
    guard = f'if ({consumer.selector} -ne "{expected}") {{ throw "mismatch" }}\n'
    if mutation == "return-before-guard":
        guard = "return\n" + guard
    else:
        guard = guard.replace("throw", ("return; " if mutation == "return-before-throw" else "exit; ") + "throw")
    workflow = yaml.safe_load(integrity._consumer_file(consumer.path).read_text())
    step = next(step for step in workflow["jobs"][consumer.job]["steps"] if step.get("name") == consumer.step)
    step["run"] = guard
    with pytest.raises(AssertionError, match="Missing"):
        _check_source(tmp_path, monkeypatch, consumer, yaml.safe_dump(workflow))


_ASSIGNMENT_BYPASSES = (
    "typed-write", "invoke-expression", "variable-provider", "dynamic-set-variable", "session-state",
    "conditional-return", "conditional-exit",
)
_GUARD_BYPASSES = ("trap-before", "trap-after", "conditional-return", "conditional-exit")


def _powershell_bypass_source(consumer, mutation: str) -> str:
    """Build trusted, finite native witnesses for the reported control-flow and variable-write misses."""
    expected = json.loads((integrity.ROOT / "infra/mediamtx/runtime.json").read_text())[consumer.hash_field]
    stale = "1" * 64
    if consumer.kind == "workflow":
        guard = f'if ({consumer.selector} -ne "{expected}") {{ throw "checksum mismatch" }}\n'
        if mutation.startswith("trap-"):
            return "trap { continue }\n" + guard if mutation == "trap-before" else guard + "trap { continue }\n"
        return f'if ($true) {{ {mutation.split("-")[1]} }}\n' + guard
    variable = consumer.selector
    base = f'${variable} = "{expected}"\n'
    writes = {
        "typed-write": f'[string]${variable} = "{stale}"\n',
        "invoke-expression": f'Invoke-Expression \'${variable} = "{stale}"\'\n',
        "variable-provider": f'Set-Item -Path variable:{variable} -Value "{stale}"\n',
        "dynamic-set-variable": f'& ("Set"+"-Variable") {variable} "{stale}"\n',
        "session-state": f'$ExecutionContext.SessionState.PSVariable.Set("{variable}", "{stale}")\n',
    }
    if mutation.startswith("conditional-"):
        return f'if ($true) {{ {mutation.split("-")[1]} }}\n' + base
    return base + writes[mutation]


def _check_powershell_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumer, source: str) -> None:
    """Route a native snippet through the real consumer entry, preserving the workflow envelope when required."""
    if consumer.kind == "workflow":
        workflow = yaml.safe_load(integrity._consumer_file(consumer.path).read_text())
        step = next(step for step in workflow["jobs"][consumer.job]["steps"] if step.get("name") == consumer.step)
        step["run"] = source
        source = yaml.safe_dump(workflow)
    _check_source(tmp_path, monkeypatch, consumer, source)


_NATIVE_BYPASSES = [
    pytest.param(parameter.values[0], mutation, id=f"{parameter.id}-{mutation}")
    for parameter in integrity._CONSUMER_PINS if parameter.values[0].kind != "python"
    for mutation in (_GUARD_BYPASSES if parameter.values[0].kind == "workflow" else _ASSIGNMENT_BYPASSES)
]


@pytest.mark.parametrize("consumer,mutation", _NATIVE_BYPASSES)
def test_reported_native_bypasses_fail_real_consumer_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumer, mutation: str
) -> None:
    """Reject every reported native false accept through the public ZIP/EXE consumer test entry."""
    source = _powershell_bypass_source(consumer, mutation)
    with pytest.raises(AssertionError, match="Missing|Stale or conflicting"):
        _check_powershell_script(tmp_path, monkeypatch, consumer, source)


@pytest.mark.parametrize("consumer,mutation", _NATIVE_BYPASSES)
def test_native_wrong_hash_witnesses_demonstrate_why_bypasses_are_rejected(
    tmp_path: Path, consumer, mutation: str
) -> None:
    """Execute only the trusted synthetic fixtures: stale writes or skipped/suppressed guards exit successfully."""
    source = _powershell_bypass_source(consumer, mutation)
    prefix = 'Write-Output "WITNESS_STARTED"\n'
    if consumer.kind == "workflow":
        if consumer.selector == "$hash":
            prefix += '$hash="' + "1" * 64 + '"\n'
        else:
            prefix += ('function Get-FileHash { [pscustomobject]@{Hash="' + "1" * 64 +
                       '"} }\n$installedMedia="fixture"\n')
        suffix = 'Write-Output "GUARD_COMPLETED"\n'
    else:
        suffix = f'Write-Output ("PIN=" + ${consumer.selector})\n'
    fixture = tmp_path / "native-witness.ps1"
    fixture.write_text(prefix + source + suffix, encoding="utf-8")
    executable = shutil.which("pwsh")
    assert executable, "Native PowerShell witnesses require pwsh; do not skip this gate"
    process = subprocess.run([executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(fixture)],
                             text=True, capture_output=True, timeout=30)
    assert process.returncode == 0 and "WITNESS_STARTED" in process.stdout, process.stderr
    if mutation.startswith("conditional-"):
        assert "PIN=" not in process.stdout and "GUARD_COMPLETED" not in process.stdout
    elif consumer.kind == "workflow":
        assert "GUARD_COMPLETED" in process.stdout
    else:
        assert "PIN=" + "1" * 64 in process.stdout


@pytest.mark.parametrize("consumer", [parameter for parameter in integrity._CONSUMER_PINS
                                     if parameter.values[0].kind == "workflow"])
def test_native_valid_guard_rejects_wrong_observed_hash(tmp_path: Path, consumer) -> None:
    """Control both native selectors: a real stale digest reaches throw and fails the PowerShell process."""
    expected = json.loads((integrity.ROOT / "infra/mediamtx/runtime.json").read_text())[consumer.hash_field]
    if consumer.selector == "$hash":
        prefix = '$hash="' + "1" * 64 + '"\n'
    else:
        prefix = ('function Get-FileHash { [pscustomobject]@{Hash="' + "1" * 64 +
                  '"} }\n$installedMedia="fixture"\n')
    fixture = tmp_path / "valid-guard.ps1"
    fixture.write_text(prefix + f'if ({consumer.selector} -ne "{expected}") {{ throw "CHECKSUM_REJECTED" }}\n'
                       + 'Write-Output "GUARD_COMPLETED"\n', encoding="utf-8")
    executable = shutil.which("pwsh")
    assert executable, "Native PowerShell witnesses require pwsh; do not skip this gate"
    process = subprocess.run([executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(fixture)],
                             text=True, capture_output=True, timeout=30)
    assert process.returncode != 0 and "CHECKSUM_REJECTED" in process.stderr
    assert "GUARD_COMPLETED" not in process.stdout


@pytest.mark.parametrize("consumer", [parameter for parameter in integrity._CONSUMER_PINS
                                     if parameter.values[0].kind != "python"])
def test_native_contract_preserves_preflight_throws_and_unrelated_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumer
) -> None:
    """Accept legitimate preflight throws, literal contents, unused functions and unrelated filesystem setup."""
    expected = json.loads((integrity.ROOT / "infra/mediamtx/runtime.json").read_text())[consumer.hash_field]
    source = ('if ($false) { throw "preflight failure" }\n'
              '# trap { continue }; if ($true) { return }\n'
              'function Unused { trap { continue }; return }\n'
              '[string]$unrelated = "value"\n'
              'New-Item -ItemType Directory -Path "fixture-directory"\n')
    source += (f'if ({consumer.selector} -ne "{expected}") {{ throw "checksum mismatch" }}\n'
               if consumer.kind == "workflow" else f'${consumer.selector} = "{expected}"\n')
    _check_powershell_script(tmp_path, monkeypatch, consumer, source)


# F1: checksum guards must throw a constant string, not evaluate an operand
# that can return/exit before the exception is raised.
_WORKFLOW_GUARD_CONSUMERS = [
    parameter.values[0] for parameter in integrity._CONSUMER_PINS
    if parameter.values[0].kind == "workflow"
]


def _current_workflow_checksum_guard(consumer):
    """Read the real CI guard without executing it or trusting a synthetic replacement."""
    source = integrity._consumer_file(consumer.path).read_text(encoding="utf-8")
    workflow = yaml.safe_load(source)
    step = next(item for item in workflow["jobs"][consumer.job]["steps"]
                if item.get("name") == consumer.step)
    digest = json.loads((integrity.ROOT / "infra/mediamtx/runtime.json").read_text())[
        consumer.hash_field]
    candidates = [line.strip() for line in step["run"].splitlines()
                  if digest in line and "{throw " in line]
    assert len(candidates) == 1, "Expected exactly one currently enforced checksum guard"
    return workflow, step, candidates[0]


def _wrong_digest_prefix(consumer):
    """Use a synthetic wrong digest; never touch a real installer or certificate."""
    stale = "1" * 64
    if consumer.selector == "$hash":
        return f'$hash="{stale}"\n'
    return ('function Get-FileHash { [pscustomobject]@{Hash="' + stale +
            '"} }\n$installedMedia="synthetic-fixture"\n')


@pytest.mark.parametrize("consumer", _WORKFLOW_GUARD_CONSUMERS, ids=["zip", "exe"])
@pytest.mark.parametrize("exit_form", ["return", "exit 0"], ids=["return", "exit-zero"])
def test_f1_nested_exit_is_rejected_through_actual_workflow_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumer, exit_form: str
) -> None:
    """Four independently constructed PR #174 public-entry mutations must fail closed."""
    workflow, step, original = _current_workflow_checksum_guard(consumer)
    before, separator, _ = original.partition("{throw ")
    assert separator and original.endswith("}")
    mutated = before + "{throw $(" + exit_form + ")}"
    assert step["run"].count(original) == 1
    step["run"] = step["run"].replace(original, mutated, 1)
    with pytest.raises(AssertionError, match="Missing MediaMTX checksum assignment"):
        _check_source(tmp_path, monkeypatch, consumer, yaml.safe_dump(workflow))


@pytest.mark.parametrize("consumer", _WORKFLOW_GUARD_CONSUMERS, ids=["zip", "exe"])
@pytest.mark.parametrize("exit_form", ["return", "exit 0"], ids=["return", "exit-zero"])
def test_f1_nested_exit_native_wrong_digest_witness(
    tmp_path: Path, consumer, exit_form: str
) -> None:
    """Execute trusted finite PowerShell snippets to prove both nested exits bypass throw."""
    _, _, original = _current_workflow_checksum_guard(consumer)
    mutated = original.partition("{throw ")[0] + "{throw $(" + exit_form + ")}"
    fixture = tmp_path / "f1-native-witness.ps1"
    fixture.write_text('Write-Output "WITNESS_STARTED"\n' +
                       _wrong_digest_prefix(consumer) + mutated + '\n' +
                       'Write-Output "GUARD_COMPLETED"\n', encoding="utf-8")
    executable = shutil.which("pwsh")
    assert executable, "Native PowerShell is mandatory for F1 verification"
    process = subprocess.run([executable, "-NoLogo", "-NoProfile", "-NonInteractive",
                              "-File", str(fixture)], text=True, capture_output=True, timeout=30)
    assert process.returncode == 0 and "WITNESS_STARTED" in process.stdout, process.stderr
    assert "GUARD_COMPLETED" not in process.stdout


@pytest.mark.parametrize("consumer", _WORKFLOW_GUARD_CONSUMERS, ids=["zip", "exe"])
def test_f1_real_unmodified_guard_fails_on_wrong_digest(tmp_path: Path, consumer) -> None:
    """The two original workflow guard statements must actually fail the native process."""
    _, _, original = _current_workflow_checksum_guard(consumer)
    fixture = tmp_path / "f1-original-guard.ps1"
    fixture.write_text(_wrong_digest_prefix(consumer) + original + '\n' +
                       'Write-Output "GUARD_COMPLETED"\n', encoding="utf-8")
    executable = shutil.which("pwsh")
    assert executable, "Native PowerShell is mandatory for F1 verification"
    process = subprocess.run([executable, "-NoLogo", "-NoProfile", "-NonInteractive",
                              "-File", str(fixture)], text=True, capture_output=True, timeout=30)
    assert process.returncode != 0, process.stdout + process.stderr
    assert "GUARD_COMPLETED" not in process.stdout
