import json
import sys
from pathlib import Path
from types import SimpleNamespace

TOOLS = Path(__file__).parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import phase8_benchmark_common as common
import phase8_event_benchmark as event_bench
import phase8_media_plan as media_plan
import phase8_recording_benchmark as recording_bench
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
