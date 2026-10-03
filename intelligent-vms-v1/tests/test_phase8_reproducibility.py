import sys
from pathlib import Path

TOOLS = Path(__file__).parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import phase8_reproducibility as reproducibility


def result(
    *,
    benchmark_id,
    throughput=1000.0,
    p95_ms=10.0,
    p50_ms=5.0,
    p99_ms=15.0,
    max_ms=20.0,
    config=None,
    commit="commit-a",
    duration=600.0,
    warmup=60.0,
    failure_rate=0.0,
    thermal_measured=True,
    thermal_limit_exceeded=False,
    cpu_freq_ratio_min=0.90,
):
    return {
        "schema_version": "phase8-benchmark-v1",
        "benchmark_id": benchmark_id,
        "environment": {
            "commit_sha": commit,
            "captured_at": "2026-09-26T00:00:00+00:00",
            "os": "Linux",
            "os_release": "test",
            "kernel": "test",
            "architecture": "x86_64",
            "python": "3.12",
            "container_runtime": "Docker test",
            "hardware": {
                "cpu_model": "Test CPU",
                "cpu_logical_cores": 16,
                "ram_total_bytes": 64 * 1024**3,
                "network_interfaces": [
                    {
                        "name": "eth0",
                        "is_up": True,
                        "speed_mbps": 10000,
                        "mtu": 1500,
                        "duplex": "2",
                    }
                ],
                "storage_path": "/bench",
                "storage_device": "/dev/nvme0n1",
                "storage_fstype": "xfs",
                "storage_total_bytes": 10 * 10**12,
                "storage_free_bytes": 8 * 10**12,
                "gpus": [],
            },
        },
        "workload": {
            "type": "event-ingest-http",
            "config": config if config is not None else {"events": 100000, "concurrency": 100},
            "started_at": "2026-09-26T00:00:00+00:00",
            "warmup_seconds": warmup,
            "duration_seconds": duration,
        },
        "result": {
            "operations_ok": int(throughput * max(duration, 1)),
            "operations_failed": 0,
            "failure_rate": failure_rate,
            "throughput_ops_s": throughput,
            "latency": {
                "count": 1000,
                "p50_ms": p50_ms,
                "p95_ms": p95_ms,
                "p99_ms": p99_ms,
                "max_ms": max_ms,
                "mean_ms": 7.0,
            },
        },
        "resources": {
            "samples": 600,
            "cpu_pct": {"mean": 50.0, "p95": 65.0, "max": 75.0},
            "cpu_freq_mhz": {"mean": 3000.0, "p95": 3200.0, "max": 3300.0},
            "cpu_freq_max_mhz": {"mean": 3500.0, "p95": 3500.0, "max": 3500.0},
            "cpu_freq_ratio_min": cpu_freq_ratio_min,
            "max_temperature_c": {"mean": 65.0, "p95": 75.0, "max": 80.0},
            "thermal_measured": thermal_measured,
            "thermal_limit_exceeded": thermal_limit_exceeded,
            "ram_used_bytes": {"mean": 1.0, "p95": 2.0, "max": 3.0},
            "ram_pct": {"mean": 50.0, "p95": 60.0, "max": 65.0},
            "net_rx_mbps": {"mean": 10.0, "p95": 12.0, "max": 15.0},
            "net_tx_mbps": {"mean": 10.0, "p95": 12.0, "max": 15.0},
            "disk_read_mbps": {"mean": 1.0, "p95": 2.0, "max": 3.0},
            "disk_write_mbps": {"mean": 1.0, "p95": 2.0, "max": 3.0},
            "gpu": [],
            "gpu_measured": False,
        },
    }


def stable_results():
    return [
        result(benchmark_id="b1", throughput=1000, p95_ms=10.0),
        result(benchmark_id="b2", throughput=980, p95_ms=10.5),
        result(benchmark_id="b3", throughput=1020, p95_ms=9.8),
    ]


def test_stable_three_repeat_group_passes():
    report = reproducibility.build_report(results=stable_results())
    assert report["summary"] == {"groups": 1, "passed": 1, "failed": 0}
    group = report["groups"][0]
    assert group["status"] == "PASS"
    assert group["repeat_count"] == 3
    assert group["metrics"]["capacity"]["dimension"] == "events_per_second"
    assert group["metrics"]["capacity"]["cv"] < 0.10
    assert group["metrics"]["p95_latency_ms"]["cv"] < 0.15


def test_unstable_throughput_fails_repeatability():
    results = [
        result(benchmark_id="b1", throughput=500),
        result(benchmark_id="b2", throughput=1000),
        result(benchmark_id="b3", throughput=1500),
    ]
    group = reproducibility.build_report(results=results)["groups"][0]
    assert group["status"] == "FAIL"
    assert any("capacity CV" in reason for reason in group["reasons"])
    assert any("capacity relative range" in reason for reason in group["reasons"])


def test_unstable_p95_latency_fails_repeatability():
    results = [
        result(benchmark_id="b1", p95_ms=5),
        result(benchmark_id="b2", p95_ms=10),
        result(benchmark_id="b3", p95_ms=20),
    ]
    group = reproducibility.build_report(results=results)["groups"][0]
    assert group["status"] == "FAIL"
    assert any("p95 latency CV" in reason for reason in group["reasons"])


def test_invalid_percentile_order_is_rejected():
    results = stable_results()
    results[1] = result(
        benchmark_id="b2",
        throughput=980,
        p50_ms=12,
        p95_ms=10,
        p99_ms=9,
        max_ms=20,
    )
    group = reproducibility.build_report(results=results)["groups"][0]
    assert group["status"] == "FAIL"
    assert any("percentile ordering" in reason for reason in group["reasons"])


def test_different_workload_configs_are_separate_groups_and_cannot_make_three_repeats():
    results = [
        result(benchmark_id="b1", config={"events": 100000, "concurrency": 100}),
        result(benchmark_id="b2", config={"events": 100000, "concurrency": 100}),
        result(benchmark_id="b3", config={"events": 100000, "concurrency": 200}),
    ]
    report = reproducibility.build_report(results=results)
    assert report["summary"]["groups"] == 2
    assert report["summary"]["passed"] == 0
    assert all(group["status"] == "FAIL" for group in report["groups"])


def test_short_duration_and_warmup_fail():
    results = [
        result(benchmark_id=f"b{i}", duration=20, warmup=1)
        for i in range(3)
    ]
    group = reproducibility.build_report(results=results)["groups"][0]
    assert group["status"] == "FAIL"
    reasons = " ".join(group["reasons"])
    assert "duration" in reasons
    assert "warmup" in reasons


def test_failure_rate_threshold_is_enforced():
    results = stable_results()
    results[2] = result(benchmark_id="b3", throughput=1020, failure_rate=0.02)
    group = reproducibility.build_report(results=results)["groups"][0]
    assert group["status"] == "FAIL"
    assert any("failure_rate" in reason for reason in group["reasons"])


def test_observed_thermal_limit_is_hard_failure():
    results = stable_results()
    results[2] = result(
        benchmark_id="b3",
        throughput=1020,
        thermal_limit_exceeded=True,
    )
    group = reproducibility.build_report(results=results)["groups"][0]
    assert group["status"] == "FAIL"
    assert any("thermal" in reason for reason in group["reasons"])


def test_missing_thermal_is_warning_by_default_and_failure_when_required():
    results = [
        result(benchmark_id=f"b{i}", thermal_measured=False)
        for i in range(3)
    ]
    default_group = reproducibility.build_report(results=results)["groups"][0]
    assert default_group["status"] == "PASS"
    assert any("thermal sensors" in warning for warning in default_group["warnings"])

    required_group = reproducibility.build_report(
        results=results,
        require_thermal=True,
    )["groups"][0]
    assert required_group["status"] == "FAIL"
    assert any("thermal evidence is required" in reason for reason in required_group["reasons"])


def test_low_frequency_ratio_is_visible_as_throttling_dvfs_warning():
    results = [
        result(benchmark_id=f"b{i}", cpu_freq_ratio_min=0.40)
        for i in range(3)
    ]
    group = reproducibility.build_report(results=results)["groups"][0]
    assert group["status"] == "PASS"
    assert any("frequency/max ratio" in warning for warning in group["warnings"])


def test_mixed_commits_and_hardware_cannot_be_grouped():
    a = stable_results()[:2]
    b = result(benchmark_id="b3", commit="commit-b")
    report = reproducibility.build_report(results=[*a, b])
    assert report["summary"]["groups"] == 2
    assert report["summary"]["passed"] == 0



def test_recording_repeatability_uses_recording_mbps_not_file_ops():
    results = stable_results()
    for index, item in enumerate(results):
        item["workload"]["type"] = "recording-directory-growth"
        item["workload"]["config"] = {"expected_streams": 500}
        item["result"]["observed_recording_mbps"] = [480.0, 500.0, 490.0][index]
        item["result"]["throughput_ops_s"] = [1.0, 100.0, 2.0][index]
    group = reproducibility.build_report(results=results)["groups"][0]
    assert group["status"] == "PASS"
    assert group["metrics"]["capacity"]["dimension"] == "recording_mbps"
    assert group["metrics"]["capacity"]["values"] == [480.0, 500.0, 490.0]


def test_storage_repeatability_uses_write_mbps():
    results = stable_results()
    for index, item in enumerate(results):
        item["workload"]["type"] = "synthetic-storage-write"
        item["workload"]["config"] = {"streams": 32, "fsync_each_chunk": True}
        item["result"]["aggregate_write_mbps"] = [8000.0, 7900.0, 8100.0][index]
    group = reproducibility.build_report(results=results)["groups"][0]
    assert group["status"] == "PASS"
    assert group["metrics"]["capacity"]["dimension"] == "storage_write_mbps"
