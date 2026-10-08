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

from phase8_benchmark_common import (
    Rejection,
    parse_benchmark_record,
    repeat_verdict,
    result_fingerprint,
)
from phase8_benchmark_common import hardware_key as shared_hardware_key


MATRIX_VERSION = "phase8-hardware-matrix-v1"

# Role, dimension, and result key for workloads the matrix can qualify.
# Capacity must be a positive finite number. Reconnect has no entry.
_WORKLOAD_CAPACITY = {
    "event-ingest-http": ("event_ingest", "events_per_second", "throughput_ops_s"),
    "recording-directory-growth": ("recording", "recording_mbps", "observed_recording_mbps"),
    "synthetic-storage-write": ("storage", "storage_write_mbps", "aggregate_write_mbps"),
    "media-relay": ("media", "media_mbps", "observed_media_mbps"),
    "ai-inference": ("ai", "ai_mpix_s", "observed_ai_mpix_s"),
    "control-api": ("control_api", "control_ops_per_second", "throughput_ops_s"),
}


@dataclass(frozen=True)
class Evidence:
    """Qualified-capacity evidence extracted from one benchmark result.

    Attributes:
        benchmark_id: Source benchmark identifier.
        content_fingerprint: Allowlisted measurement fingerprint. Descriptive
            hardware text, notes, timestamps, and volatile host state are omitted.
        commit_sha: Benchmarked source revision.
        hardware_key: Stable hardware/environment identity.
        hardware: Captured hardware metadata.
        role: Capacity role represented by the workload.
        dimension: Measured capacity dimension.
        observed_capacity: Observed workload capacity.
        duration_seconds: Measured workload duration.
        warmup_seconds: Warmup duration.
        failure_rate: Observed operation failure rate, or None when that field
            is absent, empty, or JSON null. Qualified rows always carry a number.
        cpu_p95_pct: CPU p95 utilization when measured.
        ram_p95_pct: RAM p95 utilization when measured.
        qualified: Whether policy thresholds are satisfied.
        reasons: Qualification rejection reasons.
        workload_key: Allowlisted workload identity shared with the reproducibility gate.
    """

    benchmark_id: str
    content_fingerprint: str
    commit_sha: str
    hardware_key: str
    hardware: dict[str, Any]
    role: str
    dimension: str
    observed_capacity: float
    duration_seconds: float
    warmup_seconds: float
    failure_rate: float | None
    cpu_p95_pct: float | None
    ram_p95_pct: float | None
    qualified: bool
    reasons: tuple[str, ...]
    workload_key: str = ""


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


def _ensure_path_reason(reasons: list[str], path: str) -> None:
    """Add a missing-field reason when no reason already names the path.

    Args:
        reasons: Qualification reasons collected for one record.
        path: Required measured path that was not parsed as a number.

    Returns:
        None. The list gains MISSING_MEASURED_FIELD:<path> only when that path
        is not already named by a null or missing-field reason.
    """
    if any(path in reason for reason in reasons):
        return
    reasons.append(f"MISSING_MEASURED_FIELD:{path}")


def _duplicate_repeat_reason(items: list[Evidence]) -> str | None:
    """Reject copies that reuse one benchmark identity or measured-content fingerprint.

    Args:
        items: Evidence rows already grouped by role, dimension, commit and hardware.

    Returns:
        Rejection reason when an identity or content fingerprint repeats, otherwise None.
    """
    _repeat_count, verdict = repeat_verdict(
        [item.benchmark_id for item in items],
        [item.content_fingerprint for item in items],
        min_repeats=1,
    )
    reasons = [reason for reason in verdict if reason.startswith("duplicate ")]
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
        groups.setdefault((item.commit_sha, item.hardware_key, item.workload_key), []).append(item)
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

    build_report and build_matrix both use this key. Descriptive text is
    canonicalized, including OS name and version, and NIC and GPU inventories
    are sorted. Letter case, repeated spaces, Unicode format characters, dotted
    or dotless I, and inventory rotation therefore stay on one machine.

    Args:
        result: Validated Phase-8 benchmark result.

    Returns:
        Twenty-character SHA-256-derived hardware identity.

    Raises:
        ValueError: If a hardware number is not finite or a GPU index is missing
            or duplicated.
    """
    return shared_hardware_key(result)


def capacity_dimension(result: dict[str, Any]) -> tuple[str, str, float] | None:
    """Map a supported workload result to role, dimension and measured capacity.

    Args:
        result: Validated Phase-8 benchmark result.

    Returns:
        Tuple of role, capacity-dimension name and positive observed capacity,
        or None when the workload has no qualification mapping.
    """
    workload = result.get("workload") if isinstance(result, dict) else None
    if not isinstance(workload, dict) or not isinstance(workload.get("type"), str):
        return None
    workload_type = workload["type"]
    metrics = result.get("result") if isinstance(result.get("result"), dict) else {}
    spec = _WORKLOAD_CAPACITY.get(workload_type)
    if spec is None:
        return None
    role, dimension, key = spec
    capacity = _num(metrics.get(key))
    return (role, dimension, capacity) if capacity and capacity > 0 else None


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
        Evidence object with qualification reasons. Malformed content is an
        unqualified evidence row, not an exception. None means the workload
        has no capacity mapping and is not an incomplete mapped workload. A
        mapped workload with a missing required field is unqualified and names
        that field.
    """
    parsed = parse_benchmark_record(result)
    if isinstance(parsed, Rejection):
        return Evidence(
            benchmark_id=parsed.benchmark_id,
            content_fingerprint="",
            commit_sha="",
            hardware_key="",
            workload_key="",
            hardware={},
            role="",
            dimension="",
            observed_capacity=0.0,
            duration_seconds=0.0,
            warmup_seconds=0.0,
            failure_rate=0.0,
            cpu_p95_pct=None,
            ram_p95_pct=None,
            qualified=False,
            reasons=(parsed.reason,),
        )
    result = parsed.source
    mapping = capacity_dimension(result)
    failure_rate = parsed.failure_rate
    duration = parsed.duration_seconds
    warmup = parsed.warmup_seconds
    cpu_p95 = parsed.cpu_p95_pct
    ram_p95 = parsed.ram_p95_pct
    reasons = list(parsed.null_measured_reasons)
    if mapping is None:
        workload = result.get("workload") if isinstance(result.get("workload"), dict) else {}
        workload_type = workload.get("type") if isinstance(workload.get("type"), str) else ""
        spec = _WORKLOAD_CAPACITY.get(workload_type)
        if spec is None or not reasons:
            return None
        role, dimension, _capacity_key = spec
        capacity = 0.0
    else:
        role, dimension, capacity = mapping
    if duration < min_duration_seconds:
        reasons.append(f"duration {duration:.1f}s < required {min_duration_seconds:.1f}s")
    if warmup < min_warmup_seconds:
        reasons.append(f"warmup {warmup:.1f}s < required {min_warmup_seconds:.1f}s")
    if failure_rate is not None and failure_rate > max_failure_rate:
        reasons.append(
            f"failure_rate {failure_rate:.6f} > allowed {max_failure_rate:.6f}"
        )
    if cpu_p95 is None:
        _ensure_path_reason(reasons, "resources.cpu_pct.p95")
    elif cpu_p95 > max_cpu_p95_pct:
        reasons.append(f"CPU p95 {cpu_p95:.1f}% > allowed {max_cpu_p95_pct:.1f}%")
    if ram_p95 is None:
        _ensure_path_reason(reasons, "resources.ram_pct.p95")
    elif ram_p95 > max_ram_p95_pct:
        reasons.append(f"RAM p95 {ram_p95:.1f}% > allowed {max_ram_p95_pct:.1f}%")

    env = result.get("environment") if isinstance(result.get("environment"), dict) else {}
    hardware = env.get("hardware") if isinstance(env.get("hardware"), dict) else {}
    return Evidence(
        benchmark_id=parsed.benchmark_id,
        content_fingerprint=parsed.content_fingerprint,
        commit_sha=parsed.commit_sha,
        hardware_key=parsed.hardware_key,
        workload_key=parsed.workload_key,
        hardware=dict(hardware),
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
        Loaded JSON values. Container and schema checks happen in
        parse_benchmark_record so one bad file cannot abort the matrix.

    Raises:
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
            if isinstance(payload, dict):
                payload["_source_file"] = str(candidate)
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
        Groups that reuse a benchmark identity or allowlisted content fingerprint
        are omitted. repeat_count is the number of unique content fingerprints.
        Copies that differ only by descriptive hardware text, labels, notes,
        timestamps, number spelling, or volatile host state collide and are not
        qualified. Byte-identical genuine aggregates fail closed. Distinct
        fabricated numbers are outside this gate.
    """
    groups: dict[tuple[str, str, str, str, str], list[Evidence]] = {}
    for item in evidence:
        if not item.qualified:
            continue
        key = (item.role, item.dimension, item.commit_sha, item.hardware_key, item.workload_key)
        groups.setdefault(key, []).append(item)

    best: dict[tuple[str, str], dict[str, Any]] = {}
    for (role, dimension, commit_sha, hw_key, _workload_key), items in groups.items():
        repeat_count, verdict = repeat_verdict(
            [item.benchmark_id for item in items],
            [item.content_fingerprint for item in items],
            min_repeats=min_repeats,
        )
        if any(reason.startswith("duplicate ") for reason in verdict):
            continue
        independent_repeats = repeat_count
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

    Duplicate benchmark identities and duplicate allowlisted content fingerprints
    are not independent repeats and cannot produce QUALIFIED_FROM_MEASURED_EVIDENCE.
    That uniqueness is enforced in this function, including when no reproducibility
    report is supplied. repeat_count is the number of unique content fingerprints.
    The fingerprint omits descriptive hardware text, notes, timestamps, and
    volatile host state such as storage_free_bytes. That text is part of the
    shared hardware key instead. Byte-identical aggregated measurements fail
    closed. This gate does not defend against deliberately fabricated measurements.

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
        ValueError: If policy values or deployment demands are invalid. Malformed
            benchmark content is reported as unqualified evidence.
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
            if not isinstance(result, dict):
                qa_excluded.append("")
                continue
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


def _fail_closed_matrix(reason: str) -> dict[str, Any]:
    """Build a minimal unqualified matrix that is always safe to serialize.

    Args:
        reason: Controlled explanation of the load or serialization failure.

    Returns:
        Matrix with no qualified evidence.
    """
    return {
        "report_version": "phase8-hardware-matrix-v1",
        "qualified_evidence": [],
        "rejected_evidence": [{"reason": reason}],
        "profiles": [],
    }


def _serialize_matrix(matrix: dict[str, Any]) -> str:
    try:
        return json.dumps(matrix, indent=2, sort_keys=True)
    except RecursionError:
        return json.dumps(
            _fail_closed_matrix("report nesting exceeds the serialization limit"),
            indent=2,
            sort_keys=True,
        )


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
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
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
    except RecursionError:
        matrix = _fail_closed_matrix("benchmark record nesting exceeds the serialization limit")
    except (OSError, json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError) as exc:
        matrix = _fail_closed_matrix(str(exc) or exc.__class__.__name__)
    target.write_text(_serialize_matrix(matrix), encoding="utf-8")

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
