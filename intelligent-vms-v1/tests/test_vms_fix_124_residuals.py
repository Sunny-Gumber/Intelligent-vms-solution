"""Regression locks for FIX-037 residuals REV-037-001 through REV-037-006.

These cases are the Low residuals from the FIX-037 review. They must fail
closed: duplicate JSON keys, identity-flag booleans, a zero capacity, a direct
matrix call after a reproducibility failure, a depth-32 path, and the markdown
path inventory.
"""

import copy
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import phase8_benchmark_common as common
import phase8_hardware_matrix as matrix
import phase8_reproducibility as reproducibility


_NUMERIC_FLAG_TEXT = {format(0.0, ".17g"), format(1.0, ".17g"), "0", "1", "0.0", "1.0"}
_ZERO_CAPACITY_CASES = (
    ("synthetic-storage-write", "aggregate_write_mbps", "storage", "storage_write_mbps"),
    ("recording-directory-growth", "observed_recording_mbps", "recording", "recording_mbps"),
    ("media-relay", "observed_media_mbps", "media", "media_mbps"),
    ("ai-inference", "observed_ai_mpix_s", "ai", "ai_mpix_s"),
    ("event-ingest-http", "throughput_ops_s", "event_ingest", "events_per_second"),
    ("control-api", "throughput_ops_s", "control_api", "control_ops_per_second"),
)


def _sample():
    return {
        "cpu_pct": 10.0,
        "cpu_freq_mhz": 2400.0,
        "cpu_freq_max_mhz": None,
        "max_temperature_c": None,
        "ram_used_bytes": 1024,
        "ram_pct": 40.0,
        "net_rx_mbps": 1.0,
        "net_tx_mbps": 1.0,
        "disk_read_mbps": 1.0,
        "disk_write_mbps": 1.0,
        "gpu": [],
    }


def _pin(result):
    resources = result["resources"]
    resources["cpu_pct"] = {"mean": 10.0, "p95": 20.0, "max": 30.0}
    resources["ram_pct"] = {"mean": 40.0, "p95": 50.0, "max": 60.0}
    resources["cpu_freq_mhz"] = {"mean": 2400.0, "p95": 2400.0, "max": 2400.0}
    for key in (
        "ram_used_bytes",
        "net_rx_mbps",
        "net_tx_mbps",
        "disk_read_mbps",
        "disk_write_mbps",
    ):
        resources[key] = {"mean": 1.0, "p95": 2.0, "max": 3.0}
    resources["cpu_freq_max_mhz"] = {"mean": None, "p95": None, "max": None}
    resources["max_temperature_c"] = {"mean": None, "p95": None, "max": None}
    resources["cpu_freq_ratio_min"] = None
    hardware = result["environment"]["hardware"]
    hardware["network_interfaces"] = [
        {"name": "eth0", "is_up": True, "speed_mbps": None, "mtu": 1500}
    ]
    return result


def _record(workload_type, extra=None):
    result = common.build_result(
        workload_type=workload_type,
        workload_config={"path": "/tmp/vms-fix-124", "streams": 1, "fsync_each_chunk": False},
        started_at="2026-10-08T00:00:00+00:00",
        duration_seconds=600.0,
        warmup_seconds=60.0,
        operations_ok=4,
        operations_failed=0,
        latencies_seconds=[0.001, 0.002, 0.003, 0.004],
        samples=[_sample()],
        extra_metrics=extra,
    )
    return _pin(result)


def _storage_record():
    return _record(
        "synthetic-storage-write",
        {
            "bytes_written": 1048576,
            "aggregate_write_mbps": 20000.0,
            "aggregate_write_MBps": 2500.0,
        },
    )


def _demand(role, dimension, requested=1500.0):
    return {
        "required_roles": {role: dimension},
        "profiles": [
            {"name": "probe", "cameras": 1, "demands": {dimension: requested}}
        ],
    }


def _storage_demand():
    return _demand("storage", "storage_write_mbps")


def _report(results):
    return reproducibility.build_report(
        results=results,
        min_repeats=1,
        min_duration_seconds=0,
        min_warmup_seconds=0,
    )


def _matrix(results, demand):
    return matrix.build_matrix(
        results=results,
        demand=demand,
        min_repeats=1,
    )


def _exported_code_paths():
    paths = set()
    for kind in ("storage", "reconnect", "hardware-matrix", "reproducibility"):
        paths.update(common.required_measured_fields(kind))
    for workload in (
        "synthetic-storage-write",
        "tcp-reconnect-storm",
        "event-ingest-http",
        "recording-directory-growth",
        "media-relay",
        "ai-inference",
        "control-api",
    ):
        paths.update(common.required_measured_fields_for_workload(workload))
    return paths


def _duplicate_failure_rate_text(record, first, second):
    text = json.dumps(record)
    needle = '"failure_rate": 0.0'
    assert needle in text
    replacement = f'"failure_rate": {first}, "failure_rate": {second}'
    return text.replace(needle, replacement, 1)


def test_duplicate_json_key_names_the_path(tmp_path):
    record = _storage_record()
    text = _duplicate_failure_rate_text(record, "{}", "0.0")
    path = tmp_path / "duplicate.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=r"duplicate JSON key: result\.failure_rate"):
        reproducibility.load_results([str(path)])
    with pytest.raises(ValueError, match=r"duplicate JSON key: result\.failure_rate"):
        matrix.load_results([str(path)])


def test_duplicate_trailing_number_cannot_qualify(tmp_path):
    record = _storage_record()
    text = _duplicate_failure_rate_text(record, "{}", "0.0")
    result_dir = tmp_path / "results"
    result_dir.mkdir()
    (result_dir / "duplicate.json").write_text(text, encoding="utf-8")
    demand_path = tmp_path / "demand.json"
    demand_path.write_text(json.dumps(_storage_demand()), encoding="utf-8")
    report_path = tmp_path / "reproducibility.json"
    matrix_path = tmp_path / "hardware-matrix.json"
    report_cli = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "phase8_reproducibility.py"),
            "--results",
            str(result_dir),
            "--output",
            str(report_path),
            "--min-repeats",
            "1",
            "--min-duration-seconds",
            "0",
            "--min-warmup-seconds",
            "0",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert report_path.is_file(), report_cli.stderr
    assert "Traceback" not in report_cli.stderr
    written = json.loads(report_path.read_text(encoding="utf-8"))
    rendered = json.dumps(written)
    assert "duplicate JSON key: result.failure_rate" in rendered
    assert written["summary"]["passed"] == 0
    matrix_cli = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "phase8_hardware_matrix.py"),
            "--results",
            str(result_dir),
            "--demand",
            str(demand_path),
            "--reproducibility-report",
            str(report_path),
            "--output",
            str(matrix_path),
            "--min-repeats",
            "1",
            "--min-duration-seconds",
            "0",
            "--min-warmup-seconds",
            "0",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert matrix_path.is_file(), matrix_cli.stderr
    assert "Traceback" not in matrix_cli.stderr
    produced = json.loads(matrix_path.read_text(encoding="utf-8"))
    assert produced["qualified_evidence"] == []
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in json.dumps(produced)


def test_identity_flags_are_not_numeric_zero_or_one():
    record = _storage_record()
    record["resources"]["thermal_measured"] = True
    record["resources"]["gpu_measured"] = False
    record["resources"]["thermal_limit_exceeded"] = False
    content = common._measured_content(record)
    for key in ("thermal_measured", "gpu_measured", "thermal_limit_exceeded"):
        assert content["resources"][key] not in _NUMERIC_FLAG_TEXT
    material = common.canonical_hardware_material(record)
    assert material["network_interfaces"][0]["is_up"] not in _NUMERIC_FLAG_TEXT
    shaped = common.workload_config_identity(record["workload"]["config"])
    assert shaped["fsync_each_chunk"] not in _NUMERIC_FLAG_TEXT


def test_boolean_and_integer_flags_still_share_one_fingerprint():
    true_record = _storage_record()
    one_record = copy.deepcopy(true_record)
    true_record["resources"]["thermal_measured"] = True
    one_record["resources"]["thermal_measured"] = 1
    true_record["resources"]["gpu_measured"] = False
    one_record["resources"]["gpu_measured"] = 0
    true_record["environment"]["hardware"]["network_interfaces"][0]["is_up"] = True
    one_record["environment"]["hardware"]["network_interfaces"][0]["is_up"] = 1
    assert common.content_fingerprint(true_record) == common.content_fingerprint(one_record)
    assert common.hardware_key(true_record) == common.hardware_key(one_record)
    assert "is_up" not in matrix._WORKLOAD_CAPACITY["synthetic-storage-write"]
    assert "thermal_measured" not in matrix._WORKLOAD_CAPACITY["synthetic-storage-write"]


def test_rejection_row_does_not_store_failure_rate_zero():
    record = _storage_record()
    record["result"]["failure_rate"] = False
    parsed = common.parse_benchmark_record(record)
    assert isinstance(parsed, common.Rejection)
    assert parsed.reason == "result.failure_rate is not a finite number"
    evidence = matrix.extract_evidence(
        record,
        min_duration_seconds=0,
        min_warmup_seconds=0,
        max_failure_rate=1,
        max_cpu_p95_pct=100,
        max_ram_p95_pct=100,
    )
    assert evidence is not None
    assert evidence.qualified is False
    assert evidence.failure_rate is None
    output = _matrix([record], _storage_demand())
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in json.dumps(output)
    assert "failure_rate" not in output["rejected_evidence"][0]


def test_empty_failure_rate_stays_missing_and_unset():
    record = _storage_record()
    record["result"]["failure_rate"] = {}
    parsed = common.parse_benchmark_record(record)
    assert not isinstance(parsed, common.Rejection)
    assert parsed.failure_rate is None
    assert "MISSING_MEASURED_FIELD:result.failure_rate" in parsed.null_measured_reasons
    output = _matrix([record], _storage_demand())
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in json.dumps(output)


@pytest.mark.parametrize("workload,key,role,dimension", _ZERO_CAPACITY_CASES)
@pytest.mark.parametrize("value", (0.0, -0.0))
def test_nonpositive_capacity_fails_reproducibility(workload, key, role, dimension, value):
    extra = None
    if workload == "synthetic-storage-write":
        extra = {
            "bytes_written": 1048576,
            "aggregate_write_mbps": 20000.0,
            "aggregate_write_MBps": 2500.0,
        }
    elif workload == "recording-directory-growth":
        extra = {"observed_recording_mbps": 10.0}
    elif workload == "media-relay":
        extra = {"observed_media_mbps": 10.0}
    elif workload == "ai-inference":
        extra = {"observed_ai_mpix_s": 10.0}
    record = _record(workload, extra)
    record["result"][key] = value
    report = _report([record])
    group = report["groups"][0]
    assert group["status"] == "FAIL"
    assert f"capacity metric result.{key} is not a positive finite number" in group["reasons"]
    assert "no hardware-qualification capacity metric is defined for this workload" not in group["warnings"]
    output = _matrix([record], _demand(role, dimension, 1.0))
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in json.dumps(output)
    role_row = output["profiles"][0]["roles"][role]
    assert "nodes_required" not in role_row


def test_complete_reconnect_repeat_can_stay_unqualified():
    record = _record("tcp-reconnect-storm")
    report = _report([record])
    group = report["groups"][0]
    assert group["status"] == "PASS"
    assert "no hardware-qualification capacity metric is defined for this workload" in group["warnings"]
    output = _matrix([record], _storage_demand())
    rendered = json.dumps(output)
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in rendered
    assert output["qualified_evidence"] == []
    assert "nodes_required" not in output["profiles"][0]["roles"]["storage"]


def test_direct_build_matrix_rejects_percentile_order_failure():
    record = _storage_record()
    record["result"]["latency"]["p95_ms"] = -0.0
    report = _report([record])
    assert report["groups"][0]["status"] == "FAIL"
    assert any("latency percentile ordering invalid" in reason for reason in report["groups"][0]["reasons"])
    output = _matrix([record], _storage_demand())
    role = output["profiles"][0]["roles"]["storage"]
    assert role["status"] == "UNQUALIFIED"
    assert "nodes_required" not in role
    assert output["qualified_evidence"] == []
    rejected = " ".join(reason for row in output["rejected_evidence"] for reason in row["reasons"])
    assert "latency percentile ordering invalid" in rejected
    assert record["benchmark_id"] in rejected


def test_direct_build_matrix_rejects_thermal_limit_failure():
    record = _storage_record()
    record["resources"]["thermal_limit_exceeded"] = True
    report = _report([record])
    assert report["groups"][0]["status"] == "FAIL"
    assert any("thermal high/critical limit observed" in reason for reason in report["groups"][0]["reasons"])
    output = _matrix([record], _storage_demand())
    role = output["profiles"][0]["roles"]["storage"]
    assert role["status"] == "UNQUALIFIED"
    assert "nodes_required" not in role
    assert output["qualified_evidence"] == []
    rejected = " ".join(reason for row in output["rejected_evidence"] for reason in row["reasons"])
    assert "thermal high/critical limit observed" in rejected


def test_nesting_deeper_than_32_names_the_path():
    record = _storage_record()
    node = {"leaf": 1}
    for _ in range(40):
        node = {"child": node}
    record["result"]["failure_rate"] = node
    parsed = common.parse_benchmark_record(record)
    assert isinstance(parsed, common.Rejection)
    assert "benchmark record nesting exceeds 32 at result.failure_rate" in parsed.reason
    report = _report([record])
    assert report["summary"]["passed"] == 0
    assert report["groups"][0]["repeat_count"] == 0
    assert "result.failure_rate" in " ".join(report["groups"][0]["reasons"])
    output = _matrix([record], _storage_demand())
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in json.dumps(output)


def test_shallow_nested_object_stays_a_missing_field():
    record = _storage_record()
    node = {"child": 1}
    for _ in range(6):
        node = {"child": node}
    record["result"]["failure_rate"] = node
    parsed = common.parse_benchmark_record(record)
    assert not isinstance(parsed, common.Rejection)
    assert parsed.failure_rate is None
    assert "MISSING_MEASURED_FIELD:result.failure_rate" in parsed.null_measured_reasons
    assert "nesting exceeds" not in " ".join(parsed.null_measured_reasons)


def test_complete_storage_record_still_qualifies():
    record = _storage_record()
    report = _report([record])
    assert report["summary"]["passed"] == 1
    output = _matrix([record], _storage_demand())
    role = output["profiles"][0]["roles"]["storage"]
    assert role["status"] == "QUALIFIED_FROM_MEASURED_EVIDENCE"
    assert role["observed_capacity_per_node_min"] == 20000.0


def test_markdown_required_paths_match_exported_set():
    text = (ROOT / "benchmarks" / "HARDWARE_QUALIFICATION.md").read_text(encoding="utf-8")
    assert "A complete reconnect repeat can pass reproducibility and still leave every capacity role `UNQUALIFIED`." in text
    marker = "### Exported required paths"
    start = text.find(marker)
    assert start != -1
    rest = text[start + len(marker):]
    end = rest.find("\n## ")
    body = rest if end < 0 else rest[:end]
    documented = set(re.findall(r"`((?:result|resources)\.[A-Za-z0-9_.]+)`", body))
    assert documented == _exported_code_paths()
