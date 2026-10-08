"""Required measured fields must be present before Phase 8 qualification.

The path tuples below are the schema under test. They are compared with
required_measured_fields so a new driver field cannot qualify while this list
stays stale.
"""

import copy
import json
import math
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

TOOLS = Path(__file__).parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import phase8_benchmark_common as common
import phase8_hardware_matrix as matrix
import phase8_reproducibility as reproducibility


# Shared core written by build_result and resource_summary, excluding optional
# sensor nulls. hardware-matrix, reproducibility, and reconnect use this list.
_CORE_PATHS = (
    "result.operations_ok",
    "result.operations_failed",
    "result.failure_rate",
    "result.throughput_ops_s",
    "result.latency.count",
    "result.latency.p50_ms",
    "result.latency.p95_ms",
    "result.latency.p99_ms",
    "result.latency.max_ms",
    "result.latency.mean_ms",
    "resources.samples",
    "resources.cpu_pct.mean",
    "resources.cpu_pct.p95",
    "resources.cpu_pct.max",
    "resources.cpu_freq_mhz.mean",
    "resources.cpu_freq_mhz.p95",
    "resources.cpu_freq_mhz.max",
    "resources.ram_used_bytes.mean",
    "resources.ram_used_bytes.p95",
    "resources.ram_used_bytes.max",
    "resources.ram_pct.mean",
    "resources.ram_pct.p95",
    "resources.ram_pct.max",
    "resources.net_rx_mbps.mean",
    "resources.net_rx_mbps.p95",
    "resources.net_rx_mbps.max",
    "resources.net_tx_mbps.mean",
    "resources.net_tx_mbps.p95",
    "resources.net_tx_mbps.max",
    "resources.disk_read_mbps.mean",
    "resources.disk_read_mbps.p95",
    "resources.disk_read_mbps.max",
    "resources.disk_write_mbps.mean",
    "resources.disk_write_mbps.p95",
    "resources.disk_write_mbps.max",
)
_STORAGE_EXTRA_PATHS = (
    "result.bytes_written",
    "result.aggregate_write_mbps",
    "result.aggregate_write_MBps",
)
STORAGE_PATHS = _CORE_PATHS + _STORAGE_EXTRA_PATHS
RECONNECT_PATHS = _CORE_PATHS
HARDWARE_MATRIX_PATHS = _CORE_PATHS
REPRODUCIBILITY_PATHS = _CORE_PATHS

_DELETION_CASES = (
    [("storage", path) for path in STORAGE_PATHS]
    + [("reconnect", path) for path in RECONNECT_PATHS]
    + [("hardware-matrix", path) for path in HARDWARE_MATRIX_PATHS]
    + [("reproducibility", path) for path in REPRODUCIBILITY_PATHS]
)


def _storage_demand(mbps=1500.0):
    return {
        "required_roles": {"storage": "storage_write_mbps"},
        "profiles": [
            {"name": "probe", "cameras": 1, "demands": {"storage_write_mbps": mbps}}
        ],
    }


def _event_demand():
    return {
        "required_roles": {"event_ingest": "events_per_second"},
        "profiles": [
            {"name": "probe", "cameras": 1, "demands": {"events_per_second": 1.0}}
        ],
    }


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


def _pin_optional_sensor_nulls(result):
    """Keep the documented sensor nulls and finite cpu/ram summaries.

    build_result already writes these keys. Pinning them makes the fixture
    independent of whichever sensors this host can read.
    """
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
    interfaces = list(hardware.get("network_interfaces") or [])
    if not interfaces:
        interfaces = [{"name": "eth0", "is_up": True, "mtu": 1500}]
    for nic in interfaces:
        if isinstance(nic, dict):
            nic["speed_mbps"] = None
    hardware["network_interfaces"] = interfaces
    return result


def _record(workload_type, extra_metrics=None):
    result = common.build_result(
        workload_type=workload_type,
        workload_config={"path": "/tmp/vms-fix-037", "streams": 1},
        started_at="2026-10-08T00:00:00+00:00",
        duration_seconds=600.0,
        warmup_seconds=60.0,
        operations_ok=4,
        operations_failed=0,
        latencies_seconds=[0.001, 0.002, 0.003, 0.004],
        samples=[_sample()],
        extra_metrics=extra_metrics,
    )
    return _pin_optional_sensor_nulls(result)


def _storage_record():
    return _record(
        "synthetic-storage-write",
        {
            "bytes_written": 1048576,
            "aggregate_write_mbps": 20000.0,
            "aggregate_write_MBps": 2500.0,
        },
    )


def _event_record():
    return _record("event-ingest-http")


def _reconnect_record():
    return _record("tcp-reconnect-storm")


@pytest.fixture(scope="module")
def bases():
    """One complete record per evidence kind, built by the current writer."""
    event = _event_record()
    return {
        "storage": _storage_record(),
        "reconnect": _reconnect_record(),
        "hardware-matrix": event,
        "reproducibility": copy.deepcopy(event),
    }


def _delete(record, path):
    cursor = record
    parts = path.split(".")
    for part in parts[:-1]:
        cursor = cursor[part]
    del cursor[parts[-1]]


def _gate_text(report):
    return " ".join(reason for group in report["groups"] for reason in group["reasons"])


def _matrix(results, demand):
    return matrix.build_matrix(
        results=results,
        demand=demand,
        min_repeats=1,
        min_duration_seconds=0,
        min_warmup_seconds=0,
    )


def _report(results, min_repeats=1):
    return reproducibility.build_report(
        results=results,
        min_repeats=min_repeats,
        min_duration_seconds=0,
        min_warmup_seconds=0,
    )


def _demand_for(kind):
    if kind == "storage":
        return _storage_demand()
    return _event_demand()


def test_exported_schema_matches_enumerated_paths():
    exporter = getattr(common, "required_measured_fields", None)
    workload = getattr(common, "required_measured_fields_for_workload", None)
    assert exporter is not None
    assert workload is not None
    assert exporter("storage") == STORAGE_PATHS
    assert exporter("reconnect") == RECONNECT_PATHS
    assert exporter("hardware-matrix") == HARDWARE_MATRIX_PATHS
    assert exporter("reproducibility") == REPRODUCIBILITY_PATHS
    assert workload("synthetic-storage-write") == STORAGE_PATHS
    assert workload("tcp-reconnect-storm") == RECONNECT_PATHS
    assert workload("event-ingest-http") == _CORE_PATHS
    recording = _CORE_PATHS + ("result.observed_recording_mbps",)
    assert workload("recording-directory-growth") == recording
    with pytest.raises(ValueError):
        exporter("not-a-kind")


def test_complete_driver_shaped_records_qualify(bases):
    storage = _matrix([bases["storage"]], _storage_demand())
    assert storage["profiles"][0]["roles"]["storage"]["status"] == (
        "QUALIFIED_FROM_MEASURED_EVIDENCE"
    )
    storage_report = _report([bases["storage"]])
    assert storage_report["summary"]["passed"] == 1
    event = _matrix([bases["hardware-matrix"]], _event_demand())
    assert event["profiles"][0]["roles"]["event_ingest"]["status"] == (
        "QUALIFIED_FROM_MEASURED_EVIDENCE"
    )
    event_report = _report([bases["reproducibility"]])
    assert event_report["summary"]["passed"] == 1
    reconnect_report = _report([bases["reconnect"]])
    assert reconnect_report["summary"]["passed"] == 1
    reconnect_matrix = _matrix([bases["reconnect"]], _storage_demand())
    rendered = json.dumps(reconnect_matrix)
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in rendered


@pytest.mark.parametrize("kind,path", _DELETION_CASES)
def test_deleting_each_required_field_is_unqualified(bases, kind, path):
    record = copy.deepcopy(bases[kind])
    _delete(record, path)
    report = _report([record])
    assert report["summary"]["passed"] == 0
    assert f"MISSING_MEASURED_FIELD:{path}" in _gate_text(report)
    if kind == "reconnect":
        output = _matrix([record], _storage_demand())
        assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in json.dumps(output)
        return
    output = _matrix([record], _demand_for(kind))
    rendered = json.dumps(output)
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in rendered
    rejected = " ".join(
        reason for row in output["rejected_evidence"] for reason in row["reasons"]
    )
    assert f"MISSING_MEASURED_FIELD:{path}" in rejected


def test_empty_resource_summary_is_unqualified(bases):
    record = copy.deepcopy(bases["storage"])
    record["resources"]["disk_read_mbps"] = {}
    report = _report([record])
    text = _gate_text(report)
    assert report["summary"]["passed"] == 0
    for stat in ("mean", "p95", "max"):
        assert f"MISSING_MEASURED_FIELD:resources.disk_read_mbps.{stat}" in text
    output = _matrix([record], _storage_demand())
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in json.dumps(output)


def test_three_deleted_leaves_do_not_qualify(bases):
    edits = (
        ("disk_read_mbps", "mean"),
        ("disk_write_mbps", "max"),
        ("net_rx_mbps", "p95"),
    )
    copies = []
    for index, (summary, stat) in enumerate(edits):
        item = copy.deepcopy(bases["storage"])
        item["benchmark_id"] = f"deleted-{index}"
        del item["resources"][summary][stat]
        copies.append(item)
    report = _report(copies, min_repeats=3)
    assert report["summary"]["passed"] == 0
    output = matrix.build_matrix(
        results=copies,
        demand=_storage_demand(),
        min_repeats=3,
        min_duration_seconds=0,
        min_warmup_seconds=0,
    )
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in json.dumps(output)


def test_three_empty_summaries_do_not_qualify(bases):
    summaries = ("disk_read_mbps", "disk_write_mbps", "net_rx_mbps")
    copies = []
    for index, summary in enumerate(summaries):
        item = copy.deepcopy(bases["storage"])
        item["benchmark_id"] = f"empty-{index}"
        item["resources"][summary] = {}
        copies.append(item)
    report = _report(copies, min_repeats=3)
    assert report["summary"]["passed"] == 0
    output = matrix.build_matrix(
        results=copies,
        demand=_storage_demand(),
        min_repeats=3,
        min_duration_seconds=0,
        min_warmup_seconds=0,
    )
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in json.dumps(output)


def test_null_required_field_is_unqualified(bases):
    record = copy.deepcopy(bases["storage"])
    record["resources"]["disk_read_mbps"]["mean"] = None
    parsed = common.parse_benchmark_record(record)
    assert not isinstance(parsed, common.Rejection)
    assert "resources.disk_read_mbps.mean is null" in parsed.null_measured_reasons
    report = _report([record])
    assert report["summary"]["passed"] == 0
    output = _matrix([record], _storage_demand())
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in json.dumps(output)


@pytest.mark.parametrize("bad", (float("nan"), float("inf"), float("-inf")))
def test_non_finite_required_field_is_unqualified(bases, bad):
    record = copy.deepcopy(bases["storage"])
    record["resources"]["cpu_pct"]["p95"] = bad
    report = _report([record])
    assert report["summary"]["passed"] == 0
    assert "is not a finite number" in _gate_text(report)
    output = _matrix([record], _storage_demand())
    rendered = json.dumps(output)
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in rendered
    rejected = " ".join(
        reason for row in output["rejected_evidence"] for reason in row["reasons"]
    )
    assert "is not a finite number" in rejected


def test_missing_failure_rate_is_not_zero(bases):
    record = copy.deepcopy(bases["storage"])
    record["result"]["operations_failed"] = 4
    del record["result"]["failure_rate"]
    parsed = common.parse_benchmark_record(record)
    assert not isinstance(parsed, common.Rejection)
    assert parsed.failure_rate is None
    assert parsed.failure_rate != 0.0
    assert "MISSING_MEASURED_FIELD:result.failure_rate" in parsed.null_measured_reasons
    report = _report([record])
    assert report["summary"]["passed"] == 0
    output = _matrix([record], _storage_demand())
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in json.dumps(output)
    rejected = " ".join(
        reason for row in output["rejected_evidence"] for reason in row["reasons"]
    )
    assert "MISSING_MEASURED_FIELD:result.failure_rate" in rejected


def test_optional_sensor_nulls_missing_keys_and_empty_objects_still_qualify(bases):
    variants = []
    nulled = copy.deepcopy(bases["storage"])
    variants.append(nulled)
    missing = copy.deepcopy(bases["storage"])
    for key in ("cpu_freq_max_mhz", "max_temperature_c", "cpu_freq_ratio_min"):
        missing["resources"].pop(key, None)
    for nic in missing["environment"]["hardware"]["network_interfaces"]:
        if isinstance(nic, dict):
            nic.pop("speed_mbps", None)
    variants.append(missing)
    emptied = copy.deepcopy(bases["storage"])
    emptied["resources"]["cpu_freq_max_mhz"] = {}
    emptied["resources"]["max_temperature_c"] = {}
    variants.append(emptied)
    for item in variants:
        parsed = common.parse_benchmark_record(item)
        assert not isinstance(parsed, common.Rejection)
        assert parsed.null_measured_reasons == ()
        report = _report([item])
        assert report["summary"]["passed"] == 1
        output = _matrix([item], _storage_demand())
        status = output["profiles"][0]["roles"]["storage"]["status"]
        assert status == "QUALIFIED_FROM_MEASURED_EVIDENCE"


def _run_writer(command):
    completed = subprocess.run(
        [sys.executable, *command],
        cwd=TOOLS.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return completed


def _accept_loop(sock, stop):
    while not stop.is_set():
        try:
            conn, _addr = sock.accept()
        except socket.timeout:
            continue
        try:
            conn.settimeout(0.2)
            try:
                conn.recv(16)
            except OSError:
                pass
            conn.close()
        except OSError:
            pass


def test_real_storage_driver_repeats_still_qualify(tmp_path):
    """Three disposable storage runs must still qualify from their own numbers.

    Duration and resource floors are relaxed because these are short local
    writes, not a production capacity run. The qualified capacity is the
    minimum aggregate_write_mbps the driver wrote.
    """
    runs = []
    data = tmp_path / "storage-data"
    for index in range(3):
        output_json = tmp_path / f"storage-{index}.json"
        _run_writer(
            [
                str(TOOLS / "phase8_storage_benchmark.py"),
                "--path",
                str(data),
                "--streams",
                "1",
                "--mib-per-stream",
                "2",
                "--chunk-mib",
                "0.5",
                "--sample-interval",
                "0.2",
                "--output-json",
                str(output_json),
            ]
        )
        runs.append(json.loads(output_json.read_text(encoding="utf-8")))
    fingerprints = set()
    capacities = []
    for item in runs:
        parsed = common.parse_benchmark_record(item)
        assert not isinstance(parsed, common.Rejection)
        assert parsed.null_measured_reasons == ()
        assert parsed.failure_rate == 0.0
        fingerprints.add(parsed.content_fingerprint)
        capacities.append(item["result"]["aggregate_write_mbps"])
    assert len(fingerprints) == 3
    report = reproducibility.build_report(
        results=runs,
        min_repeats=3,
        min_duration_seconds=0,
        min_warmup_seconds=0,
        max_capacity_cv=1.0,
        max_p95_latency_cv=1.0,
        max_capacity_relative_range=1.0,
    )
    assert report["summary"]["passed"] == 1
    assert report["groups"][0]["repeat_count"] == 3
    output = matrix.build_matrix(
        results=runs,
        demand=_storage_demand(1.0),
        min_repeats=3,
        min_duration_seconds=0,
        min_warmup_seconds=0,
        max_cpu_p95_pct=100.0,
        max_ram_p95_pct=100.0,
    )
    role = output["profiles"][0]["roles"]["storage"]
    observed = min(capacities)
    assert role["status"] == "QUALIFIED_FROM_MEASURED_EVIDENCE"
    assert role["observed_capacity_per_node_min"] == observed
    safe = observed * 0.80
    assert role["safe_capacity_per_node"] == safe
    assert role["base_nodes"] == math.ceil(1.0 / safe)
    assert role["nodes_required"] == role["base_nodes"] + 1


def test_real_reconnect_driver_repeats_still_qualify(tmp_path):
    """Three local reconnect runs stay complete and pass reproducibility.

    The matrix has no reconnect capacity role, so it must not report
    QUALIFIED_FROM_MEASURED_EVIDENCE for these files.
    """
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(256)
    sock.settimeout(0.2)
    port = sock.getsockname()[1]
    stop = threading.Event()
    thread = threading.Thread(target=_accept_loop, args=(sock, stop), daemon=True)
    thread.start()
    try:
        runs = []
        for index in range(3):
            output_json = tmp_path / f"reconnect-{index}.json"
            _run_writer(
                [
                    str(TOOLS / "phase8_reconnect_benchmark.py"),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--attempts",
                    "40",
                    "--warmup-attempts",
                    "0",
                    "--concurrency",
                    "8",
                    "--timeout",
                    "1.0",
                    "--sample-interval",
                    "0.2",
                    "--output-json",
                    str(output_json),
                ]
            )
            runs.append(json.loads(output_json.read_text(encoding="utf-8")))
    finally:
        stop.set()
        thread.join(timeout=2)
        sock.close()
    fingerprints = set()
    for item in runs:
        parsed = common.parse_benchmark_record(item)
        assert not isinstance(parsed, common.Rejection), getattr(parsed, "reason", "")
        assert parsed.null_measured_reasons == (), parsed.null_measured_reasons
        assert parsed.failure_rate == 0.0
        fingerprints.add(parsed.content_fingerprint)
    assert len(fingerprints) == 3
    report = reproducibility.build_report(
        results=runs,
        min_repeats=3,
        min_duration_seconds=0,
        min_warmup_seconds=0,
        max_capacity_cv=1.0,
        max_p95_latency_cv=1.0,
        max_capacity_relative_range=1.0,
    )
    assert report["summary"]["passed"] == 1, report["groups"][0]["reasons"]
    assert report["groups"][0]["repeat_count"] == 3
    output = matrix.build_matrix(
        results=runs,
        demand=_storage_demand(1.0),
        min_repeats=3,
        min_duration_seconds=0,
        min_warmup_seconds=0,
        max_cpu_p95_pct=100.0,
        max_ram_p95_pct=100.0,
    )
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in json.dumps(output)
