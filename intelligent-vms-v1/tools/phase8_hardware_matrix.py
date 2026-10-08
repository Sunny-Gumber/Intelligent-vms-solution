#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from phase8_benchmark_common import result_fingerprint, validate_result


MATRIX_VERSION = "phase8-hardware-matrix-v1"


@dataclass(frozen=True)
class Evidence:
    """Qualified-capacity evidence extracted from one benchmark result.

    Attributes:
        benchmark_id: Source benchmark identifier.
        fingerprint: Immutable full-result fingerprint, including benchmark_id.
        commit_sha: Benchmarked source revision.
        hardware_key: Stable hardware/environment identity.
        hardware: Captured hardware metadata.
        role: Capacity role represented by the workload.
        dimension: Measured capacity dimension.
        observed_capacity: Observed workload capacity.
        duration_seconds: Measured workload duration.
        warmup_seconds: Warmup duration.
        failure_rate: Observed operation failure rate.
        cpu_p95_pct: CPU p95 utilization when measured.
        ram_p95_pct: RAM p95 utilization when measured.
        qualified: Whether policy thresholds are satisfied.
        reasons: Qualification rejection reasons.
    """

    benchmark_id: str
    fingerprint: str
    commit_sha: str
    hardware_key: str
    hardware: dict[str, Any]
    role: str
    dimension: str
    observed_capacity: float
    duration_seconds: float
    warmup_seconds: float
    failure_rate: float
    cpu_p95_pct: float | None
    ram_p95_pct: float | None
    qualified: bool
    reasons: tuple[str, ...]


def _num(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _repeated_values(values: list[str]) -> list[str]:
    """Return values that occur more than once, in sorted order.

    Args:
        values: Benchmark identities or result fingerprints in submission order.

    Returns:
        Deterministic list of values whose count is greater than one.
    """
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return sorted(value for value, count in counts.items() if count > 1)


def _duplicate_repeat_reason(items: list[Evidence]) -> str | None:
    """Reject copies that reuse one benchmark identity or result fingerprint.

    Args:
        items: Evidence rows already grouped by role, dimension, commit and hardware.

    Returns:
        Rejection reason when an identity or fingerprint repeats, otherwise None.
    """
    reasons: list[str] = []
    duplicate_identities = _repeated_values([item.benchmark_id for item in items])
    if duplicate_identities:
        reasons.append(
            "duplicate benchmark identities are not independent repeats: "
            + ", ".join(duplicate_identities)
        )
    duplicate_fingerprints = _repeated_values([item.fingerprint for item in items])
    if duplicate_fingerprints:
        reasons.append("duplicate benchmark fingerprints are not independent repeats")
    if not reasons:
        return None
    return "; ".join(reasons)


def _role_duplicate_reason(
    evidence: list[Evidence],
    *,
    role: str,
    dimension: str,
) -> str | None:
    """Find a duplicate-repeat rejection for one demanded role.

    Args:
        evidence: Extracted benchmark evidence, including rows that failed thresholds.
        role: Capacity role being qualified.
        dimension: Capacity dimension required for that role.

    Returns:
        A duplicate identity or fingerprint reason for a qualified-threshold group,
        or None when no such group reused an identity or fingerprint.
    """
    groups: dict[tuple[str, str], list[Evidence]] = {}
    for item in evidence:
        if not item.qualified or item.role != role or item.dimension != dimension:
            continue
        groups.setdefault((item.commit_sha, item.hardware_key), []).append(item)
    reasons = [
        reason
        for grouped in groups.values()
        if (reason := _duplicate_repeat_reason(grouped)) is not None
    ]
    if not reasons:
        return None
    return sorted(reasons)[0]


def hardware_key(result: dict[str, Any]) -> str:
    """Build a stable identity for benchmark commit and hardware characteristics.

    Args:
        result: Validated Phase-8 benchmark result.

    Returns:
        Twenty-character SHA-256-derived hardware identity.
    """
    env = result["environment"]
    hardware = env.get("hardware", {})
    material = {
        "commit_sha": env.get("commit_sha"),
        "cpu_model": hardware.get("cpu_model"),
        "cpu_logical_cores": hardware.get("cpu_logical_cores"),
        "ram_total_bytes": hardware.get("ram_total_bytes"),
        "network_interfaces": hardware.get("network_interfaces", []),
        "gpus": hardware.get("gpus", []),
        "storage_path": hardware.get("storage_path"),
        "storage_device": hardware.get("storage_device"),
        "storage_fstype": hardware.get("storage_fstype"),
        "storage_total_bytes": hardware.get("storage_total_bytes"),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:20]


def capacity_dimension(result: dict[str, Any]) -> tuple[str, str, float] | None:
    """Map a supported workload result to role, dimension and measured capacity.

    Args:
        result: Validated Phase-8 benchmark result.

    Returns:
        Tuple of role, capacity-dimension name and positive observed capacity,
        or None when the workload has no qualification mapping.
    """
    workload_type = result["workload"]["type"]
    metrics = result["result"]

    if workload_type == "event-ingest-http":
        capacity = _num(metrics.get("throughput_ops_s"))
        return ("event_ingest", "events_per_second", capacity) if capacity and capacity > 0 else None

    if workload_type == "recording-directory-growth":
        capacity = _num(metrics.get("observed_recording_mbps"))
        return ("recording", "recording_mbps", capacity) if capacity and capacity > 0 else None

    if workload_type == "synthetic-storage-write":
        capacity = _num(metrics.get("aggregate_write_mbps"))
        return ("storage", "storage_write_mbps", capacity) if capacity and capacity > 0 else None

    # Future media/AI benchmark drivers must emit explicit measured capacity.
    if workload_type == "media-relay":
        capacity = _num(metrics.get("observed_media_mbps"))
        return ("media", "media_mbps", capacity) if capacity and capacity > 0 else None

    if workload_type == "ai-inference":
        capacity = _num(metrics.get("observed_ai_mpix_s"))
        return ("ai", "ai_mpix_s", capacity) if capacity and capacity > 0 else None

    if workload_type == "control-api":
        capacity = _num(metrics.get("throughput_ops_s"))
        return ("control_api", "control_ops_per_second", capacity) if capacity and capacity > 0 else None

    return None


def extract_evidence(
    result: dict[str, Any],
    *,
    min_duration_seconds: float,
    min_warmup_seconds: float,
    max_failure_rate: float,
    max_cpu_p95_pct: float,
    max_ram_p95_pct: float,
) -> Evidence | None:
    """Evaluate one benchmark result against hardware-qualification thresholds.

    Args:
        result: Phase-8 benchmark result.
        min_duration_seconds: Minimum measured workload duration.
        min_warmup_seconds: Minimum warmup duration.
        max_failure_rate: Maximum acceptable operation failure rate.
        max_cpu_p95_pct: Maximum acceptable CPU p95 utilization.
        max_ram_p95_pct: Maximum acceptable RAM p95 utilization.

    Returns:
        Evidence object with qualification reasons, or None for an unmapped workload.

    Raises:
        ValueError: If benchmark result validation fails.
    """
    validate_result(result)
    mapping = capacity_dimension(result)
    if mapping is None:
        return None
    role, dimension, capacity = mapping
    failure_rate = _num(result["result"].get("failure_rate"))
    failure_rate = 0.0 if failure_rate is None else failure_rate
    duration = float(result["workload"].get("duration_seconds") or 0.0)
    warmup = float(result["workload"].get("warmup_seconds") or 0.0)
    cpu_p95 = _num(result["resources"].get("cpu_pct", {}).get("p95"))
    ram_p95 = _num(result["resources"].get("ram_pct", {}).get("p95"))

    reasons = []
    if duration < min_duration_seconds:
        reasons.append(f"duration {duration:.1f}s < required {min_duration_seconds:.1f}s")
    if warmup < min_warmup_seconds:
        reasons.append(f"warmup {warmup:.1f}s < required {min_warmup_seconds:.1f}s")
    if failure_rate > max_failure_rate:
        reasons.append(
            f"failure_rate {failure_rate:.6f} > allowed {max_failure_rate:.6f}"
        )
    if cpu_p95 is None:
        reasons.append("CPU p95 not measured")
    elif cpu_p95 > max_cpu_p95_pct:
        reasons.append(f"CPU p95 {cpu_p95:.1f}% > allowed {max_cpu_p95_pct:.1f}%")
    if ram_p95 is None:
        reasons.append("RAM p95 not measured")
    elif ram_p95 > max_ram_p95_pct:
        reasons.append(f"RAM p95 {ram_p95:.1f}% > allowed {max_ram_p95_pct:.1f}%")

    env = result["environment"]
    return Evidence(
        benchmark_id=str(result["benchmark_id"]),
        fingerprint=result_fingerprint(result),
        commit_sha=str(env["commit_sha"]),
        hardware_key=hardware_key(result),
        hardware=dict(env.get("hardware", {})),
        role=role,
        dimension=dimension,
        observed_capacity=float(capacity),
        duration_seconds=duration,
        warmup_seconds=warmup,
        failure_rate=failure_rate,
        cpu_p95_pct=cpu_p95,
        ram_p95_pct=ram_p95,
        qualified=not reasons,
        reasons=tuple(reasons),
    )


def load_results(paths: list[str]) -> list[dict[str, Any]]:
    """Load and validate benchmark-result JSON files from files or directories.

    Args:
        paths: JSON files or directories containing JSON benchmark results.

    Returns:
        Validated benchmark result dictionaries.

    Raises:
        ValueError: If any benchmark result is invalid.
        OSError: If an input path cannot be read.
        json.JSONDecodeError: If an input file is not valid JSON.
    """
    results = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            candidates = sorted(path.glob("*.json"))
        else:
            candidates = [path]
        for candidate in candidates:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            validate_result(payload)
            results.append(payload)
    return results


def grouped_qualified_evidence(
    evidence: list[Evidence],
    *,
    min_repeats: int,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Select conservative repeated qualified evidence by role and dimension.

    Args:
        evidence: Extracted benchmark evidence records.
        min_repeats: Minimum repeated qualified runs per commit/hardware/workload.

    Returns:
        Best conservative qualified evidence row for each role/dimension pair.
        Groups that reuse a benchmark identity or result fingerprint are omitted
        so one run cannot satisfy the independent-repeat minimum.
    """
    groups: dict[tuple[str, str, str, str], list[Evidence]] = {}
    for item in evidence:
        if not item.qualified:
            continue
        key = (item.role, item.dimension, item.commit_sha, item.hardware_key)
        groups.setdefault(key, []).append(item)

    best: dict[tuple[str, str], dict[str, Any]] = {}
    for (role, dimension, commit_sha, hw_key), items in groups.items():
        if _duplicate_repeat_reason(items) is not None:
            continue
        independent_repeats = len({item.benchmark_id for item in items})
        if independent_repeats < min_repeats:
            continue
        conservative_capacity = min(i.observed_capacity for i in items)
        candidate = {
            "role": role,
            "dimension": dimension,
            "commit_sha": commit_sha,
            "hardware_key": hw_key,
            "hardware": items[0].hardware,
            "repeat_count": independent_repeats,
            "benchmark_ids": [i.benchmark_id for i in items],
            "observed_capacity_min": conservative_capacity,
            "observed_capacity_values": [i.observed_capacity for i in items],
            "max_failure_rate": max(i.failure_rate for i in items),
            "max_cpu_p95_pct": max(i.cpu_p95_pct or 0.0 for i in items),
            "max_ram_p95_pct": max(i.ram_p95_pct or 0.0 for i in items),
        }
        current = best.get((role, dimension))
        if current is None or candidate["observed_capacity_min"] > current["observed_capacity_min"]:
            best[(role, dimension)] = candidate
    return best


def build_matrix(
    *,
    results: list[dict[str, Any]],
    demand: dict[str, Any],
    min_repeats: int = 3,
    min_duration_seconds: float = 300.0,
    min_warmup_seconds: float = 30.0,
    max_failure_rate: float = 0.001,
    max_cpu_p95_pct: float = 70.0,
    max_ram_p95_pct: float = 75.0,
    design_headroom_fraction: float = 0.80,
    n_plus_one: bool = True,
    approved_benchmark_fingerprints: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build deployment node requirements strictly from qualified measured evidence.

    Duplicate benchmark identities and duplicate result fingerprints are not
    independent repeats and cannot produce QUALIFIED_FROM_MEASURED_EVIDENCE.

    Args:
        results: Phase-8 benchmark results.
        demand: Deployment demand profiles and required dimensions.
        min_repeats: Minimum qualified repeat count.
        min_duration_seconds: Minimum measured duration per run.
        min_warmup_seconds: Minimum warmup per run.
        max_failure_rate: Maximum accepted failure rate.
        max_cpu_p95_pct: Maximum accepted CPU p95 utilization.
        max_ram_p95_pct: Maximum accepted RAM p95 utilization.
        design_headroom_fraction: Fraction of observed capacity usable for design.
        n_plus_one: Whether one spare node is added to required nonzero roles.
        approved_benchmark_fingerprints: Optional reproducibility-approved IDs/fingerprints.

    Returns:
        Hardware matrix with policy, qualified/rejected evidence and profile results.

    Raises:
        ValueError: If policy values, benchmark evidence or deployment demands are invalid.
    """
    if not 0 < design_headroom_fraction <= 1:
        raise ValueError("design_headroom_fraction must be in (0, 1]")
    if min_repeats < 1:
        raise ValueError("min_repeats must be >= 1")

    qa_excluded = []
    qa_fingerprint_mismatches = []
    if approved_benchmark_fingerprints is not None:
        approved_results = []
        for result in results:
            benchmark_id = str(result.get("benchmark_id"))
            expected = approved_benchmark_fingerprints.get(benchmark_id)
            if expected is None:
                qa_excluded.append(benchmark_id)
                continue
            actual = result_fingerprint(result)
            if actual != expected:
                qa_fingerprint_mismatches.append(benchmark_id)
                continue
            approved_results.append(result)
        results = approved_results

    evidence = [
        item
        for result in results
        if (
            item := extract_evidence(
                result,
                min_duration_seconds=min_duration_seconds,
                min_warmup_seconds=min_warmup_seconds,
                max_failure_rate=max_failure_rate,
                max_cpu_p95_pct=max_cpu_p95_pct,
                max_ram_p95_pct=max_ram_p95_pct,
            )
        )
        is not None
    ]
    qualified = grouped_qualified_evidence(evidence, min_repeats=min_repeats)

    profiles = []
    required_roles = demand.get(
        "required_roles",
        {
            "control_api": "control_ops_per_second",
            "event_ingest": "events_per_second",
            "media": "media_mbps",
            "recording": "recording_mbps",
            "storage": "storage_write_mbps",
            "ai": "ai_mpix_s",
        },
    )

    for profile in demand.get("profiles", []):
        demands = profile.get("demands", {})
        roles = {}
        for role, dimension in required_roles.items():
            requested = _num(demands.get(dimension))
            evidence_row = qualified.get((role, dimension))
            if requested is None:
                roles[role] = {
                    "status": "UNQUALIFIED",
                    "dimension": dimension,
                    "reason": "deployment demand not provided",
                }
                continue
            if requested < 0:
                raise ValueError(f"{profile.get('name')}: negative demand for {dimension}")
            if requested == 0:
                roles[role] = {
                    "status": "NOT_REQUIRED",
                    "dimension": dimension,
                    "demand": 0.0,
                    "nodes_required": 0,
                }
                continue
            if evidence_row is None:
                duplicate_reason = _role_duplicate_reason(
                    evidence,
                    role=role,
                    dimension=dimension,
                )
                roles[role] = {
                    "status": "UNQUALIFIED",
                    "dimension": dimension,
                    "demand": requested,
                    "reason": duplicate_reason
                    or f"no evidence with >= {min_repeats} qualified repeats",
                }
                continue

            observed = float(evidence_row["observed_capacity_min"])
            safe_capacity = observed * design_headroom_fraction
            base_nodes = math.ceil(requested / safe_capacity)
            total_nodes = base_nodes + (1 if n_plus_one and base_nodes > 0 else 0)
            roles[role] = {
                "status": "QUALIFIED_FROM_MEASURED_EVIDENCE",
                "dimension": dimension,
                "demand": requested,
                "observed_capacity_per_node_min": observed,
                "safe_capacity_per_node": safe_capacity,
                "design_headroom_fraction": design_headroom_fraction,
                "base_nodes": base_nodes,
                "n_plus_one": bool(n_plus_one),
                "nodes_required": total_nodes,
                "evidence": evidence_row,
            }

        profiles.append(
            {
                "name": profile.get("name"),
                "cameras": profile.get("cameras"),
                "roles": roles,
            }
        )

    rejected = [
        {
            "benchmark_id": item.benchmark_id,
            "role": item.role,
            "dimension": item.dimension,
            "commit_sha": item.commit_sha,
            "hardware_key": item.hardware_key,
            "reasons": list(item.reasons),
        }
        for item in evidence
        if not item.qualified
    ]

    return {
        "matrix_version": MATRIX_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy": {
            "min_repeats": min_repeats,
            "min_duration_seconds": min_duration_seconds,
            "min_warmup_seconds": min_warmup_seconds,
            "max_failure_rate": max_failure_rate,
            "max_cpu_p95_pct": max_cpu_p95_pct,
            "max_ram_p95_pct": max_ram_p95_pct,
            "design_headroom_fraction": design_headroom_fraction,
            "n_plus_one": n_plus_one,
        },
        "qualified_evidence": list(qualified.values()),
        "rejected_evidence": rejected,
        "qa_filter_applied": approved_benchmark_fingerprints is not None,
        "qa_excluded_benchmark_ids": qa_excluded,
        "qa_fingerprint_mismatch_benchmark_ids": qa_fingerprint_mismatches,
        "profiles": profiles,
    }


def approved_fingerprints_from_reproducibility_report(path: str) -> dict[str, str]:
    """Load benchmark fingerprints from PASS groups in a reproducibility report.

    Args:
        path: Phase-8 reproducibility report JSON path.

    Returns:
        Mapping of approved benchmark IDs to immutable result fingerprints.

    Raises:
        ValueError: If the report version or PASS-group fingerprint evidence is invalid.
        OSError: If the report cannot be read.
        json.JSONDecodeError: If the report is not valid JSON.
    """
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    if report.get("report_version") != "phase8-reproducibility-v1":
        raise ValueError("unsupported reproducibility report")
    approved: dict[str, str] = {}
    for group in report.get("groups", []):
        if group.get("status") != "PASS":
            continue
        fingerprints = group.get("benchmark_fingerprints")
        if not isinstance(fingerprints, dict):
            raise ValueError("PASS reproducibility group is missing benchmark fingerprints")
        for benchmark_id, fingerprint in fingerprints.items():
            if not fingerprint:
                raise ValueError("PASS reproducibility group contains an empty fingerprint")
            approved[str(benchmark_id)] = str(fingerprint)
    return approved


def main():
    """Build and write a hardware matrix from reproducibility-approved evidence."""
    parser = argparse.ArgumentParser(
        description="Build hardware node matrix only from repeated measured Phase 8 evidence"
    )
    parser.add_argument("--results", nargs="+", required=True)
    parser.add_argument("--demand", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--reproducibility-report",
        required=True,
        help="Phase 8 reproducibility QA report; only PASS benchmark IDs are eligible",
    )
    parser.add_argument("--min-repeats", type=int, default=3)
    parser.add_argument("--min-duration-seconds", type=float, default=300)
    parser.add_argument("--min-warmup-seconds", type=float, default=30)
    parser.add_argument("--max-failure-rate", type=float, default=0.001)
    parser.add_argument("--max-cpu-p95-pct", type=float, default=70)
    parser.add_argument("--max-ram-p95-pct", type=float, default=75)
    parser.add_argument("--design-headroom-fraction", type=float, default=0.80)
    parser.add_argument("--no-n-plus-one", action="store_true")
    args = parser.parse_args()

    results = load_results(args.results)
    demand = json.loads(Path(args.demand).read_text(encoding="utf-8"))
    approved_fingerprints = approved_fingerprints_from_reproducibility_report(
        args.reproducibility_report
    )
    matrix = build_matrix(
        results=results,
        demand=demand,
        approved_benchmark_fingerprints=approved_fingerprints,
        min_repeats=args.min_repeats,
        min_duration_seconds=args.min_duration_seconds,
        min_warmup_seconds=args.min_warmup_seconds,
        max_failure_rate=args.max_failure_rate,
        max_cpu_p95_pct=args.max_cpu_p95_pct,
        max_ram_p95_pct=args.max_ram_p95_pct,
        design_headroom_fraction=args.design_headroom_fraction,
        n_plus_one=not args.no_n_plus_one,
    )
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(matrix, indent=2, sort_keys=True), encoding="utf-8")

    qualified_roles = sum(
        1
        for profile in matrix["profiles"]
        for role in profile["roles"].values()
        if role["status"] == "QUALIFIED_FROM_MEASURED_EVIDENCE"
    )
    unqualified_roles = sum(
        1
        for profile in matrix["profiles"]
        for role in profile["roles"].values()
        if role["status"] == "UNQUALIFIED"
    )
    print(
        f"profiles={len(matrix['profiles'])} qualified_roles={qualified_roles} "
        f"unqualified_roles={unqualified_roles} output={target}"
    )


if __name__ == "__main__":
    main()
