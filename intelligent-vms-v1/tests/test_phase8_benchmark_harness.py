import copy
import json
import os
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


def _sensor_hook_env(directory, *, max_mhz, temp_c, nic_speed, current_mhz=1234.0):
    """Return an environment that forces psutil sensor readings for writer CLIs.

    The child process imports this sitecustomize before the benchmark driver.
    max_mhz 0, temp_c None, and nic_speed 0 are the unavailable-sensor nulls.
    """
    hook = directory / "sensor-hook"
    hook.mkdir()
    temp_literal = "None" if temp_c is None else repr(float(temp_c))
    (hook / "sitecustomize.py").write_text(
        "\n".join(
            [
                "import psutil",
                "",
                "class _Freq:",
                f"    current = {float(current_mhz)!r}",
                "    min = 0.0",
                f"    max = {float(max_mhz)!r}",
                "",
                "def _cpu_freq(*_args, **_kwargs):",
                "    return _Freq()",
                "",
                "class _Reading:",
                "    label = ''",
                "    high = None",
                "    critical = None",
                f"    current = {temp_literal}",
                "",
                "def _temps(*_args, **_kwargs):",
                "    if _Reading.current is None:",
                "        return {}",
                "    return {'cpu-thermal': [_Reading()]}",
                "",
                "class _Stat:",
                "    def __init__(self, inner):",
                "        self._inner = inner",
                "    @property",
                "    def speed(self):",
                f"        return {int(nic_speed)!r}",
                "    @property",
                "    def mtu(self):",
                "        return self._inner.mtu",
                "    @property",
                "    def isup(self):",
                "        return self._inner.isup",
                "    @property",
                "    def duplex(self):",
                "        return self._inner.duplex",
                "",
                "_real_stats = psutil.net_if_stats",
                "",
                "def _stats(*_args, **_kwargs):",
                "    return {name: _Stat(stat) for name, stat in _real_stats().items()}",
                "",
                "psutil.cpu_freq = _cpu_freq",
                "psutil.sensors_temperatures = _temps",
                "psutil.net_if_stats = _stats",
                "",
            ]
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    previous = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(hook) if not previous else str(hook) + os.pathsep + previous
    return env


@pytest.mark.parametrize("writer", ("storage", "recording", "reconnect", "event"))
def test_phase8_driver_null_sensors_are_measurable(tmp_path, writer):
    output_json = tmp_path / f"{writer}.json"
    completed = subprocess.run(
        [sys.executable, str(TOOLS / f"phase8_{writer}_benchmark.py"), *_writer_command(writer, tmp_path, output_json)],
        cwd=TOOLS.parent,
        check=False,
        capture_output=True,
        text=True,
        env=_sensor_hook_env(tmp_path, max_mhz=0.0, temp_c=None, nic_speed=0),
    )
    assert completed.returncode == 0, completed.stderr
    assert output_json.is_file()
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    nulls = _null_paths(payload)
    assert any(marker in path for path in nulls for marker in _SENSOR_NULL_MARKERS)
    resources = payload["resources"]
    assert resources["cpu_freq_mhz"]["mean"] == 1234.0
    assert resources["cpu_freq_max_mhz"]["mean"] is None
    assert resources["max_temperature_c"]["mean"] is None
    assert resources["cpu_freq_ratio_min"] is None
    for nic in payload["environment"]["hardware"]["network_interfaces"]:
        if isinstance(nic, dict):
            assert nic["speed_mbps"] is None
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
    assert common.content_fingerprint(numbered) == common.content_fingerprint(copies[0])
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


def _storage_demand():
    return {
        "required_roles": {"storage": "storage_write_mbps"},
        "profiles": [
            {
                "name": "probe",
                "cameras": 1,
                "demands": {"storage_write_mbps": 1500},
            }
        ],
    }


def _qa601_measured_base():
    """Build one storage result whose unavailable sensors are null by construction.

    Samples set cpu_freq_max_mhz and max_temperature_c to null, so the ratio is
    null as well. NIC speed_mbps is then set null even when this host reports a
    speed. cpu_pct, ram_pct, failure_rate, bytes, and latency stay numbers.
    """
    result = common.build_result(
        workload_type="synthetic-storage-write",
        workload_config={
            "path": "/tmp/qa601",
            "streams": 1,
            "mib_per_stream": 1,
            "chunk_mib": 0.25,
            "fsync_each_chunk": False,
        },
        started_at="2026-10-08T00:00:00+00:00",
        duration_seconds=1.0,
        warmup_seconds=0.0,
        operations_ok=4,
        operations_failed=0,
        latencies_seconds=[0.001, 0.002, 0.003, 0.004],
        samples=[
            {
                "cpu_pct": 10.0,
                "cpu_freq_mhz": 2400.0,
                "cpu_freq_max_mhz": None,
                "max_temperature_c": None,
                "ram_used_bytes": 1024,
                "ram_pct": 40.0,
                "net_rx_mbps": 0.0,
                "net_tx_mbps": 0.0,
                "disk_read_mbps": 0.0,
                "disk_write_mbps": 0.0,
                "gpu": [],
            }
        ],
        extra_metrics={
            "bytes_written": 1048576,
            "aggregate_write_mbps": 2000.0,
            "aggregate_write_MBps": 250.0,
        },
    )
    hardware = result["environment"]["hardware"]
    interfaces = list(hardware.get("network_interfaces") or [])
    if not interfaces:
        interfaces = [{"name": "eth0", "is_up": True, "mtu": 1500}]
    for nic in interfaces:
        nic["speed_mbps"] = None
    hardware["network_interfaces"] = interfaces
    return result


def _install_allowlisted_nulls(payload):
    """Force the documented sensor nulls and numeric cpu/ram summaries.

    The base is real driver JSON. Overwriting these fields keeps the repro
    independent of whichever sensors this host can read.
    """
    resources = payload["resources"]
    for key in ("cpu_freq_max_mhz", "max_temperature_c"):
        resources[key] = {"mean": None, "p95": None, "max": None}
    resources["cpu_freq_ratio_min"] = None
    resources["cpu_pct"] = {"mean": 10.0, "p95": 20.0, "max": 30.0}
    resources["ram_pct"] = {"mean": 40.0, "p95": 50.0, "max": 60.0}
    hardware = payload["environment"]["hardware"]
    interfaces = list(hardware.get("network_interfaces") or [])
    if not interfaces:
        interfaces = [{"name": "eth0", "is_up": True, "mtu": 1500}]
    for nic in interfaces:
        if isinstance(nic, dict):
            nic["speed_mbps"] = None
    hardware["network_interfaces"] = interfaces
    return payload


def _null_swap_copies(base, edits):
    copies = []
    for index, edit in enumerate(edits):
        item = copy.deepcopy(base)
        item["benchmark_id"] = f"nullswap-{index}"
        if edit is not None:
            edit(item)
        copies.append(item)
    return copies


def _edit_cpu_mean(item):
    item["resources"]["cpu_pct"]["mean"] = None


def _edit_ram_mean(item):
    item["resources"]["ram_pct"]["mean"] = None


def _edit_bytes(item):
    item["result"]["bytes_written"] = None


def _edit_p95(item):
    item["result"]["latency"]["p95_ms"] = None


def _edit_failure_rate(item):
    item["result"]["failure_rate"] = None
    item["result"]["operations_ok"] = 0
    item["result"]["operations_failed"] = 4


_QA601_REPROS = (
    ("cpu_ram_mean", (None, _edit_cpu_mean, _edit_ram_mean)),
    ("bytes_and_p95", (None, _edit_bytes, _edit_p95)),
    ("failure_rate", (_edit_failure_rate, _edit_bytes, _edit_p95)),
)


def _assert_null_copies_do_not_qualify(tmp_path, copies):
    report = reproducibility.build_report(
        results=copies,
        min_repeats=3,
        min_duration_seconds=0,
        min_warmup_seconds=0,
    )
    assert report["summary"]["passed"] == 0
    assert all(group["status"] != "PASS" for group in report["groups"])
    assert all(group["repeat_count"] < 3 for group in report["groups"])
    output = matrix.build_matrix(
        results=copies,
        demand=_storage_demand(),
        min_repeats=3,
        min_duration_seconds=0,
        min_warmup_seconds=0,
    )
    rendered = json.dumps(output)
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in rendered
    result_dir = tmp_path / "results"
    result_dir.mkdir(parents=True)
    for index, item in enumerate(copies):
        (result_dir / f"{index}.json").write_text(json.dumps(item), encoding="utf-8")
    report_path = tmp_path / "reproducibility.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "phase8_reproducibility.py"),
            "--results",
            str(result_dir),
            "--output",
            str(report_path),
            "--fail-on-rejected-groups",
            "--min-repeats",
            "3",
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
    assert report_path.is_file(), completed.stderr
    assert "Traceback" not in completed.stderr
    assert completed.returncode != 0
    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert written["summary"]["passed"] == 0
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in rendered
    return report


def test_allowlisted_sensor_null_matches_missing_key_and_not_a_number():
    base = _qa601_measured_base()
    missing = copy.deepcopy(base)
    for key in ("cpu_freq_ratio_min", "cpu_freq_max_mhz", "max_temperature_c"):
        missing["resources"].pop(key, None)
    assert common.content_fingerprint(base) == common.content_fingerprint(missing)
    numbered = copy.deepcopy(base)
    numbered["resources"]["cpu_freq_ratio_min"] = 0.5
    assert common.content_fingerprint(numbered) == common.content_fingerprint(base)
    parsed = common.parse_benchmark_record(base)
    assert not isinstance(parsed, common.Rejection)
    report = reproducibility.build_report(
        results=[base],
        min_repeats=1,
        min_duration_seconds=0,
        min_warmup_seconds=0,
    )
    assert report["summary"]["passed"] == 1


@pytest.mark.parametrize("name,edits", _QA601_REPROS)
def test_qa_012_601_build_result_null_swap_is_not_a_repeat(tmp_path, name, edits):
    del name
    base = _qa601_measured_base()
    copies = _null_swap_copies(base, edits)
    report = _assert_null_copies_do_not_qualify(tmp_path, copies)
    reasons = _gate_reasons(report)
    assert "not a finite number" not in reasons
    if _edit_failure_rate in edits:
        nulled = next(item for item in copies if item["result"]["operations_failed"] == 4)
        parsed = common.parse_benchmark_record(nulled)
        assert not isinstance(parsed, common.Rejection)
        assert parsed.failure_rate is None
        assert "result.failure_rate is null" in reasons
        assert "no operations measured" not in reasons


def test_qa_012_601_storage_driver_null_swap_is_not_a_repeat(tmp_path):
    output_json = tmp_path / "storage.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "phase8_storage_benchmark.py"),
            *_writer_command("storage", tmp_path, output_json),
        ],
        cwd=TOOLS.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    payload = _install_allowlisted_nulls(json.loads(output_json.read_text(encoding="utf-8")))
    assert payload["result"]["bytes_written"]
    assert payload["result"]["latency"]["p95_ms"] is not None
    assert payload["result"]["failure_rate"] == 0.0
    solo = reproducibility.build_report(
        results=[payload],
        min_repeats=1,
        min_duration_seconds=0,
        min_warmup_seconds=0,
    )
    assert solo["summary"]["passed"] == 1
    for name, edits in _QA601_REPROS:
        copies = _null_swap_copies(payload, edits)
        report = _assert_null_copies_do_not_qualify(tmp_path / name, copies)
        reasons = _gate_reasons(report)
        assert "not a finite number" not in reasons
        if _edit_failure_rate in edits:
            nulled = next(item for item in copies if item["result"]["operations_failed"] == 4)
            parsed = common.parse_benchmark_record(nulled)
            assert parsed.failure_rate is None
            assert "result.failure_rate is null" in reasons


def test_recording_driver_null_metrics_do_not_qualify(tmp_path):
    output_json = tmp_path / "recording.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "phase8_recording_benchmark.py"),
            *_writer_command("recording", tmp_path, output_json),
        ],
        cwd=TOOLS.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    common.validate_result(payload)
    parsed = common.parse_benchmark_record(payload)
    assert not isinstance(parsed, common.Rejection)
    assert parsed.failure_rate is None
    report = reproducibility.build_report(
        results=[payload],
        min_repeats=1,
        min_duration_seconds=0,
        min_warmup_seconds=0,
    )
    reasons = _gate_reasons(report)
    assert "not a finite number" not in reasons
    assert report["summary"]["passed"] == 0
    assert report["groups"][0]["repeat_count"] == 0
    assert "no operations measured" in reasons
    assert "result.failure_rate is null" in reasons
    output = matrix.build_matrix(
        results=[payload],
        demand=_demand(),
        min_repeats=1,
        min_duration_seconds=0,
        min_warmup_seconds=0,
    )
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in json.dumps(output)


_ALLOWLISTED_SENSOR_FIELDS = (
    "cpu_freq_max_mhz.mean",
    "cpu_freq_max_mhz.p95",
    "cpu_freq_max_mhz.max",
    "cpu_freq_max_mhz",
    "max_temperature_c.mean",
    "max_temperature_c.p95",
    "max_temperature_c.max",
    "max_temperature_c",
    "cpu_freq_ratio_min",
    "speed_mbps",
)


def _numbered_sensor_base():
    """Storage-shaped result with every allowlisted sensor present as a number.

    Duration and warmup meet the default 300s and 30s floors. Capacity is the
    same on every copy so a sensor null is the only edit.
    """
    result = common.build_result(
        workload_type="synthetic-storage-write",
        workload_config={
            "path": "/tmp/qa701",
            "streams": 1,
            "mib_per_stream": 1,
            "chunk_mib": 0.25,
            "fsync_each_chunk": False,
        },
        started_at="2026-10-08T00:00:00+00:00",
        duration_seconds=600.0,
        warmup_seconds=60.0,
        operations_ok=4,
        operations_failed=0,
        latencies_seconds=[0.001, 0.002, 0.003, 0.004],
        samples=[
            {
                "cpu_pct": 10.0,
                "cpu_freq_mhz": 2400.0,
                "cpu_freq_max_mhz": 5000.0,
                "max_temperature_c": 55.0,
                "ram_used_bytes": 1024,
                "ram_pct": 40.0,
                "net_rx_mbps": 0.0,
                "net_tx_mbps": 0.0,
                "disk_read_mbps": 0.0,
                "disk_write_mbps": 0.0,
                "gpu": [],
            }
        ],
        extra_metrics={
            "bytes_written": 1048576,
            "aggregate_write_mbps": 20000.0,
            "aggregate_write_MBps": 2500.0,
        },
    )
    resources = result["resources"]
    resources["cpu_pct"] = {"mean": 10.0, "p95": 20.0, "max": 30.0}
    resources["ram_pct"] = {"mean": 40.0, "p95": 50.0, "max": 60.0}
    resources["cpu_freq_mhz"] = {"mean": 2400.0, "p95": 2400.0, "max": 2400.0}
    resources["cpu_freq_max_mhz"] = {"mean": 5000.0, "p95": 5000.0, "max": 5000.0}
    resources["max_temperature_c"] = {"mean": 55.0, "p95": 55.0, "max": 55.0}
    resources["cpu_freq_ratio_min"] = 0.48
    hardware = result["environment"]["hardware"]
    interfaces = list(hardware.get("network_interfaces") or [])
    if not interfaces:
        interfaces = [{"name": "eth0", "is_up": True, "mtu": 1500}]
    for nic in interfaces:
        nic["speed_mbps"] = 1000
    hardware["network_interfaces"] = interfaces
    return result


def _null_allowlisted_sensor(item, field):
    if field == "speed_mbps":
        for nic in item["environment"]["hardware"]["network_interfaces"]:
            nic["speed_mbps"] = None
        return
    if field == "cpu_freq_ratio_min":
        item["resources"]["cpu_freq_ratio_min"] = None
        return
    if "." not in field:
        item["resources"][field] = {"mean": None, "p95": None, "max": None}
        return
    summary, component = field.split(".", 1)
    item["resources"][summary][component] = None


def _assert_sensor_copies_do_not_qualify(tmp_path, copies, *, min_repeats, min_duration, min_warmup):
    report = reproducibility.build_report(
        results=copies,
        min_repeats=min_repeats,
        min_duration_seconds=min_duration,
        min_warmup_seconds=min_warmup,
    )
    assert report["summary"]["passed"] == 0
    assert all(group["repeat_count"] < min_repeats for group in report["groups"])
    output = matrix.build_matrix(
        results=copies,
        demand=_storage_demand(),
        min_repeats=min_repeats,
        min_duration_seconds=min_duration,
        min_warmup_seconds=min_warmup,
    )
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in json.dumps(output)
    result_dir = tmp_path / "results"
    result_dir.mkdir(parents=True)
    paths = []
    for index, item in enumerate(copies):
        path = result_dir / f"{index}.json"
        path.write_text(json.dumps(item), encoding="utf-8")
        paths.append(str(path))
    report_path = tmp_path / "reproducibility.json"
    matrix_path = tmp_path / "hardware-matrix.json"
    demand_path = tmp_path / "demand.json"
    demand_path.write_text(json.dumps(_storage_demand()), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "phase8_reproducibility.py"),
            "--results",
            *paths,
            "--output",
            str(report_path),
            "--fail-on-rejected-groups",
            "--min-repeats",
            str(min_repeats),
            "--min-duration-seconds",
            str(min_duration),
            "--min-warmup-seconds",
            str(min_warmup),
        ],
        cwd=TOOLS.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    assert report_path.is_file(), completed.stderr
    assert "Traceback" not in completed.stderr
    assert completed.returncode != 0
    matrix_cli = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "phase8_hardware_matrix.py"),
            "--results",
            *paths,
            "--demand",
            str(demand_path),
            "--reproducibility-report",
            str(report_path),
            "--output",
            str(matrix_path),
            "--min-repeats",
            str(min_repeats),
            "--min-duration-seconds",
            str(min_duration),
            "--min-warmup-seconds",
            str(min_warmup),
        ],
        cwd=TOOLS.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    assert matrix_path.is_file(), matrix_cli.stderr
    assert "Traceback" not in matrix_cli.stderr
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in matrix_path.read_text(encoding="utf-8")
    return report


def test_qa_012_701_storage_driver_sensor_nulls_are_not_repeats(tmp_path):
    output_json = tmp_path / "storage.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "phase8_storage_benchmark.py"),
            *_writer_command("storage", tmp_path, output_json),
        ],
        cwd=TOOLS.parent,
        check=False,
        capture_output=True,
        text=True,
        env=_sensor_hook_env(tmp_path, max_mhz=5000.0, temp_c=55.0, nic_speed=1000, current_mhz=2400.0),
    )
    assert completed.returncode == 0, completed.stderr
    base = json.loads(output_json.read_text(encoding="utf-8"))
    assert base["resources"]["cpu_freq_max_mhz"]["mean"] == 5000.0
    assert isinstance(base["resources"]["cpu_freq_ratio_min"], float)
    copies = []
    for name, field in (
        ("a", None),
        ("b", "cpu_freq_max_mhz.mean"),
        ("c", "cpu_freq_ratio_min"),
    ):
        item = copy.deepcopy(base)
        item["benchmark_id"] = f"sensor-null-{name}"
        if field is not None:
            _null_allowlisted_sensor(item, field)
        copies.append(item)
    assert len({common.content_fingerprint(item) for item in copies}) == 1
    _assert_sensor_copies_do_not_qualify(
        tmp_path / "gate",
        copies,
        min_repeats=3,
        min_duration=0,
        min_warmup=0,
    )


def test_qa_012_701_default_floors_reject_nulled_sensor_numbers(tmp_path):
    base = _numbered_sensor_base()
    copies = []
    for name, field in (
        ("orig", None),
        ("freq-mean-null", "cpu_freq_max_mhz.mean"),
        ("freq-p95-null", "cpu_freq_max_mhz.p95"),
    ):
        item = copy.deepcopy(base)
        item["benchmark_id"] = name
        if field is not None:
            _null_allowlisted_sensor(item, field)
        copies.append(item)
    assert len({common.content_fingerprint(item) for item in copies}) == 1
    _assert_sensor_copies_do_not_qualify(
        tmp_path,
        copies,
        min_repeats=3,
        min_duration=300,
        min_warmup=30,
    )


@pytest.mark.parametrize("field", _ALLOWLISTED_SENSOR_FIELDS)
def test_qa_012_701_each_allowlisted_sensor_null_is_not_distinct(tmp_path, field):
    base = _numbered_sensor_base()
    nulled = copy.deepcopy(base)
    nulled["benchmark_id"] = f"null-{field}"
    _null_allowlisted_sensor(nulled, field)
    base["benchmark_id"] = f"number-{field}"
    assert common.content_fingerprint(base) == common.content_fingerprint(nulled)
    _assert_sensor_copies_do_not_qualify(
        tmp_path,
        [base, nulled],
        min_repeats=2,
        min_duration=300,
        min_warmup=30,
    )
