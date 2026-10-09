"""Regression lock for FIX-124 residual REV-124-001 / QA-124-001.

A direct build_matrix call must not report QUALIFIED_FROM_MEASURED_EVIDENCE
for a reproducibility group that failed. The capacities 20000, 30000 and
40000 are synthetic fixture values. They are not a hardware measurement.
"""

import json
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import phase8_benchmark_common as common
import phase8_hardware_matrix as matrix
import phase8_reproducibility as reproducibility


def _synthetic_storage(
    *,
    benchmark_id,
    aggregate_write_mbps,
    p50_ms=1.0,
    p95_ms=2.0,
    p99_ms=3.0,
    max_ms=4.0,
):
    """Build one synthetic storage record. Nothing here was measured on hardware."""
    return {
        "schema_version": "phase8-benchmark-v1",
        "benchmark_id": benchmark_id,
        "environment": {
            "commit_sha": "synthetic-commit",
            "captured_at": "2026-10-09T00:00:00+00:00",
            "os": "Synthetic",
            "os_release": "fixture",
            "kernel": "fixture",
            "architecture": "x86_64",
            "python": "3.12",
            "container_runtime": "fixture",
            "hardware": {
                "cpu_model": "Synthetic CPU",
                "cpu_logical_cores": 4,
                "ram_total_bytes": 8 * 1024**3,
                "network_interfaces": [
                    {
                        "name": "eth0",
                        "is_up": True,
                        "speed_mbps": 1000,
                        "mtu": 1500,
                    }
                ],
                "storage_path": "/synthetic",
                "storage_device": "/dev/synthetic",
                "storage_fstype": "fixture",
                "storage_total_bytes": 10**12,
                "storage_free_bytes": 10**11,
                "gpus": [],
            },
        },
        "workload": {
            "type": "synthetic-storage-write",
            "config": {},
            "started_at": "2026-10-09T00:00:00+00:00",
            "warmup_seconds": 60.0,
            "duration_seconds": 600.0,
        },
        "result": {
            "operations_ok": 100,
            "operations_failed": 0,
            "failure_rate": 0.0,
            "throughput_ops_s": 10.0,
            "bytes_written": 1048576,
            "aggregate_write_mbps": aggregate_write_mbps,
            "aggregate_write_MBps": aggregate_write_mbps / 8.0,
            "latency": {
                "count": 4,
                "p50_ms": p50_ms,
                "p95_ms": p95_ms,
                "p99_ms": p99_ms,
                "max_ms": max_ms,
                "mean_ms": p50_ms,
            },
        },
        "resources": {
            "samples": 100,
            "cpu_pct": {"mean": 10.0, "p95": 20.0, "max": 30.0},
            "cpu_freq_mhz": {"mean": 2400.0, "p95": 2400.0, "max": 2400.0},
            "ram_used_bytes": {"mean": 1.0, "p95": 2.0, "max": 3.0},
            "ram_pct": {"mean": 40.0, "p95": 50.0, "max": 60.0},
            "net_rx_mbps": {"mean": 1.0, "p95": 2.0, "max": 3.0},
            "net_tx_mbps": {"mean": 1.0, "p95": 2.0, "max": 3.0},
            "disk_read_mbps": {"mean": 1.0, "p95": 2.0, "max": 3.0},
            "disk_write_mbps": {"mean": 1.0, "p95": 2.0, "max": 3.0},
            "gpu": [],
            "gpu_measured": False,
        },
    }


def _storage_demand(requested=1500.0):
    return {
        "required_roles": {"storage": "storage_write_mbps"},
        "profiles": [
            {
                "name": "synthetic-probe",
                "cameras": 1,
                "demands": {"storage_write_mbps": requested},
            }
        ],
    }


def _failed_cv_group():
    """Three finite synthetic storage rows that fail capacity CV and relative range."""
    return [
        _synthetic_storage(benchmark_id="synthetic-cv-20000", aggregate_write_mbps=20000.0),
        _synthetic_storage(benchmark_id="synthetic-cv-30000", aggregate_write_mbps=30000.0),
        _synthetic_storage(benchmark_id="synthetic-cv-40000", aggregate_write_mbps=40000.0),
    ]


def _assert_unqualified(output):
    rendered = json.dumps(output)
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in rendered
    assert output["qualified_evidence"] == []
    role = output["profiles"][0]["roles"]["storage"]
    assert role["status"] == "UNQUALIFIED"
    assert "nodes_required" not in role
    assert "capacity CV" in role["reason"]
    assert "capacity relative range" in role["reason"]
    rejected = " ".join(
        reason for row in output["rejected_evidence"] for reason in row["reasons"]
    )
    assert "capacity CV" in rejected
    assert "capacity relative range" in rejected


def test_direct_build_matrix_does_not_qualify_failed_capacity_cv_group():
    results = _failed_cv_group()
    report = reproducibility.build_report(results=results)
    assert report["summary"] == {"groups": 1, "passed": 0, "failed": 1}
    reasons = " ".join(report["groups"][0]["reasons"])
    assert "capacity CV" in reasons
    assert "capacity relative range" in reasons

    direct = matrix.build_matrix(results=results, demand=_storage_demand())
    _assert_unqualified(direct)

    approved = {
        item["benchmark_id"]: common.result_fingerprint(item) for item in results
    }
    forced = matrix.build_matrix(
        results=results,
        demand=_storage_demand(),
        approved_benchmark_fingerprints=approved,
    )
    _assert_unqualified(forced)


def test_direct_build_matrix_does_not_qualify_failed_p95_latency_cv_group():
    """Stable synthetic capacity with a failed p95-latency CV stays unqualified.

    These latencies are fixture numbers, not a hardware measurement.
    """
    results = [
        _synthetic_storage(
            benchmark_id="synthetic-p95-a",
            aggregate_write_mbps=1000.0,
            p50_ms=1.0,
            p95_ms=10.0,
            p99_ms=11.0,
            max_ms=12.0,
        ),
        _synthetic_storage(
            benchmark_id="synthetic-p95-b",
            aggregate_write_mbps=1000.0,
            p50_ms=1.0,
            p95_ms=80.0,
            p99_ms=81.0,
            max_ms=82.0,
        ),
        _synthetic_storage(
            benchmark_id="synthetic-p95-c",
            aggregate_write_mbps=1000.0,
            p50_ms=1.0,
            p95_ms=200.0,
            p99_ms=201.0,
            max_ms=202.0,
        ),
    ]
    report = reproducibility.build_report(results=results)
    assert report["groups"][0]["status"] == "FAIL"
    assert any("p95 latency CV" in reason for reason in report["groups"][0]["reasons"])
    assert not any("capacity CV" in reason for reason in report["groups"][0]["reasons"])

    output = matrix.build_matrix(results=results, demand=_storage_demand())
    rendered = json.dumps(output)
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in rendered
    role = output["profiles"][0]["roles"]["storage"]
    assert role["status"] == "UNQUALIFIED"
    assert "nodes_required" not in role
    assert output["qualified_evidence"] == []
    assert "p95 latency CV" in role["reason"]


def _dispersed_identity_group():
    """Five synthetic rows in one commit/hardware/workload group.

    1000, 1010 and 1020 are tight. 400 and 2500 make the full group fail
    capacity CV and relative range. These are fixture numbers, not hardware
    measurements.
    """
    capacities = (
        ("synthetic-tight-1000", 1000.0),
        ("synthetic-tight-1010", 1010.0),
        ("synthetic-tight-1020", 1020.0),
        ("synthetic-dispersed-400", 400.0),
        ("synthetic-dispersed-2500", 2500.0),
    )
    return [
        _synthetic_storage(benchmark_id=benchmark_id, aggregate_write_mbps=capacity)
        for benchmark_id, capacity in capacities
    ]


def _assert_tight_three_still_qualify(results):
    """The same three tight rows qualify when the dispersed rows are absent."""
    output = matrix.build_matrix(
        results=results,
        demand=_storage_demand(1000.0),
        approved_benchmark_fingerprints={
            item["benchmark_id"]: common.result_fingerprint(item) for item in results
        },
    )
    role = output["profiles"][0]["roles"]["storage"]
    assert role["status"] == "QUALIFIED_FROM_MEASURED_EVIDENCE"
    assert role["observed_capacity_per_node_min"] == 1000.0
    assert role["nodes_required"] == 3


def test_partial_fingerprint_map_cannot_qualify_dispersed_group():
    results = _dispersed_identity_group()
    report = reproducibility.build_report(results=results)
    assert report["summary"] == {"groups": 1, "passed": 0, "failed": 1}
    reasons = " ".join(report["groups"][0]["reasons"])
    assert "capacity CV 0.5887" in reasons
    assert "capacity relative range 1.7707" in reasons

    tight = results[:3]
    approved = {
        item["benchmark_id"]: common.result_fingerprint(item) for item in tight
    }
    output = matrix.build_matrix(
        results=results,
        demand=_storage_demand(1000.0),
        approved_benchmark_fingerprints=approved,
    )
    rendered = json.dumps(output)
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in rendered
    assert output["qualified_evidence"] == []
    role = output["profiles"][0]["roles"]["storage"]
    assert role["status"] == "UNQUALIFIED"
    assert "nodes_required" not in role
    assert "observed_capacity_per_node_min" not in role
    assert "capacity CV" in role["reason"]
    assert "capacity relative range" in role["reason"]
    assert output["qa_excluded_benchmark_ids"] == [
        "synthetic-dispersed-400",
        "synthetic-dispersed-2500",
    ]

    _assert_tight_three_still_qualify(tight)
    other = _synthetic_storage(
        benchmark_id="synthetic-other-commit",
        aggregate_write_mbps=2500.0,
    )
    other["environment"]["commit_sha"] = "synthetic-other-commit"
    separated = matrix.build_matrix(
        results=[*tight, other],
        demand=_storage_demand(1000.0),
        approved_benchmark_fingerprints=approved,
    )
    separated_role = separated["profiles"][0]["roles"]["storage"]
    assert separated_role["status"] == "QUALIFIED_FROM_MEASURED_EVIDENCE"
    assert separated_role["observed_capacity_per_node_min"] == 1000.0
    assert separated["qa_excluded_benchmark_ids"] == ["synthetic-other-commit"]


def test_cli_subset_reproducibility_report_cannot_qualify_full_results(tmp_path):
    results = _dispersed_identity_group()
    tight = results[:3]
    result_dir = tmp_path / "results"
    tight_dir = tmp_path / "tight"
    result_dir.mkdir()
    tight_dir.mkdir()
    for item in results:
        (result_dir / f"{item['benchmark_id']}.json").write_text(
            json.dumps(item),
            encoding="utf-8",
        )
    for item in tight:
        (tight_dir / f"{item['benchmark_id']}.json").write_text(
            json.dumps(item),
            encoding="utf-8",
        )
    demand_path = tmp_path / "demand.json"
    demand_path.write_text(json.dumps(_storage_demand(1000.0)), encoding="utf-8")
    report_path = tmp_path / "reproducibility.json"
    matrix_path = tmp_path / "hardware-matrix.json"
    tight_report_cli = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "phase8_reproducibility.py"),
            "--results",
            str(tight_dir),
            "--output",
            str(report_path),
            "--fail-on-rejected-groups",
        ],
        cwd=TOOLS.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    assert tight_report_cli.returncode == 0, tight_report_cli.stdout + tight_report_cli.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"]["passed"] == 1
    assert set(report["groups"][0]["benchmark_ids"]) == {
        "synthetic-tight-1000",
        "synthetic-tight-1010",
        "synthetic-tight-1020",
    }
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
        ],
        cwd=TOOLS.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    assert matrix_cli.returncode == 0, matrix_cli.stdout + matrix_cli.stderr
    produced = json.loads(matrix_path.read_text(encoding="utf-8"))
    rendered = json.dumps(produced)
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in rendered
    role = produced["profiles"][0]["roles"]["storage"]
    assert role["status"] == "UNQUALIFIED"
    assert "nodes_required" not in role
    assert "observed_capacity_per_node_min" not in role
    assert "covers a subset" in role["reason"]
    assert "synthetic-dispersed-400" in role["reason"]
    assert "synthetic-dispersed-2500" in role["reason"]

    tight_matrix_path = tmp_path / "tight-matrix.json"
    tight_matrix_cli = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "phase8_hardware_matrix.py"),
            "--results",
            str(tight_dir),
            "--demand",
            str(demand_path),
            "--reproducibility-report",
            str(report_path),
            "--output",
            str(tight_matrix_path),
        ],
        cwd=TOOLS.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    assert tight_matrix_cli.returncode == 0, tight_matrix_cli.stdout + tight_matrix_cli.stderr
    tight_produced = json.loads(tight_matrix_path.read_text(encoding="utf-8"))
    tight_role = tight_produced["profiles"][0]["roles"]["storage"]
    assert tight_role["status"] == "QUALIFIED_FROM_MEASURED_EVIDENCE"
    assert tight_role["observed_capacity_per_node_min"] == 1000.0
    assert tight_role["nodes_required"] == 3
