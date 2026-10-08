#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from phase8_benchmark_common import (
    Rejection,
    parse_benchmark_record,
    repeat_verdict,
    safe_workload_config,
    workload_identity_json,
    workload_key as shared_workload_key,
)
from phase8_hardware_matrix import capacity_dimension


REPORT_VERSION = "phase8-reproducibility-v1"


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


def canonical_workload(result: dict[str, Any]) -> str:
    """Serialize workload type and allowlisted shaping config.

    The allowlist is WORKLOAD_SHAPING_CONFIG_FIELDS, the same constant the
    repeat fingerprint uses. Free-text config keys are not part of the identity.

    Args:
        result: Validated Phase-8 benchmark result.

    Returns:
        Canonical JSON containing workload type and shaping configuration.
    """
    return workload_identity_json(result)


def workload_key(result: dict[str, Any]) -> str:
    """Build a stable key for equivalent benchmark workload definitions.

    Args:
        result: Validated Phase-8 benchmark result.

    Returns:
        Twenty-character SHA-256-derived workload identity.
    """
    return shared_workload_key(result)



def coefficient_variation(values: list[float]) -> float | None:
    """Calculate population coefficient of variation for repeat measurements.

    Args:
        values: Numeric repeated measurements.

    Returns:
        Population standard deviation divided by absolute mean, zero for stable
        zero/single values, or None when undefined.
    """
    if not values:
        return None
    mean = statistics.fmean(values)
    if mean == 0:
        return 0.0 if all(v == 0 for v in values) else None
    if len(values) == 1:
        return 0.0
    return statistics.pstdev(values) / abs(mean)


def relative_range(values: list[float]) -> float | None:
    """Calculate measurement range relative to absolute mean.

    Args:
        values: Numeric repeated measurements.

    Returns:
        Relative range, zero for identical zero values, or None when undefined.
    """
    if not values:
        return None
    mean = statistics.fmean(values)
    if mean == 0:
        return 0.0 if max(values) == min(values) else None
    return (max(values) - min(values)) / abs(mean)


def percentile_order_valid(result: dict[str, Any]) -> bool:
    """Check that present latency percentiles are monotonically ordered.

    Args:
        result: Phase-8 benchmark result.

    Returns:
        True when present p50, p95, p99 and max values are nondecreasing.
    """
    metrics = result.get("result") if isinstance(result, dict) else None
    latency = metrics.get("latency", {}) if isinstance(metrics, dict) else {}
    if not isinstance(latency, dict):
        return False
    values = [
        _num(latency.get("p50_ms")),
        _num(latency.get("p95_ms")),
        _num(latency.get("p99_ms")),
        _num(latency.get("max_ms")),
    ]
    present = [value for value in values if value is not None]
    return present == sorted(present)


def load_results(paths: list[str]) -> list[dict[str, Any]]:
    """Load benchmark-result JSON files and retain source-file provenance.

    Args:
        paths: JSON files or directories containing benchmark results.

    Returns:
        Loaded JSON values with source-file metadata on objects. Container and
        schema checks happen in parse_benchmark_record, not here, so one bad
        file cannot abort the report.

    Raises:
        OSError: If an input cannot be read.
        json.JSONDecodeError: If an input is not valid JSON.
    """
    results: list[Any] = []
    for raw in paths:
        path = Path(raw)
        candidates = sorted(path.glob("*.json")) if path.is_dir() else [path]
        for candidate in candidates:
            result = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(result, dict):
                result["_source_file"] = str(candidate)
            results.append(result)
    return results


def review_group(
    results: list[Any],
    *,
    min_repeats: int,
    min_duration_seconds: float,
    min_warmup_seconds: float,
    max_failure_rate: float,
    max_capacity_cv: float,
    max_p95_latency_cv: float,
    max_capacity_relative_range: float,
    require_thermal: bool,
) -> dict[str, Any]:
    """Evaluate one commit/hardware/workload repeat group for reproducibility.

    Args:
        results: Parsed benchmark repeats from parse_benchmark_record.
        min_repeats: Minimum required repeat count.
        min_duration_seconds: Minimum duration for every repeat.
        min_warmup_seconds: Minimum warmup for every repeat.
        max_failure_rate: Maximum failure rate across repeats.
        max_capacity_cv: Maximum allowed capacity coefficient of variation.
        max_p95_latency_cv: Maximum allowed p95-latency coefficient of variation.
        max_capacity_relative_range: Maximum allowed capacity relative range.
        require_thermal: Whether thermal sensor evidence is mandatory for all repeats.

    Returns:
        PASS/FAIL group review with fingerprints, reasons, warnings and metrics.
        Duplicate benchmark identities and duplicate allowlisted content
        fingerprints fail the group. repeat_count is the number of unique content
        fingerprints, not the number of submitted labels. The fingerprint omits
        descriptive hardware text, notes, timestamps, and volatile host state, so
        copies that differ only there collide. Those descriptors are compared by
        the shared hardware key instead. Non-finite measurements, including
        integers that overflow float, fail the group. The check defends against
        duplicated or relabeled evidence, not against deliberately fabricated
        measurements.
    """
    reasons: list[str] = []
    warnings: list[str] = []
    sources = [record.source for record in results]
    independent_repeats, verdict_reasons = repeat_verdict(
        [record.benchmark_id for record in results],
        [record.content_fingerprint for record in results],
        min_repeats=min_repeats,
    )
    reasons.extend(verdict_reasons)
    submitted_count = len(results)
    durations = [record.duration_seconds for record in results]
    warmups = [record.warmup_seconds for record in results]
    failures = [record.failure_rate for record in results]

    if durations and min(durations) < min_duration_seconds:
        reasons.append(
            f"minimum duration {min(durations):.1f}s < required {min_duration_seconds:.1f}s"
        )
    if warmups and min(warmups) < min_warmup_seconds:
        reasons.append(
            f"minimum warmup {min(warmups):.1f}s < required {min_warmup_seconds:.1f}s"
        )
    if failures and max(failures) > max_failure_rate:
        reasons.append(
            f"maximum failure_rate {max(failures):.6f} > allowed {max_failure_rate:.6f}"
        )

    bad_percentiles = [
        record.benchmark_id
        for record in results
        if not percentile_order_valid(record.source)
    ]
    if bad_percentiles:
        reasons.append(
            "latency percentile ordering invalid for benchmark IDs: "
            + ", ".join(bad_percentiles)
        )

    capacity_rows = [capacity_dimension(record.source) for record in results]
    capacity_dimensions = {
        row[1]
        for row in capacity_rows
        if row is not None
    }
    capacity_values = [
        float(row[2])
        for row in capacity_rows
        if row is not None
    ]
    capacity_dimension_name = (
        next(iter(capacity_dimensions))
        if len(capacity_dimensions) == 1
        else None
    )
    p95_latencies = [record.p95_ms for record in results if record.p95_ms is not None]

    capacity_cv = coefficient_variation(capacity_values)
    p95_latency_cv = coefficient_variation(p95_latencies)
    capacity_range = relative_range(capacity_values)

    if not capacity_values:
        warnings.append("no hardware-qualification capacity metric is defined for this workload")
    elif len(capacity_values) != submitted_count or len(capacity_dimensions) != 1:
        reasons.append("capacity metric is missing or inconsistent across repeats")
    else:
        if capacity_cv is not None and capacity_cv > max_capacity_cv:
            reasons.append(
                f"capacity CV {capacity_cv:.4f} > allowed {max_capacity_cv:.4f}"
            )
        if (
            capacity_range is not None
            and capacity_range > max_capacity_relative_range
        ):
            reasons.append(
                f"capacity relative range {capacity_range:.4f} > allowed "
                f"{max_capacity_relative_range:.4f}"
            )

    if len(p95_latencies) != submitted_count:
        warnings.append("p95 latency is not available for every repeat")
    elif p95_latency_cv is not None and p95_latency_cv > max_p95_latency_cv:
        reasons.append(
            f"p95 latency CV {p95_latency_cv:.4f} > allowed {max_p95_latency_cv:.4f}"
        )

    thermal_measured = [record.thermal_measured for record in results]
    thermal_limit_exceeded = [
        record.benchmark_id for record in results if record.thermal_limit_exceeded
    ]
    if thermal_limit_exceeded:
        reasons.append(
            "thermal high/critical limit observed for benchmark IDs: "
            + ", ".join(thermal_limit_exceeded)
        )
    if require_thermal and (not thermal_measured or not all(thermal_measured)):
        reasons.append("thermal evidence is required but missing for one or more repeats")
    elif thermal_measured and not all(thermal_measured):
        warnings.append("thermal sensors were unavailable for one or more repeats")
    elif thermal_measured and not any(thermal_measured):
        warnings.append("thermal sensors were unavailable for all repeats")

    freq_ratios = [
        record.cpu_freq_ratio_min
        for record in results
        if record.cpu_freq_ratio_min is not None
    ]
    if freq_ratios and min(freq_ratios) < 0.50:
        warnings.append(
            f"minimum observed CPU frequency/max ratio was {min(freq_ratios):.3f}; "
            "review host power/thermal policy for throttling or DVFS effects"
        )

    commit_sha = results[0].commit_sha if results else ""
    hw_key = results[0].hardware_key if results else ""
    workload_hash = results[0].workload_key if results else ""
    benchmark_ids = [record.benchmark_id for record in results]
    benchmark_fingerprints = {
        record.benchmark_id: record.result_fingerprint for record in results
    }

    return {
        "status": "PASS" if not reasons else "FAIL",
        "commit_sha": commit_sha,
        "hardware_key": hw_key,
        "workload_key": workload_hash,
        "workload": (
            {
                "type": sources[0]["workload"]["type"],
                "config": safe_workload_config(sources[0]),
            }
            if sources
            else {}
        ),
        "repeat_count": independent_repeats,
        "benchmark_ids": benchmark_ids,
        "benchmark_fingerprints": benchmark_fingerprints,
        "source_files": [source.get("_source_file") for source in sources],
        "reasons": reasons,
        "warnings": warnings,
        "metrics": {
            "duration_seconds_min": min(durations) if durations else None,
            "warmup_seconds_min": min(warmups) if warmups else None,
            "failure_rate_max": max(failures) if failures else None,
            "capacity": {
                "dimension": capacity_dimension_name,
                "values": capacity_values,
                "mean": statistics.fmean(capacity_values) if capacity_values else None,
                "cv": capacity_cv,
                "relative_range": capacity_range,
            },
            "p95_latency_ms": {
                "values": p95_latencies,
                "mean": statistics.fmean(p95_latencies) if p95_latencies else None,
                "cv": p95_latency_cv,
            },
            "thermal_measured_all": bool(thermal_measured and all(thermal_measured)),
            "thermal_limit_exceeded": bool(thermal_limit_exceeded),
            "cpu_freq_ratio_min": min(freq_ratios) if freq_ratios else None,
        },
    }


def _rejection_group(result: Any, rejection: Rejection) -> dict[str, Any]:
    """Build one failed group for a record the parser rejected.

    Args:
        result: Original benchmark record, used only for provenance. Non-objects
            and deep config are not copied into the group.
        rejection: Structured parse rejection.

    Returns:
        FAIL group with repeat_count zero and the parser reason.
    """
    if not isinstance(result, dict):
        result = {}
    workload = result.get("workload") if isinstance(result.get("workload"), dict) else {}
    environment = result.get("environment") if isinstance(result.get("environment"), dict) else {}
    return {
        "status": "FAIL",
        "commit_sha": str(environment.get("commit_sha") or ""),
        "hardware_key": "",
        "workload_key": "",
        "workload": {
            "type": workload.get("type"),
            "config": safe_workload_config(result),
        },
        "repeat_count": 0,
        "benchmark_ids": [rejection.benchmark_id],
        "benchmark_fingerprints": {},
        "source_files": [result.get("_source_file")],
        "reasons": [rejection.reason],
        "warnings": [],
        "metrics": {
            "duration_seconds_min": None,
            "warmup_seconds_min": None,
            "failure_rate_max": None,
            "capacity": {
                "dimension": None,
                "values": [],
                "mean": None,
                "cv": None,
                "relative_range": None,
            },
            "p95_latency_ms": {"values": [], "mean": None, "cv": None},
            "thermal_measured_all": False,
            "thermal_limit_exceeded": False,
            "cpu_freq_ratio_min": None,
        },
    }


def build_report(
    *,
    results: list[dict[str, Any]],
    min_repeats: int = 3,
    min_duration_seconds: float = 300.0,
    min_warmup_seconds: float = 30.0,
    max_failure_rate: float = 0.001,
    max_capacity_cv: float = 0.10,
    max_p95_latency_cv: float = 0.15,
    max_capacity_relative_range: float = 0.20,
    require_thermal: bool = False,
) -> dict[str, Any]:
    """Build reproducibility QA report grouped by commit, hardware and workload.

    Args:
        results: Phase-8 benchmark results.
        min_repeats: Minimum repeat count per group.
        min_duration_seconds: Minimum measured duration.
        min_warmup_seconds: Minimum warmup duration.
        max_failure_rate: Maximum allowed failure rate.
        max_capacity_cv: Maximum allowed capacity coefficient of variation.
        max_p95_latency_cv: Maximum allowed p95 latency coefficient of variation.
        max_capacity_relative_range: Maximum allowed capacity relative range.
        require_thermal: Whether every repeat must include thermal evidence.

    Returns:
        Versioned reproducibility report with policy, summary and reviewed groups.
        Uniqueness of allowlisted measured content is enforced here, not only by
        a later CLI handoff. Duplicate or relabeled evidence fails closed.
        Malformed numeric or hardware content is a failed group, not an exception.
        Fabricated measurements with distinct values are outside this gate.

    Raises:
        ValueError: If min_repeats is below one. Record content does not raise.
    """
    if min_repeats < 1:
        raise ValueError("min_repeats must be >= 1")

    groups: dict[tuple[str, str, str], list[Any]] = {}
    rejections: list[tuple[dict[str, Any], Rejection]] = []
    for result in results:
        parsed = parse_benchmark_record(result)
        if isinstance(parsed, Rejection):
            rejections.append((result, parsed))
            continue
        groups.setdefault(
            (parsed.commit_sha, parsed.hardware_key, parsed.workload_key),
            [],
        ).append(parsed)

    reviewed = [_rejection_group(result, rejection) for result, rejection in rejections]
    reviewed.extend(
        review_group(
            group,
            min_repeats=min_repeats,
            min_duration_seconds=min_duration_seconds,
            min_warmup_seconds=min_warmup_seconds,
            max_failure_rate=max_failure_rate,
            max_capacity_cv=max_capacity_cv,
            max_p95_latency_cv=max_p95_latency_cv,
            max_capacity_relative_range=max_capacity_relative_range,
            require_thermal=require_thermal,
        )
        for _, group in sorted(groups.items())
    )

    passed = sum(1 for row in reviewed if row["status"] == "PASS")
    failed = len(reviewed) - passed

    return {
        "report_version": REPORT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy": {
            "min_repeats": min_repeats,
            "min_duration_seconds": min_duration_seconds,
            "min_warmup_seconds": min_warmup_seconds,
            "max_failure_rate": max_failure_rate,
            "max_capacity_cv": max_capacity_cv,
            "max_p95_latency_cv": max_p95_latency_cv,
            "max_capacity_relative_range": max_capacity_relative_range,
            "require_thermal": require_thermal,
        },
        "summary": {
            "groups": len(reviewed),
            "passed": passed,
            "failed": failed,
        },
        "groups": reviewed,
    }


def _fail_closed_report(reason: str) -> dict[str, Any]:
    """Build a minimal FAIL report that is always safe to serialize.

    Args:
        reason: Controlled explanation of the load or serialization failure.

    Returns:
        One failed group with an empty config and no measured repeats.
    """
    return {
        "report_version": REPORT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy": {},
        "summary": {"groups": 1, "passed": 0, "failed": 1},
        "groups": [
            {
                "status": "FAIL",
                "commit_sha": "",
                "hardware_key": "",
                "workload_key": "",
                "workload": {"type": None, "config": {}},
                "repeat_count": 0,
                "benchmark_ids": [],
                "benchmark_fingerprints": {},
                "source_files": [],
                "reasons": [reason],
                "warnings": [],
                "metrics": {
                    "duration_seconds_min": None,
                    "warmup_seconds_min": None,
                    "failure_rate_max": None,
                    "capacity": {
                        "dimension": None,
                        "values": [],
                        "mean": None,
                        "cv": None,
                        "relative_range": None,
                    },
                    "p95_latency_ms": {"values": [], "mean": None, "cv": None},
                    "thermal_measured_all": False,
                    "thermal_limit_exceeded": False,
                    "cpu_freq_ratio_min": None,
                },
            }
        ],
    }


def _serialize_report(report: dict[str, Any]) -> str:
    try:
        return json.dumps(report, indent=2, sort_keys=True)
    except RecursionError:
        return json.dumps(
            _fail_closed_report("report nesting exceeds the serialization limit"),
            indent=2,
            sort_keys=True,
        )


def main() -> int:
    """Validate Phase-8 repeatability evidence and write the QA report.

    Returns:
        Zero on normal completion, or one when configured to fail on rejected groups.
    """
    parser = argparse.ArgumentParser(
        description="Validate repeatability and thermal/percentile evidence for Phase 8 benchmark runs"
    )
    parser.add_argument("--results", nargs="+", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--min-repeats", type=int, default=3)
    parser.add_argument("--min-duration-seconds", type=float, default=300)
    parser.add_argument("--min-warmup-seconds", type=float, default=30)
    parser.add_argument("--max-failure-rate", type=float, default=0.001)
    parser.add_argument("--max-capacity-cv", type=float, default=0.10)
    parser.add_argument("--max-p95-latency-cv", type=float, default=0.15)
    parser.add_argument("--max-capacity-relative-range", type=float, default=0.20)
    parser.add_argument("--require-thermal", action="store_true")
    parser.add_argument(
        "--fail-on-rejected-groups",
        action="store_true",
        help="exit non-zero when any workload group fails QA",
    )
    args = parser.parse_args()
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        report = build_report(
            results=load_results(args.results),
            min_repeats=args.min_repeats,
            min_duration_seconds=args.min_duration_seconds,
            min_warmup_seconds=args.min_warmup_seconds,
            max_failure_rate=args.max_failure_rate,
            max_capacity_cv=args.max_capacity_cv,
            max_p95_latency_cv=args.max_p95_latency_cv,
            max_capacity_relative_range=args.max_capacity_relative_range,
            require_thermal=args.require_thermal,
        )
    except RecursionError:
        report = _fail_closed_report("benchmark record nesting exceeds the serialization limit")
    except (OSError, json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError) as exc:
        report = _fail_closed_report(str(exc) or exc.__class__.__name__)
    target.write_text(_serialize_report(report), encoding="utf-8")

    print(
        f"groups={report['summary']['groups']} passed={report['summary']['passed']} "
        f"failed={report['summary']['failed']} output={target}"
    )
    for group in report["groups"]:
        print(
            f"{group['status']} workload={group['workload'].get('type')} "
            f"repeats={group['repeat_count']} hardware={group['hardware_key']} "
            f"reasons={'; '.join(group['reasons']) or '-'} "
            f"warnings={'; '.join(group['warnings']) or '-'}"
        )

    if args.fail_on_rejected_groups and report["summary"]["failed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
