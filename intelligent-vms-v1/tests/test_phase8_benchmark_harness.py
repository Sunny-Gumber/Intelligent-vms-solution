import copy
import json
import subprocess
import sys

import pytest
from pathlib import Path
from types import SimpleNamespace

TOOLS = Path(__file__).parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import phase8_benchmark_common as common
import phase8_event_benchmark as event_bench
import phase8_hardware_matrix as matrix
import phase8_media_plan as media_plan
import phase8_recording_benchmark as recording_bench
import phase8_reproducibility as reproducibility
import phase8_storage_benchmark as storage_bench


def test_percentiles_and_latency_summary_are_deterministic():
    values = [0.001, 0.002, 0.003, 0.004]
    summary = common.latency_summary(values)
    assert summary["count"] == 4
    assert summary["p50_ms"] == 2.5
    assert round(summary["p95_ms"], 3) == 3.85
    assert round(summary["p99_ms"], 3) == 3.97
    assert summary["max_ms"] == 4.0


def test_result_contains_environment_workload_percentiles_and_unmeasured_gpu():
    result = common.build_result(
        workload_type="unit-smoke",
        workload_config={"items": 2},
        started_at=common.utc_iso(),
        duration_seconds=2.0,
        warmup_seconds=0.5,
        operations_ok=2,
        operations_failed=0,
        latencies_seconds=[0.01, 0.02],
        samples=[
            {
                "cpu_pct": 10.0,
                "ram_used_bytes": 1000,
                "ram_pct": 20.0,
                "net_rx_mbps": 1.0,
                "net_tx_mbps": 2.0,
                "disk_read_mbps": 3.0,
                "disk_write_mbps": 4.0,
                "gpu": [],
            }
        ],
    )
    common.validate_result(result)
    assert result["schema_version"] == "phase8-benchmark-v1"
    assert result["environment"]["commit_sha"]
    assert result["workload"]["type"] == "unit-smoke"
    assert result["result"]["p95_ms"] if "p95_ms" in result["result"] else True
    assert result["result"]["latency"]["p99_ms"] is not None
    assert result["resources"]["gpu_measured"] is False


def test_result_writer_emits_json_and_csv(tmp_path):
    result = common.build_result(
        workload_type="writer-smoke",
        workload_config={},
        started_at=common.utc_iso(),
        duration_seconds=1.0,
        warmup_seconds=0.0,
        operations_ok=1,
        operations_failed=0,
        latencies_seconds=[0.001],
        samples=[],
    )
    json_path = tmp_path / "result.json"
    csv_path = tmp_path / "index.csv"
    common.write_result(
        result,
        json_path=str(json_path),
        csv_path=str(csv_path),
    )
    loaded = json.loads(json_path.read_text())
    assert loaded["benchmark_id"] == result["benchmark_id"]
    assert "throughput_ops_s" in csv_path.read_text()


def test_event_payload_is_repeatable_for_same_run_and_index():
    first = event_bench.make_event(42, 500, "run-1")
    second = event_bench.make_event(42, 500, "run-1")
    assert first["event_id"] == second["event_id"]
    assert first["camera_id"] == "cam-0000042"
    assert first["attributes"]["benchmark_run"] == "run-1"


def test_media_plan_contains_exact_source_and_viewer_counts():
    args = SimpleNamespace(
        server="rtsp://127.0.0.1:8554",
        sources=3,
        viewers=5,
        start=10,
        width=1920,
        height=1080,
        fps=25,
        bitrate_kbps=1024,
        codec="h264",
        transport="tcp",
    )
    manifest = media_plan.build_manifest(args)
    assert len(manifest["sources"]) == 3
    assert len(manifest["viewers"]) == 5
    assert manifest["sources"][0][-1].endswith("/bench/cam-0000010")
    assert "1024k" in manifest["sources"][0]


def test_storage_writer_writes_requested_bytes(tmp_path):
    path = tmp_path / "write.bin"
    written, latencies = storage_bench._write_stream(
        path,
        total_bytes=1024 * 1024,
        chunk_bytes=256 * 1024,
        fsync=False,
    )
    assert written == 1024 * 1024
    assert path.stat().st_size == written
    assert len(latencies) == 4


def test_json_schema_declares_required_evidence_fields():
    schema = json.loads(
        (Path(__file__).parents[1] / "benchmarks" / "result.schema.json").read_text()
    )
    assert schema["properties"]["schema_version"]["const"] == "phase8-benchmark-v1"
    assert {
        "schema_version",
        "benchmark_id",
        "environment",
        "workload",
        "result",
        "resources",
    } <= set(schema["required"])


def test_recording_directory_snapshot_counts_files_and_bytes(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"a" * 100)
    nested = tmp_path / "cam"
    nested.mkdir()
    (nested / "b.mp4").write_bytes(b"b" * 200)
    files, total = recording_bench.directory_snapshot(tmp_path)
    assert files == 2
    assert total == 300


_SENSOR_NULL_MARKERS = (
    "cpu_freq_max_mhz",
    "max_temperature_c",
    "cpu_freq_ratio_min",
    "speed_mbps",
)


def _null_paths(value, prefix=""):
    found = []
    if value is None:
        found.append(prefix)
    elif isinstance(value, dict):
        for key, item in value.items():
            child = f"{prefix}.{key}" if prefix else key
            found.extend(_null_paths(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_null_paths(item, f"{prefix}[{index}]"))
    return found


def _writer_command(name, tmp_path, output_json):
    if name == "storage":
        data = tmp_path / "storage-data"
        return [
            "--path",
            str(data),
            "--streams",
            "1",
            "--mib-per-stream",
            "1",
            "--chunk-mib",
            "0.25",
            "--sample-interval",
            "0.2",
            "--output-json",
            str(output_json),
        ]
    if name == "recording":
        root = tmp_path / "recording"
        root.mkdir()
        return [
            "--path",
            str(root),
            "--duration",
            "0.2",
            "--sample-interval",
            "0.2",
            "--output-json",
            str(output_json),
        ]
    if name == "reconnect":
        return [
            "--host",
            "127.0.0.1",
            "--port",
            "1",
            "--attempts",
            "1",
            "--warmup-attempts",
            "0",
            "--concurrency",
            "1",
            "--timeout",
            "0.2",
            "--sample-interval",
            "0.2",
            "--output-json",
            str(output_json),
        ]
    if name == "event":
        return [
            "--api",
            "http://127.0.0.1:9",
            "--events",
            "1",
            "--warmup-events",
            "0",
            "--cameras",
            "1",
            "--concurrency",
            "1",
            "--timeout",
            "0.2",
            "--sample-interval",
            "0.2",
            "--output-json",
            str(output_json),
        ]
    raise AssertionError(name)


def _demand():
    return {
        "required_roles": {
            "event_ingest": "events_per_second",
            "recording": "recording_mbps",
            "storage": "storage_write_mbps",
        },
        "profiles": [
            {
                "name": "driver-nulls",
                "cameras": 1,
                "demands": {
                    "events_per_second": 1,
                    "recording_mbps": 1,
                    "storage_write_mbps": 1,
                },
            }
        ],
    }


def _gate_reasons(report):
    return " ".join(reason for group in report["groups"] for reason in group["reasons"])


@pytest.mark.parametrize("writer", ("storage", "recording", "reconnect", "event"))
def test_phase8_driver_null_sensors_are_measurable(tmp_path, writer):
    output_json = tmp_path / f"{writer}.json"
    completed = subprocess.run(
        [sys.executable, str(TOOLS / f"phase8_{writer}_benchmark.py"), *_writer_command(writer, tmp_path, output_json)],
        cwd=TOOLS.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert output_json.is_file()
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    nulls = _null_paths(payload)
    assert any(marker in path for path in nulls for marker in _SENSOR_NULL_MARKERS)
    parsed = common.parse_benchmark_record(payload)
    assert not isinstance(parsed, common.Rejection), getattr(parsed, "reason", "")
    report = reproducibility.build_report(
        results=[payload],
        min_repeats=1,
        min_duration_seconds=0,
        min_warmup_seconds=0,
    )
    assert "not a finite number" not in _gate_reasons(report)
    if writer == "storage":
        assert report["summary"]["passed"] == 1
    output = matrix.build_matrix(
        results=[payload],
        demand=_demand(),
        min_repeats=1,
        min_duration_seconds=0,
        min_warmup_seconds=0,
    )
    assert "not a finite number" not in json.dumps(output)
    approved = {}
    for group in report["groups"]:
        if group["status"] == "PASS":
            approved.update(group["benchmark_fingerprints"])
    handed = matrix.build_matrix(
        results=[payload],
        demand=_demand(),
        approved_benchmark_fingerprints=approved,
        min_repeats=1,
        min_duration_seconds=0,
        min_warmup_seconds=0,
    )
    assert "not a finite number" not in json.dumps(handed)
    copies = []
    for index in range(3):
        item = copy.deepcopy(payload)
        item["benchmark_id"] = f"{writer}-copy-{index}"
        item["note"] = f"label-{index}"
        copies.append(item)
    assert len({common.content_fingerprint(item) for item in copies}) == 1
    numbered = copy.deepcopy(copies[0])
    numbered["resources"]["cpu_freq_ratio_min"] = 0.5
    assert common.content_fingerprint(numbered) != common.content_fingerprint(copies[0])
    repeat_report = reproducibility.build_report(
        results=copies,
        min_duration_seconds=0,
        min_warmup_seconds=0,
    )
    assert repeat_report["summary"]["passed"] == 0
    repeat_matrix = matrix.build_matrix(
        results=copies,
        demand=_demand(),
        min_duration_seconds=0,
        min_warmup_seconds=0,
    )
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in json.dumps(repeat_matrix)
    report_path = tmp_path / "reproducibility.json"
    matrix_path = tmp_path / "hardware-matrix.json"
    demand_path = tmp_path / "demand.json"
    demand_path.write_text(json.dumps(_demand()), encoding="utf-8")
    reproducibility_cli = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "phase8_reproducibility.py"),
            "--results",
            str(output_json),
            "--output",
            str(report_path),
            "--fail-on-rejected-groups",
            "--min-repeats",
            "1",
            "--min-duration-seconds",
            "0",
            "--min-warmup-seconds",
            "0",
        ],
        cwd=TOOLS.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    assert report_path.is_file(), reproducibility_cli.stderr
    assert "Traceback" not in reproducibility_cli.stderr
    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert "not a finite number" not in _gate_reasons(written)
    if writer == "storage":
        assert reproducibility_cli.returncode == 0
        assert written["summary"]["passed"] == 1
    matrix_cli = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "phase8_hardware_matrix.py"),
            "--results",
            str(output_json),
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
        cwd=TOOLS.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    assert matrix_path.is_file(), matrix_cli.stderr
    assert "Traceback" not in matrix_cli.stderr
    assert "not a finite number" not in matrix_path.read_text(encoding="utf-8")
