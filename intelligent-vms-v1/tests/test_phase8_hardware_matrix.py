import copy
import sys
from pathlib import Path

TOOLS = Path(__file__).parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import phase8_benchmark_common as common
import phase8_hardware_matrix as matrix
import phase8_reproducibility as reproducibility


def fake_result(
    *,
    benchmark_id,
    workload_type="event-ingest-http",
    capacity=1000.0,
    commit="commit-a",
    duration=600.0,
    warmup=60.0,
    failure_rate=0.0,
    cpu_p95=50.0,
    ram_p95=60.0,
    nic_speed_mbps=10000,
    storage_device="/dev/nvme0n1",
):
    metrics = {
        "operations_ok": int(capacity * max(1, duration)),
        "operations_failed": 0,
        "failure_rate": failure_rate,
        "throughput_ops_s": capacity,
        "latency": {
            "count": 10,
            "p50_ms": 1.0,
            "p95_ms": 2.0,
            "p99_ms": 3.0,
            "max_ms": 4.0,
            "mean_ms": 1.5,
        },
    }
    if workload_type == "recording-directory-growth":
        metrics["observed_recording_mbps"] = capacity
    if workload_type == "synthetic-storage-write":
        metrics["aggregate_write_mbps"] = capacity
    if workload_type == "media-relay":
        metrics["observed_media_mbps"] = capacity
    if workload_type == "ai-inference":
        metrics["observed_ai_mpix_s"] = capacity
    if workload_type == "control-api":
        metrics["throughput_ops_s"] = capacity

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
                        "speed_mbps": nic_speed_mbps,
                        "mtu": 1500,
                        "duplex": "2",
                    }
                ],
                "storage_path": "/bench",
                "storage_device": storage_device,
                "storage_fstype": "xfs",
                "storage_total_bytes": 10 * 10**12,
                "storage_free_bytes": 8 * 10**12,
                "gpus": [],
            },
        },
        "workload": {
            "type": workload_type,
            "config": {},
            "started_at": "2026-09-26T00:00:00+00:00",
            "warmup_seconds": warmup,
            "duration_seconds": duration,
        },
        "result": metrics,
        "resources": {
            "samples": 100,
            "cpu_pct": {"mean": 40.0, "p95": cpu_p95, "max": 60.0},
            "ram_used_bytes": {"mean": 1.0, "p95": 2.0, "max": 3.0},
            "ram_pct": {"mean": 50.0, "p95": ram_p95, "max": 70.0},
            "net_rx_mbps": {"mean": 1.0, "p95": 2.0, "max": 3.0},
            "net_tx_mbps": {"mean": 1.0, "p95": 2.0, "max": 3.0},
            "disk_read_mbps": {"mean": 1.0, "p95": 2.0, "max": 3.0},
            "disk_write_mbps": {"mean": 1.0, "p95": 2.0, "max": 3.0},
            "gpu": [],
            "gpu_measured": False,
        },
    }


def demand(events=1500.0):
    return {
        "required_roles": {"event_ingest": "events_per_second"},
        "profiles": [
            {
                "name": "site-500",
                "cameras": 500,
                "demands": {"events_per_second": events},
            }
        ],
    }


def test_three_repeats_use_conservative_minimum_and_n_plus_one():
    results = [
        fake_result(benchmark_id="b1", capacity=1000),
        fake_result(benchmark_id="b2", capacity=900),
        fake_result(benchmark_id="b3", capacity=950),
    ]
    output = matrix.build_matrix(
        results=results,
        demand=demand(1500),
        design_headroom_fraction=0.80,
    )
    role = output["profiles"][0]["roles"]["event_ingest"]
    assert role["status"] == "QUALIFIED_FROM_MEASURED_EVIDENCE"
    assert role["observed_capacity_per_node_min"] == 900
    assert role["safe_capacity_per_node"] == 720
    assert role["base_nodes"] == 3
    assert role["nodes_required"] == 4
    assert len(role["evidence"]["benchmark_ids"]) == 3


def test_two_repeats_remain_unqualified():
    output = matrix.build_matrix(
        results=[
            fake_result(benchmark_id="b1"),
            fake_result(benchmark_id="b2"),
        ],
        demand=demand(),
    )
    role = output["profiles"][0]["roles"]["event_ingest"]
    assert role["status"] == "UNQUALIFIED"
    assert ">= 3 qualified repeats" in role["reason"]


def test_mixed_commits_do_not_combine_into_repeat_count():
    output = matrix.build_matrix(
        results=[
            fake_result(benchmark_id="b1", commit="commit-a"),
            fake_result(benchmark_id="b2", commit="commit-a"),
            fake_result(benchmark_id="b3", commit="commit-b"),
        ],
        demand=demand(),
    )
    assert output["profiles"][0]["roles"]["event_ingest"]["status"] == "UNQUALIFIED"


def test_short_ci_smoke_result_is_rejected_as_hardware_evidence():
    results = [
        fake_result(benchmark_id=f"b{i}", duration=2, warmup=0)
        for i in range(3)
    ]
    output = matrix.build_matrix(results=results, demand=demand())
    assert output["profiles"][0]["roles"]["event_ingest"]["status"] == "UNQUALIFIED"
    assert len(output["rejected_evidence"]) == 3
    assert "duration" in output["rejected_evidence"][0]["reasons"][0]


def test_high_resource_or_failure_run_is_rejected():
    bad_cpu = fake_result(benchmark_id="cpu", cpu_p95=90)
    bad_ram = fake_result(benchmark_id="ram", ram_p95=90)
    bad_failure = fake_result(benchmark_id="fail", failure_rate=0.02)
    output = matrix.build_matrix(
        results=[bad_cpu, bad_ram, bad_failure],
        demand=demand(),
        min_repeats=1,
    )
    assert output["profiles"][0]["roles"]["event_ingest"]["status"] == "UNQUALIFIED"
    reasons = " ".join(
        reason
        for row in output["rejected_evidence"]
        for reason in row["reasons"]
    )
    assert "CPU p95" in reasons
    assert "RAM p95" in reasons
    assert "failure_rate" in reasons


def test_missing_deployment_demand_is_never_guessed():
    output = matrix.build_matrix(
        results=[
            fake_result(benchmark_id="b1"),
            fake_result(benchmark_id="b2"),
            fake_result(benchmark_id="b3"),
        ],
        demand={
            "required_roles": {"event_ingest": "events_per_second"},
            "profiles": [{"name": "site-500", "cameras": 500, "demands": {}}],
        },
    )
    role = output["profiles"][0]["roles"]["event_ingest"]
    assert role["status"] == "UNQUALIFIED"
    assert role["reason"] == "deployment demand not provided"


def test_roles_without_measured_workload_stay_unqualified():
    output = matrix.build_matrix(
        results=[
            fake_result(benchmark_id="b1"),
            fake_result(benchmark_id="b2"),
            fake_result(benchmark_id="b3"),
        ],
        demand={
            "required_roles": {
                "event_ingest": "events_per_second",
                "media": "media_mbps",
            },
            "profiles": [
                {
                    "name": "site-500",
                    "cameras": 500,
                    "demands": {
                        "events_per_second": 100,
                        "media_mbps": 500,
                    },
                }
            ],
        },
    )
    assert output["profiles"][0]["roles"]["event_ingest"]["status"].startswith("QUALIFIED")
    assert output["profiles"][0]["roles"]["media"]["status"] == "UNQUALIFIED"



def test_different_nic_or_storage_identity_does_not_combine_repeats():
    output = matrix.build_matrix(
        results=[
            fake_result(benchmark_id="b1", nic_speed_mbps=10000),
            fake_result(benchmark_id="b2", nic_speed_mbps=10000),
            fake_result(
                benchmark_id="b3",
                nic_speed_mbps=1000,
                storage_device="/dev/nvme1n1",
            ),
        ],
        demand=demand(),
    )
    assert output["profiles"][0]["roles"]["event_ingest"]["status"] == "UNQUALIFIED"



def test_reproducibility_filter_excludes_unapproved_benchmark_ids():
    results = [
        fake_result(benchmark_id="b1", capacity=1000),
        fake_result(benchmark_id="b2", capacity=1000),
        fake_result(benchmark_id="b3", capacity=1000),
        fake_result(benchmark_id="unapproved-fast-run", capacity=100000),
    ]
    approved = {
        item["benchmark_id"]: common.result_fingerprint(item)
        for item in results[:3]
    }
    output = matrix.build_matrix(
        results=results,
        demand=demand(1000),
        approved_benchmark_fingerprints=approved,
    )
    role = output["profiles"][0]["roles"]["event_ingest"]
    assert role["status"] == "QUALIFIED_FROM_MEASURED_EVIDENCE"
    assert role["observed_capacity_per_node_min"] == 1000
    assert output["qa_filter_applied"] is True
    assert output["qa_excluded_benchmark_ids"] == ["unapproved-fast-run"]
    assert output["qa_fingerprint_mismatch_benchmark_ids"] == []


def test_reproducibility_report_parser_returns_only_pass_group_fingerprints(tmp_path):
    report = {
        "report_version": "phase8-reproducibility-v1",
        "groups": [
            {
                "status": "PASS",
                "benchmark_ids": ["b1", "b2", "b3"],
                "benchmark_fingerprints": {
                    "b1": "fp1",
                    "b2": "fp2",
                    "b3": "fp3",
                },
            },
            {
                "status": "FAIL",
                "benchmark_ids": ["bad1"],
                "benchmark_fingerprints": {"bad1": "badfp"},
            },
        ],
    }
    path = tmp_path / "repro.json"
    import json
    path.write_text(json.dumps(report))
    assert matrix.approved_fingerprints_from_reproducibility_report(str(path)) == {
        "b1": "fp1",
        "b2": "fp2",
        "b3": "fp3",
    }


def test_changed_result_is_rejected_even_when_benchmark_id_was_approved():
    approved_results = [
        fake_result(benchmark_id="b1", capacity=1000),
        fake_result(benchmark_id="b2", capacity=1000),
        fake_result(benchmark_id="b3", capacity=1000),
    ]
    approved = {
        item["benchmark_id"]: common.result_fingerprint(item)
        for item in approved_results
    }

    tampered = [dict(item) for item in approved_results]
    tampered[2] = fake_result(benchmark_id="b3", capacity=100000)

    output = matrix.build_matrix(
        results=tampered,
        demand=demand(1000),
        approved_benchmark_fingerprints=approved,
    )

    assert output["profiles"][0]["roles"]["event_ingest"]["status"] == "UNQUALIFIED"
    assert output["qa_fingerprint_mismatch_benchmark_ids"] == ["b3"]


def test_three_copies_of_one_benchmark_cannot_qualify_hardware_matrix():
    one_run = fake_result(benchmark_id="same-run", capacity=1000)
    copies = [copy.deepcopy(one_run) for _ in range(3)]

    output = matrix.build_matrix(
        results=copies,
        demand=demand(1500),
        design_headroom_fraction=0.80,
    )

    role = output["profiles"][0]["roles"]["event_ingest"]
    assert role["status"] == "UNQUALIFIED"
    assert "duplicate benchmark identities" in role["reason"]
    assert "duplicate benchmark fingerprints" in role["reason"]
    assert "same-run" in role["reason"]
    assert output["qualified_evidence"] == []
    assert "observed_capacity_per_node_min" not in role
    assert "nodes_required" not in role


def test_repeated_benchmark_id_cannot_qualify_even_when_payloads_differ():
    output = matrix.build_matrix(
        results=[
            fake_result(benchmark_id="same-run", capacity=1000),
            fake_result(benchmark_id="same-run", capacity=980),
            fake_result(benchmark_id="same-run", capacity=1020),
        ],
        demand=demand(1500),
    )

    role = output["profiles"][0]["roles"]["event_ingest"]
    assert role["status"] == "UNQUALIFIED"
    assert "duplicate benchmark identities" in role["reason"]
    assert "duplicate benchmark fingerprints" not in role["reason"]
    assert output["qualified_evidence"] == []
    assert "observed_capacity_per_node_min" not in role


def test_reproducibility_handoff_rejects_duplicated_benchmark_identity():
    one_run = fake_result(benchmark_id="same-run", capacity=1000)
    copies = [copy.deepcopy(one_run) for _ in range(3)]
    report = reproducibility.build_report(results=copies)
    assert report["groups"][0]["status"] == "FAIL"

    approved = {}
    for group in report["groups"]:
        if group["status"] == "PASS":
            approved.update(group["benchmark_fingerprints"])

    output = matrix.build_matrix(
        results=copies,
        demand=demand(1500),
        approved_benchmark_fingerprints=approved,
    )

    role = output["profiles"][0]["roles"]["event_ingest"]
    assert role["status"] == "UNQUALIFIED"
    assert output["qualified_evidence"] == []
    assert "QUALIFIED_FROM_MEASURED_EVIDENCE" not in role["status"]


def test_extra_copy_does_not_qualify_hardware_matrix():
    output = matrix.build_matrix(
        results=[
            fake_result(benchmark_id="run-1", capacity=1000),
            fake_result(benchmark_id="run-2", capacity=900),
            fake_result(benchmark_id="run-3", capacity=950),
            fake_result(benchmark_id="run-1", capacity=940),
        ],
        demand=demand(1500),
    )

    role = output["profiles"][0]["roles"]["event_ingest"]
    assert role["status"] == "UNQUALIFIED"
    assert "duplicate benchmark identities" in role["reason"]
    assert "run-1" in role["reason"]
    assert output["qualified_evidence"] == []
    assert "observed_capacity_per_node_min" not in role


def test_duplicate_fingerprint_with_distinct_ids_cannot_qualify():
    source = matrix.extract_evidence(
        fake_result(benchmark_id="run-1", capacity=1000),
        min_duration_seconds=300,
        min_warmup_seconds=30,
        max_failure_rate=0.001,
        max_cpu_p95_pct=70,
        max_ram_p95_pct=75,
    )
    assert source is not None
    copies = [
        matrix.Evidence(
            benchmark_id=f"run-{index}",
            fingerprint=source.fingerprint,
            commit_sha=source.commit_sha,
            hardware_key=source.hardware_key,
            hardware=source.hardware,
            role=source.role,
            dimension=source.dimension,
            observed_capacity=source.observed_capacity,
            duration_seconds=source.duration_seconds,
            warmup_seconds=source.warmup_seconds,
            failure_rate=source.failure_rate,
            cpu_p95_pct=source.cpu_p95_pct,
            ram_p95_pct=source.ram_p95_pct,
            qualified=True,
            reasons=(),
        )
        for index in range(3)
    ]

    assert matrix.grouped_qualified_evidence(copies, min_repeats=3) == {}
    reason = matrix._role_duplicate_reason(
        copies,
        role="event_ingest",
        dimension="events_per_second",
    )
    assert reason == "duplicate benchmark fingerprints are not independent repeats"


def test_three_distinct_benchmarks_still_qualify_hardware_matrix():
    results = [
        fake_result(benchmark_id="run-1", capacity=1000),
        fake_result(benchmark_id="run-2", capacity=900),
        fake_result(benchmark_id="run-3", capacity=950),
    ]

    output = matrix.build_matrix(
        results=results,
        demand=demand(1500),
        design_headroom_fraction=0.80,
    )

    role = output["profiles"][0]["roles"]["event_ingest"]
    assert role["status"] == "QUALIFIED_FROM_MEASURED_EVIDENCE"
    assert role["observed_capacity_per_node_min"] == 900
    assert role["evidence"]["repeat_count"] == 3
    assert role["evidence"]["benchmark_ids"] == ["run-1", "run-2", "run-3"]
