#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

try:
    import psutil
except ImportError:  # pragma: no cover - benchmark hosts install psutil
    psutil = None


SCHEMA_VERSION = "phase8-benchmark-v1"


def result_fingerprint(result: dict[str, Any]) -> str:
    """Build a stable SHA-256 fingerprint for benchmark evidence.

    Args:
        result: Benchmark result dictionary.

    Returns:
        Hexadecimal fingerprint excluding internal underscore-prefixed fields.
    """
    payload = {
        key: value
        for key, value in result.items()
        if not str(key).startswith("_")
    }
    material = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


_NON_FINITE_MEASUREMENT = "measured value is not a finite number"

# Explicit measured-result fields. Anything absent from these tuples is a label,
# note, timestamp, or volatile host fact and must not affect independence.
_RESULT_MEASURED_KEYS = (
    "operations_ok",
    "operations_failed",
    "failure_rate",
    "throughput_ops_s",
    "observed_recording_mbps",
    "aggregate_write_mbps",
    "aggregate_write_MBps",
    "observed_media_mbps",
    "observed_ai_mpix_s",
    "bytes_written",
    "bytes_growth",
    "new_files",
)
_LATENCY_KEYS = ("count", "p50_ms", "p95_ms", "p99_ms", "max_ms", "mean_ms")
_RESOURCE_SUMMARY_KEYS = (
    "cpu_pct",
    "cpu_freq_mhz",
    "cpu_freq_max_mhz",
    "max_temperature_c",
    "ram_used_bytes",
    "ram_pct",
    "net_rx_mbps",
    "net_tx_mbps",
    "disk_read_mbps",
    "disk_write_mbps",
)
_GPU_USAGE_KEYS = ("utilization_pct", "memory_used_mib", "temperature_c")
_SUMMARY_KEYS = ("mean", "p95", "max")


def _canonical_number(value: Any) -> str:
    """Render one measured number as a finite float with a fixed repr.

    Args:
        value: Boolean, integer, or float measurement. Booleans become 0 or 1.

    Returns:
        Fixed-precision decimal text. Negative zero is rendered as 0.

    Raises:
        ValueError: If the value is not a finite number. OverflowError from an
            integer that cannot be represented as a float uses this same path.
    """
    if isinstance(value, bool):
        value = 1 if value else 0
    if not isinstance(value, (int, float)):
        raise ValueError(_NON_FINITE_MEASUREMENT)
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(_NON_FINITE_MEASUREMENT) from exc
    if not math.isfinite(number):
        raise ValueError(_NON_FINITE_MEASUREMENT)
    if number == 0.0:
        number = 0.0
    return format(number, ".17g")


def canonical_descriptor(value: Any) -> str:
    """Canonicalize descriptive text used to group hardware and environment.

    build_report and build_matrix both group runs with this function. The steps
    are Unicode NFKC, removal of every category-Cf format character (this covers
    ZWSP, ZWNJ, ZWJ, the BOM, and the soft hyphen), mapping every Unicode space
    to one space, trimming, and casefold. Dotted I (U+0130) and dotless I
    (U+0131) are accepted as the same key as ASCII I.

    Args:
        value: Descriptive text such as an OS name, CPU model, or device name.

    Returns:
        Canonical descriptor text.
    """
    text = unicodedata.normalize("NFKC", str(value))
    text = "".join(character for character in text if unicodedata.category(character) != "Cf")
    text = "".join(" " if character.isspace() else character for character in text)
    text = " ".join(text.split()).casefold()
    return text.replace("\u0307", "").replace("\u0131", "i")


# Deep enough for real benchmark documents, and far below the depth where
# json.dumps raises RecursionError. 950 levels still serialize; 2000 does not.
MAX_STRUCTURE_DEPTH = 32


def _structure_deeper_than(value: Any, limit: int) -> bool:
    """Return whether a JSON-like tree is deeper than limit.

    The walk is iterative so a 2000-level document cannot raise RecursionError
    while it is being rejected.

    Args:
        value: JSON-like object, list, or scalar.
        limit: Maximum allowed nesting depth. The outermost container is depth 1.

    Returns:
        True when any nested container exceeds the limit.
    """
    pending = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        if isinstance(current, dict):
            if depth > limit:
                return True
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            if depth > limit:
                return True
            pending.extend((item, depth + 1) for item in current)
    return False


def _case_preserving_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value))
    text = "".join(character for character in text if unicodedata.category(character) != "Cf")
    text = "".join(" " if character.isspace() else character for character in text)
    return " ".join(text.split())


def _canonical_api(value: Any) -> str:
    """Keep HTTP path case and casefold only the DNS host.

    Args:
        value: API URL or path from workload config.

    Returns:
        Identity text. Host case does not split a workload. Path case does.
    """
    raw = str(value).strip()
    parts = urlsplit(raw)
    if not parts.scheme and not parts.netloc:
        return _case_preserving_text(raw)
    host = (parts.hostname or "").casefold()
    port = f":{parts.port}" if parts.port is not None else ""
    auth = ""
    if parts.username is not None:
        auth = parts.username
        if parts.password is not None:
            auth += f":{parts.password}"
        auth += "@"
    path = _case_preserving_text(parts.path)
    return urlunsplit((parts.scheme.casefold(), f"{auth}{host}{port}", path, parts.query, parts.fragment))


def safe_workload_config(result: Any) -> dict[str, Any]:
    """Return workload config that is safe to store in a QA report.

    A non-object config, and any config deeper than MAX_STRUCTURE_DEPTH, is
    replaced with an empty object so report serialization cannot recurse into
    the rejected document.

    Args:
        result: Candidate benchmark record.

    Returns:
        Shallow copy of a shallow config, or an empty object.
    """
    if not isinstance(result, dict):
        return {}
    workload = result.get("workload")
    if not isinstance(workload, dict):
        return {}
    config = workload.get("config", {})
    if not isinstance(config, dict) or _structure_deeper_than(config, MAX_STRUCTURE_DEPTH):
        return {}
    return dict(config)


def _summary_numbers(block: Any) -> dict[str, str] | None:
    if not isinstance(block, dict):
        return None
    summary = {
        key: _canonical_number(block[key])
        for key in _SUMMARY_KEYS
        if key in block and block[key] is not None
    }
    return summary or None


def _sorted_objects(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
    )


def _put_number(target: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        target[key] = _canonical_number(value)


# Workload-shaping fields written by the phase-8 drivers. Free-text labels,
# notes, operator names, and timestamps are intentionally absent.
_SHAPING_TEXT_FIELDS = frozenset({"api", "path", "host"})
_SHAPING_FLAG_FIELDS = frozenset({"fsync_each_chunk"})
_SHAPING_NUMBER_FIELDS = {
    "cameras": "count",
    "events": "count",
    "concurrency": "count",
    "timeout_seconds": "duration",
    "expected_streams": "count",
    "expected_bitrate_kbps_per_stream": "rate",
    "duration_requested_seconds": "duration",
    "streams": "count",
    "mib_per_stream": "count",
    "chunk_mib": "count",
    "attempts": "count",
    "port": "port",
}
WORKLOAD_SHAPING_CONFIG_FIELDS = frozenset(
    _SHAPING_TEXT_FIELDS.union(_SHAPING_FLAG_FIELDS).union(_SHAPING_NUMBER_FIELDS)
)
_NUMBER_BOUNDS = {
    "duration": (0.0, 1_000_000.0),
    "unit_interval": (0.0, 1.0),
    "throughput": (0.0, 1e12),
    "latency_ms": (0.0, 1e7),
    "count": (0.0, 1e15),
    "bytes": (0.0, 1e18),
    "percent": (0.0, 100.0),
    "ratio": (0.0, 10.0),
    "frequency": (0.0, 1e7),
    "rate": (0.0, 1e9),
    "cores": (1.0, 4096.0),
    "ram_bytes": (1.0, 1e18),
    "storage_bytes": (0.0, 1e21),
    "mtu": (0.0, 1_000_000.0),
    "gpu_index": (0.0, 256.0),
    "gpu_memory": (0.0, 1e8),
    "temperature": (0.0, 200.0),
    "port": (0.0, 65535.0),
    "samples": (0.0, 1e9),
}
_RESULT_FIELD_BOUNDS = {
    "operations_ok": "count",
    "operations_failed": "count",
    "failure_rate": "unit_interval",
    "throughput_ops_s": "throughput",
    "observed_recording_mbps": "throughput",
    "aggregate_write_mbps": "throughput",
    "aggregate_write_MBps": "throughput",
    "observed_media_mbps": "throughput",
    "observed_ai_mpix_s": "throughput",
    "bytes_written": "bytes",
    "bytes_growth": "bytes",
    "new_files": "count",
}
_LATENCY_FIELD_BOUNDS = {
    "count": "count",
    "p50_ms": "latency_ms",
    "p95_ms": "latency_ms",
    "p99_ms": "latency_ms",
    "max_ms": "latency_ms",
    "mean_ms": "latency_ms",
}
_SUMMARY_FIELD_BOUNDS = {
    "cpu_pct": "percent",
    "ram_pct": "percent",
    "cpu_freq_mhz": "frequency",
    "cpu_freq_max_mhz": "frequency",
    "max_temperature_c": "temperature",
    "ram_used_bytes": "bytes",
    "net_rx_mbps": "rate",
    "net_tx_mbps": "rate",
    "disk_read_mbps": "rate",
    "disk_write_mbps": "rate",
}
_GPU_SUMMARY_BOUNDS = {
    "utilization_pct": "percent",
    "memory_used_mib": "gpu_memory",
    "temperature_c": "temperature",
}


def _checked_number(value: Any, bounds: tuple[float, float], field: str) -> float:
    """Convert one numeric field. Bools follow the documented 0/1 mapping.

    Args:
        value: Raw measurement or hardware number.
        bounds: Inclusive minimum and maximum.
        field: Dotted field name used in the rejection reason.

    Returns:
        Finite float inside bounds. Negative zero is returned as zero.

    Raises:
        ValueError: If the value is missing, non-numeric, non-finite, out of
            range, or an integer too large to convert. OverflowError and
            TypeError are caught here and are not propagated.
    """
    if isinstance(value, bool):
        value = 1 if value else 0
    if value is None or isinstance(value, str) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} is not a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError, TypeError) as exc:
        raise ValueError(f"{field} is not a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} is not a finite number")
    low, high = bounds
    if number < low or number > high:
        raise ValueError(f"{field} is out of range")
    if number == 0.0:
        number = 0.0
    return number


def _present_number(container: Any, key: str, kind: str, field: str) -> float | None:
    if not isinstance(container, dict) or key not in container:
        return None
    return _checked_number(container[key], _NUMBER_BOUNDS[kind], field)


def _check_summary(block: Any, kind: str, field: str) -> None:
    if block is None:
        return
    if not isinstance(block, dict):
        raise ValueError(f"{field} is not a finite number")
    for key in _SUMMARY_KEYS:
        if key in block:
            _checked_number(block[key], _NUMBER_BOUNDS[kind], f"{field}.{key}")


def workload_config_identity(config: Any) -> dict[str, Any]:
    """Return the allowlisted shaping config shared by the fingerprint and workload key.

    Unknown keys, notes, labels, operator names, and timestamps of any casing
    are ignored. Copies that differ only in those keys are the same workload.
    path keeps its case, and api keeps the case of its HTTP path. host and the
    host component of api are casefolded because DNS is case-insensitive.

    Args:
        config: Workload config object, or None.

    Returns:
        Canonical shaping fields. Numbers use the fixed float repr.

    Raises:
        ValueError: If config is not an object or a shaping field is not a
            finite in-range number.
    """
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise ValueError("workload.config is not an object")
    identity: dict[str, Any] = {}
    for key in sorted(WORKLOAD_SHAPING_CONFIG_FIELDS):
        if key not in config or config[key] is None:
            continue
        value = config[key]
        field = f"workload.config.{key}"
        if key in _SHAPING_TEXT_FIELDS:
            if key == "path":
                identity[key] = _case_preserving_text(value)
            elif key == "api":
                identity[key] = _canonical_api(value)
            else:
                identity[key] = canonical_descriptor(value)
            continue
        if key in _SHAPING_FLAG_FIELDS:
            _checked_number(value, _NUMBER_BOUNDS["unit_interval"], field)
        else:
            _checked_number(value, _NUMBER_BOUNDS[_SHAPING_NUMBER_FIELDS[key]], field)
        identity[key] = _canonical_number(value)
    return identity


def workload_identity_json(result: dict[str, Any]) -> str:
    """Serialize workload type and allowlisted shaping config.

    Args:
        result: Benchmark result dictionary.

    Returns:
        Canonical JSON used by the workload key and the repeat fingerprint.
    """
    workload = result.get("workload") if isinstance(result.get("workload"), dict) else {}
    payload = {
        "type": canonical_descriptor(str(workload.get("type") or "")),
        "config": workload_config_identity(workload.get("config") or {}),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def workload_key(result: dict[str, Any]) -> str:
    """Build the workload identity used by both independent-repeat gates.

    Args:
        result: Benchmark result dictionary.

    Returns:
        Twenty-character SHA-256-derived workload identity.

    Raises:
        ValueError: If an allowlisted shaping field is not a finite number.
    """
    return hashlib.sha256(workload_identity_json(result).encode("utf-8")).hexdigest()[:20]


def _gpu_index(item: Any, field: str) -> str:
    if not isinstance(item, dict) or item.get("index") is None:
        raise ValueError(f"{field} index is required")
    return _canonical_number(item["index"])


def _indexed_gpu_records(items: list[Any], field: str) -> list[str]:
    indexes = [_gpu_index(item, field) for item in items]
    if len(indexes) != len(set(indexes)):
        raise ValueError(f"{field} contains duplicate indices")
    return indexes


def _measured_content(result: dict[str, Any]) -> dict[str, Any]:
    """Collect the allowlisted measured content of one benchmark result.

    Args:
        result: Benchmark result dictionary.

    Returns:
        Canonical measured content plus workload-config identity. Descriptive
        text, labels, timestamps, notes, and volatile host state are omitted.

    Raises:
        ValueError: If an allowlisted measured value is not a finite number, or
            if resources.gpu indices are duplicated or do not match hardware.gpus.
    """
    workload = result.get("workload") if isinstance(result.get("workload"), dict) else {}
    metrics = result.get("result") if isinstance(result.get("result"), dict) else {}
    resources = result.get("resources") if isinstance(result.get("resources"), dict) else {}
    environment = result.get("environment") if isinstance(result.get("environment"), dict) else {}
    hardware = environment.get("hardware") if isinstance(environment.get("hardware"), dict) else {}

    content: dict[str, Any] = {}
    durations: dict[str, str] = {}
    _put_number(durations, "warmup_seconds", workload.get("warmup_seconds"))
    _put_number(durations, "duration_seconds", workload.get("duration_seconds"))
    if durations:
        content["durations"] = durations

    workload_identity: dict[str, Any] = {}
    if workload.get("type") is not None:
        workload_identity["type"] = canonical_descriptor(str(workload["type"]))
    config_identity = workload_config_identity(workload.get("config") or {})
    if config_identity:
        workload_identity["config"] = config_identity
    if workload_identity:
        content["workload"] = workload_identity

    measured: dict[str, Any] = {}
    for key in _RESULT_MEASURED_KEYS:
        _put_number(measured, key, metrics.get(key))
    latency = metrics.get("latency") if isinstance(metrics.get("latency"), dict) else {}
    latency_numbers = {
        key: _canonical_number(latency[key])
        for key in _LATENCY_KEYS
        if key in latency and latency[key] is not None
    }
    if latency_numbers:
        measured["latency"] = latency_numbers
    if measured:
        content["result"] = measured

    usage: dict[str, Any] = {}
    _put_number(usage, "samples", resources.get("samples"))
    for key in _RESOURCE_SUMMARY_KEYS:
        summary = _summary_numbers(resources.get(key))
        if summary:
            usage[key] = summary
    _put_number(usage, "cpu_freq_ratio_min", resources.get("cpu_freq_ratio_min"))
    for key in ("thermal_measured", "thermal_limit_exceeded", "gpu_measured"):
        if key in resources and resources[key] is not None:
            usage[key] = _canonical_number(resources[key])
    inventory = list(hardware.get("gpus") or [])
    usage_items = list(resources.get("gpu") or [])
    if inventory or usage_items:
        inventory_indexes = _indexed_gpu_records(inventory, "hardware.gpus")
        usage_indexes = _indexed_gpu_records(usage_items, "resources.gpu")
        if set(usage_indexes) != set(inventory_indexes):
            raise ValueError("resources.gpu does not match hardware.gpus")
        gpu_usage = []
        for item in usage_items:
            canonical_gpu: dict[str, Any] = {"index": _gpu_index(item, "resources.gpu")}
            for key in _GPU_USAGE_KEYS:
                summary = _summary_numbers(item.get(key))
                if summary:
                    canonical_gpu[key] = summary
                elif item.get(key) is not None and not isinstance(item.get(key), dict):
                    canonical_gpu[key] = _canonical_number(item[key])
            gpu_usage.append(canonical_gpu)
        usage["gpu"] = _sorted_objects(gpu_usage)
    if usage:
        content["resources"] = usage
    return content


def canonical_hardware_material(result: dict[str, Any]) -> dict[str, Any]:
    """Build the hardware and environment identity shared by both gates.

    Descriptive text is canonicalized with canonical_descriptor, so format
    characters, Unicode spaces, and dotted or dotless I do not split one
    machine. NIC and hardware.gpus inventories are sorted after that
    canonicalization, so rotation does not split one machine either. OS name
    and version live here, not in the repeat fingerprint. cpu_logical_cores is
    the schema's processor count.

    Args:
        result: Benchmark result dictionary.

    Returns:
        JSON-ready identity for the hardware key.

    Raises:
        ValueError: If an identity number is not finite or a GPU inventory
            index is missing or duplicated.
    """
    environment = result.get("environment") if isinstance(result.get("environment"), dict) else {}
    hardware = environment.get("hardware") if isinstance(environment.get("hardware"), dict) else {}
    nics = []
    for nic in hardware.get("network_interfaces") or []:
        if not isinstance(nic, dict):
            nics.append({"name": canonical_descriptor(nic)})
            continue
        canonical_nic: dict[str, Any] = {}
        if nic.get("name") is not None:
            canonical_nic["name"] = canonical_descriptor(nic["name"])
        if nic.get("is_up") is not None:
            canonical_nic["is_up"] = _canonical_number(nic["is_up"])
        for key in ("speed_mbps", "mtu"):
            if nic.get(key) is not None:
                canonical_nic[key] = _canonical_number(nic[key])
        if nic.get("duplex") is not None:
            canonical_nic["duplex"] = canonical_descriptor(nic["duplex"])
        nics.append(canonical_nic)
    inventory = []
    for gpu in hardware.get("gpus") or []:
        index = _gpu_index(gpu, "hardware.gpus")
        canonical_gpu = {"index": index}
        if isinstance(gpu, dict):
            if gpu.get("name") is not None:
                canonical_gpu["name"] = canonical_descriptor(gpu["name"])
            _put_number(canonical_gpu, "memory_total_mib", gpu.get("memory_total_mib"))
            if gpu.get("driver_version") is not None:
                canonical_gpu["driver_version"] = canonical_descriptor(gpu["driver_version"])
        inventory.append(canonical_gpu)
    _indexed_gpu_records(hardware.get("gpus") or [], "hardware.gpus")
    material: dict[str, Any] = {
        "commit_sha": canonical_descriptor(environment.get("commit_sha")),
        "os": None if environment.get("os") is None else canonical_descriptor(environment.get("os")),
        "os_release": (
            None
            if environment.get("os_release") is None
            else canonical_descriptor(environment.get("os_release"))
        ),
        "cpu_model": (
            None
            if hardware.get("cpu_model") is None
            else canonical_descriptor(hardware.get("cpu_model"))
        ),
        "network_interfaces": _sorted_objects(nics),
        "gpus": _sorted_objects(inventory),
    }
    _put_number(material, "cpu_logical_cores", hardware.get("cpu_logical_cores"))
    _put_number(material, "ram_total_bytes", hardware.get("ram_total_bytes"))
    if hardware.get("storage_path") is not None:
        material["storage_path"] = canonical_descriptor(hardware.get("storage_path"))
    if hardware.get("storage_device") is not None:
        material["storage_device"] = canonical_descriptor(hardware.get("storage_device"))
    if hardware.get("storage_fstype") is not None:
        material["storage_fstype"] = canonical_descriptor(hardware.get("storage_fstype"))
    _put_number(material, "storage_total_bytes", hardware.get("storage_total_bytes"))
    return material


def hardware_key(result: dict[str, Any]) -> str:
    """Build the hardware identity used by both independent-repeat gates.

    Descriptive text is canonicalized and NIC and GPU inventories are sorted.
    Dotted and dotless I are the same key. build_report and build_matrix both
    call this function.

    Args:
        result: Benchmark result dictionary.

    Returns:
        Twenty-character SHA-256-derived hardware identity.

    Raises:
        ValueError: If a hardware number is not finite or a GPU index is missing
            or duplicated.
    """
    return hashlib.sha256(
        json.dumps(canonical_hardware_material(result), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]


def content_fingerprint(result: dict[str, Any]) -> str:
    """Hash allowlisted measurements and workload-config identity.

    The fingerprint covers durations, throughput and capacity figures, latency
    figures, failure_rate and error counts, resource-usage summaries, and the
    workload type plus WORKLOAD_SHAPING_CONFIG_FIELDS. That same constant is
    the workload key. Free-text config keys are ignored. Descriptive identity
    text such as OS name and version, CPU model, and NIC or GPU inventory names
    is not included.
    That text belongs only to the hardware key. Copies that differ only there
    collide and cannot count as independent repeats.

    Per-GPU usage stays keyed by GPU index. Duplicate resources.gpu indices, and
    usage records that do not match the hardware.gpus inventory, are rejected.
    They are not silently deduplicated. Every number becomes a finite float,
    -0.0 becomes 0.0, and an integer that overflows float uses that same
    ValueError. NaN and infinity are rejected. Keys are sorted and floats use
    one fixed repr, so 0, 0.0, and -0.0, or 1000 and 1000.0, are one measurement.

    This gate defends against duplicated or relabeled evidence. It does not
    defend against deliberately fabricated measurements; provenance or signing
    is separate future work. Genuine runs with distinct measured values, such
    as throughputs 1000, 980, and 1020, stay distinct. Byte-identical genuine
    aggregates share this fingerprint and fail closed.

    Args:
        result: Benchmark result dictionary.

    Returns:
        Hexadecimal SHA-256 of the canonical allowlisted content.

    Raises:
        ValueError: If an allowlisted measured value is not a finite number, or
            if GPU usage indices are duplicated or do not match the inventory.
    """
    material = json.dumps(
        _measured_content(result),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _validate_numeric_fields(result: dict[str, Any]) -> dict[str, Any]:
    """Check every allowlisted numeric field and return the values gates read.

    Args:
        result: Benchmark result dictionary.

    Returns:
        Parsed duration, warmup, failure rate, resource percentiles and flags.

    Raises:
        ValueError: If a present numeric field is not a finite in-range number.
    """
    workload = result.get("workload") if isinstance(result.get("workload"), dict) else {}
    metrics = result.get("result") if isinstance(result.get("result"), dict) else {}
    resources = result.get("resources") if isinstance(result.get("resources"), dict) else {}
    environment = result.get("environment") if isinstance(result.get("environment"), dict) else {}
    hardware = environment.get("hardware") if isinstance(environment.get("hardware"), dict) else {}
    duration = _checked_number(
        workload.get("duration_seconds"),
        _NUMBER_BOUNDS["duration"],
        "workload.duration_seconds",
    )
    warmup = _checked_number(
        workload.get("warmup_seconds"),
        _NUMBER_BOUNDS["duration"],
        "workload.warmup_seconds",
    )
    failure = _present_number(metrics, "failure_rate", "unit_interval", "result.failure_rate")
    for key, kind in _RESULT_FIELD_BOUNDS.items():
        if key == "failure_rate":
            continue
        _present_number(metrics, key, kind, f"result.{key}")
    latency = metrics.get("latency") if isinstance(metrics.get("latency"), dict) else {}
    for key, kind in _LATENCY_FIELD_BOUNDS.items():
        _present_number(latency, key, kind, f"result.latency.{key}")
    for key, kind in _SUMMARY_FIELD_BOUNDS.items():
        _check_summary(resources.get(key), kind, f"resources.{key}")
    _present_number(resources, "samples", "samples", "resources.samples")
    ratio = _present_number(resources, "cpu_freq_ratio_min", "ratio", "resources.cpu_freq_ratio_min")
    for key in ("thermal_measured", "thermal_limit_exceeded", "gpu_measured"):
        if key in resources:
            _checked_number(resources[key], _NUMBER_BOUNDS["unit_interval"], f"resources.{key}")
    for item in resources.get("gpu") or []:
        if not isinstance(item, dict):
            raise ValueError("resources.gpu index is required")
        _checked_number(item.get("index"), _NUMBER_BOUNDS["gpu_index"], "resources.gpu.index")
        for key, kind in _GPU_SUMMARY_BOUNDS.items():
            _check_summary(item.get(key), kind, f"resources.gpu.{key}")
    _present_number(hardware, "cpu_logical_cores", "cores", "hardware.cpu_logical_cores")
    _present_number(hardware, "ram_total_bytes", "ram_bytes", "hardware.ram_total_bytes")
    _present_number(hardware, "storage_total_bytes", "storage_bytes", "hardware.storage_total_bytes")
    for nic in hardware.get("network_interfaces") or []:
        if not isinstance(nic, dict):
            continue
        _present_number(nic, "speed_mbps", "rate", "hardware.network_interfaces.speed_mbps")
        _present_number(nic, "mtu", "mtu", "hardware.network_interfaces.mtu")
    for gpu in hardware.get("gpus") or []:
        if not isinstance(gpu, dict):
            raise ValueError("hardware.gpus index is required")
        _checked_number(gpu.get("index"), _NUMBER_BOUNDS["gpu_index"], "hardware.gpus.index")
        _present_number(gpu, "memory_total_mib", "gpu_memory", "hardware.gpus.memory_total_mib")
    workload_config_identity(workload.get("config") or {})
    cpu_pct = resources.get("cpu_pct") if isinstance(resources.get("cpu_pct"), dict) else {}
    ram_pct = resources.get("ram_pct") if isinstance(resources.get("ram_pct"), dict) else {}
    return {
        "duration_seconds": duration,
        "warmup_seconds": warmup,
        "failure_rate": 0.0 if failure is None else failure,
        "cpu_p95_pct": None if "p95" not in cpu_pct else _checked_number(
            cpu_pct["p95"], _NUMBER_BOUNDS["percent"], "resources.cpu_pct.p95"
        ),
        "ram_p95_pct": None if "p95" not in ram_pct else _checked_number(
            ram_pct["p95"], _NUMBER_BOUNDS["percent"], "resources.ram_pct.p95"
        ),
        "cpu_freq_ratio_min": ratio,
        "p95_ms": None if "p95_ms" not in latency else _checked_number(
            latency["p95_ms"], _NUMBER_BOUNDS["latency_ms"], "result.latency.p95_ms"
        ),
        "thermal_measured": bool(resources.get("thermal_measured")),
        "thermal_limit_exceeded": bool(resources.get("thermal_limit_exceeded")),
    }


def repeat_verdict(
    benchmark_ids: list[str],
    content_fingerprints: list[str],
    *,
    min_repeats: int,
) -> tuple[int, list[str]]:
    """Count independent repeats from identities and content fingerprints.

    build_report and build_matrix both use this helper. A repeated identity or
    a repeated content fingerprint cannot satisfy the repeat minimum.

    Args:
        benchmark_ids: Benchmark identities in submission order.
        content_fingerprints: Allowlisted content fingerprints in the same order.
        min_repeats: Minimum number of unique content fingerprints.

    Returns:
        Unique content-fingerprint count and the rejection reasons.
    """
    reasons: list[str] = []
    duplicate_identities = sorted(
        value for value in set(benchmark_ids) if benchmark_ids.count(value) > 1
    )
    duplicate_content = sorted(
        value for value in set(content_fingerprints) if content_fingerprints.count(value) > 1
    )
    if duplicate_identities:
        reasons.append(
            "duplicate benchmark identities are not independent repeats: "
            + ", ".join(duplicate_identities)
        )
    if duplicate_content:
        reasons.append("duplicate benchmark content fingerprints are not independent repeats")
    repeat_count = len(set(content_fingerprints))
    if repeat_count < min_repeats:
        reasons.append(f"repeat_count {repeat_count} < required {min_repeats}")
    return repeat_count, reasons


@dataclass(frozen=True)
class Rejection:
    """A benchmark record that gates report instead of counting as a repeat.

    Attributes:
        benchmark_id: Source benchmark id when the record had one.
        reason: Controlled explanation safe to store in a QA report.
    """

    benchmark_id: str
    reason: str


@dataclass(frozen=True)
class ValidatedRecord:
    """Benchmark record after the single numeric and hardware parse.

    Attributes:
        source: Original benchmark result.
        benchmark_id: Source benchmark identifier.
        content_fingerprint: Allowlisted measurement fingerprint.
        result_fingerprint: Full-result tamper fingerprint.
        hardware_key: Canonical hardware and environment identity.
        workload_key: Allowlisted workload identity.
        commit_sha: Benchmarked source revision.
        duration_seconds: Parsed workload duration.
        warmup_seconds: Parsed warmup.
        failure_rate: Parsed failure rate, or zero when the field is absent.
        cpu_p95_pct: Parsed CPU p95 when present.
        ram_p95_pct: Parsed RAM p95 when present.
        cpu_freq_ratio_min: Parsed minimum CPU frequency ratio when present.
        p95_ms: Parsed p95 latency when present.
        thermal_measured: Whether thermal evidence was recorded.
        thermal_limit_exceeded: Whether a thermal limit was recorded.
    """

    source: dict[str, Any]
    benchmark_id: str
    content_fingerprint: str
    result_fingerprint: str
    hardware_key: str
    workload_key: str
    commit_sha: str
    duration_seconds: float
    warmup_seconds: float
    failure_rate: float
    cpu_p95_pct: float | None
    ram_p95_pct: float | None
    cpu_freq_ratio_min: float | None
    p95_ms: float | None
    thermal_measured: bool
    thermal_limit_exceeded: bool


def parse_benchmark_record(result: dict[str, Any]) -> ValidatedRecord | Rejection:
    """Parse one benchmark record before either independent-repeat gate.

    Container types, nesting depth, numeric conversion, bounds, GPU index
    rules, the hardware key, and the workload key all happen here. A wrong
    container, a document deeper than MAX_STRUCTURE_DEPTH, and OverflowError,
    ValueError, TypeError, KeyError, or AttributeError from record content
    become a Rejection. Gates do not see them.

    Args:
        result: Candidate benchmark result.

    Returns:
        Validated record, or a structured rejection when the content is malformed.
    """
    benchmark_id = ""
    try:
        if not isinstance(result, dict):
            raise ValueError("benchmark result is not an object")
        benchmark_id = str(result.get("benchmark_id") or "")
        validate_result(result)
        parsed = _validate_numeric_fields(result)
        return ValidatedRecord(
            source=result,
            benchmark_id=str(result["benchmark_id"]),
            content_fingerprint=content_fingerprint(result),
            result_fingerprint=result_fingerprint(result),
            hardware_key=hardware_key(result),
            workload_key=workload_key(result),
            commit_sha=str(result["environment"]["commit_sha"]),
            duration_seconds=parsed["duration_seconds"],
            warmup_seconds=parsed["warmup_seconds"],
            failure_rate=parsed["failure_rate"],
            cpu_p95_pct=parsed["cpu_p95_pct"],
            ram_p95_pct=parsed["ram_p95_pct"],
            cpu_freq_ratio_min=parsed["cpu_freq_ratio_min"],
            p95_ms=parsed["p95_ms"],
            thermal_measured=parsed["thermal_measured"],
            thermal_limit_exceeded=parsed["thermal_limit_exceeded"],
        )
    except (
        ValueError,
        OverflowError,
        TypeError,
        KeyError,
        AttributeError,
        ArithmeticError,
        RecursionError,
    ) as exc:
        return Rejection(benchmark_id=benchmark_id, reason=str(exc) or exc.__class__.__name__)


def utc_iso() -> str:
    """Return the current timezone-aware UTC instant as ISO-8601 text."""
    return datetime.now(timezone.utc).isoformat()


def percentile(values: list[float], q: float) -> float | None:
    """Calculate an interpolated percentile for numeric values.

    Args:
        values: Numeric samples.
        q: Quantile in the inclusive range 0 through 1; values are clamped.

    Returns:
        Interpolated percentile, or None for an empty sample.
    """
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    q = min(1.0, max(0.0, q))
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def latency_summary(values_seconds: list[float]) -> dict[str, float | int | None]:
    """Summarize latency samples using millisecond percentiles and averages.

    Args:
        values_seconds: Latency observations in seconds.

    Returns:
        Count, p50, p95, p99, max and mean latency values in milliseconds.
    """
    values_ms = [max(0.0, x) * 1000.0 for x in values_seconds]
    return {
        "count": len(values_ms),
        "p50_ms": percentile(values_ms, 0.50),
        "p95_ms": percentile(values_ms, 0.95),
        "p99_ms": percentile(values_ms, 0.99),
        "max_ms": max(values_ms) if values_ms else None,
        "mean_ms": (sum(values_ms) / len(values_ms)) if values_ms else None,
    }


def _command_output(args: list[str], timeout: float = 2.0) -> str | None:
    try:
        result = subprocess.run(
            args,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = (result.stdout or result.stderr or "").strip()
    return value or None


def git_sha() -> str:
    """Return the benchmarked commit SHA when available.

    Returns:
        GITHUB_SHA, local git HEAD, or unknown when neither is available.
    """
    return (
        os.getenv("GITHUB_SHA")
        or _command_output(["git", "rev-parse", "HEAD"])
        or "unknown"
    )


def cpu_model() -> str:
    """Return a best-effort CPU model description for benchmark evidence.

    Returns:
        CPU model string from procfs/platform metadata, or unknown.
    """
    if Path("/proc/cpuinfo").exists():
        try:
            for line in Path("/proc/cpuinfo").read_text(errors="ignore").splitlines():
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or "unknown"


def gpu_inventory() -> list[dict[str, Any]]:
    """Return detected NVIDIA GPU inventory without inventing missing hardware.

    Returns:
        GPU dictionaries with index, model, memory and driver version; empty when
        nvidia-smi is unavailable or returns no usable data.
    """
    if not shutil.which("nvidia-smi"):
        return []
    output = _command_output(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ],
        timeout=4.0,
    )
    if not output:
        return []
    result = []
    for index, line in enumerate(output.splitlines()):
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 3:
            continue
        result.append(
            {
                "index": index,
                "name": parts[0],
                "memory_total_mib": float(parts[1]),
                "driver_version": parts[2],
            }
        )
    return result


def environment_metadata(storage_path: str = ".") -> dict[str, Any]:
    """Capture benchmark environment and hardware metadata.

    Args:
        storage_path: Filesystem path used to resolve storage capacity/device metadata.

    Returns:
        Environment dictionary containing commit, OS, runtime and measured hardware
        descriptors. Unavailable measurements remain None or unavailable.
    """
    ram_total = None
    interfaces: list[dict[str, Any]] = []
    storage_total = None
    storage_free = None
    storage_device = None
    storage_fstype = None
    if psutil is not None:
        ram_total = int(psutil.virtual_memory().total)
        nic_stats = psutil.net_if_stats()
        for name in sorted(psutil.net_io_counters(pernic=True).keys()):
            stat = nic_stats.get(name)
            interfaces.append(
                {
                    "name": name,
                    "is_up": bool(stat.isup) if stat else None,
                    "speed_mbps": int(stat.speed) if stat and stat.speed > 0 else None,
                    "mtu": int(stat.mtu) if stat else None,
                    "duplex": str(stat.duplex) if stat else None,
                }
            )
        usage = psutil.disk_usage(storage_path)
        storage_total = int(usage.total)
        storage_free = int(usage.free)
        try:
            resolved = str(Path(storage_path).resolve())
            partitions = sorted(
                psutil.disk_partitions(all=True),
                key=lambda p: len(p.mountpoint),
                reverse=True,
            )
            for part in partitions:
                mount = str(Path(part.mountpoint).resolve())
                if resolved == mount or resolved.startswith(mount.rstrip("/") + "/"):
                    storage_device = part.device or None
                    storage_fstype = part.fstype or None
                    break
        except (OSError, ValueError):
            pass

    return {
        "commit_sha": git_sha(),
        "captured_at": utc_iso(),
        "os": platform.system(),
        "os_release": platform.release(),
        "kernel": platform.version(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "container_runtime": _command_output(["docker", "--version"]) or "unavailable",
        "hardware": {
            "cpu_model": cpu_model(),
            "cpu_logical_cores": os.cpu_count(),
            "ram_total_bytes": ram_total,
            "network_interfaces": interfaces,
            "storage_path": str(Path(storage_path).resolve()),
            "storage_device": storage_device,
            "storage_fstype": storage_fstype,
            "storage_total_bytes": storage_total,
            "storage_free_bytes": storage_free,
            "gpus": gpu_inventory(),
        },
    }


@dataclass
class _Counters:
    monotonic: float
    net_sent: int
    net_recv: int
    disk_read: int
    disk_write: int


class SystemSampler:
    """Collect process-host resource samples for benchmark evidence."""

    def __init__(self):
        self._previous: _Counters | None = None

    def sample(self) -> dict[str, Any]:
        """Collect one CPU, RAM, network, disk, thermal and GPU sample.

        Returns:
            Resource sample dictionary. Unavailable sensors are represented by
            None or empty collections rather than fabricated values.
        """
        now = time.monotonic()
        if psutil is None:
            return {
                "monotonic": now,
                "cpu_pct": None,
                "cpu_freq_mhz": None,
                "cpu_freq_max_mhz": None,
                "max_temperature_c": None,
                "temperatures": [],
                "ram_used_bytes": None,
                "ram_pct": None,
                "net_rx_mbps": None,
                "net_tx_mbps": None,
                "disk_read_mbps": None,
                "disk_write_mbps": None,
                "gpu": [],
            }

        vm = psutil.virtual_memory()
        net = psutil.net_io_counters()
        disk = psutil.disk_io_counters()
        current = _Counters(
            monotonic=now,
            net_sent=int(net.bytes_sent),
            net_recv=int(net.bytes_recv),
            disk_read=int(disk.read_bytes if disk else 0),
            disk_write=int(disk.write_bytes if disk else 0),
        )
        rx = tx = read = write = 0.0
        if self._previous is not None:
            elapsed = max(1e-6, current.monotonic - self._previous.monotonic)
            rx = max(0, current.net_recv - self._previous.net_recv) * 8 / elapsed / 1_000_000
            tx = max(0, current.net_sent - self._previous.net_sent) * 8 / elapsed / 1_000_000
            read = max(0, current.disk_read - self._previous.disk_read) / elapsed / 1_000_000
            write = max(0, current.disk_write - self._previous.disk_write) / elapsed / 1_000_000
        self._previous = current

        gpu = []
        if shutil.which("nvidia-smi"):
            output = _command_output(
                [
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,memory.used,temperature.gpu",
                    "--format=csv,noheader,nounits",
                ],
                timeout=2.0,
            )
            if output:
                for index, line in enumerate(output.splitlines()):
                    parts = [part.strip() for part in line.split(",")]
                    if len(parts) >= 2:
                        gpu.append(
                            {
                                "index": index,
                                "utilization_pct": float(parts[0]),
                                "memory_used_mib": float(parts[1]),
                                "temperature_c": (
                                    float(parts[2]) if len(parts) >= 3 and parts[2] not in {"", "N/A"} else None
                                ),
                            }
                        )

        cpu_freq = psutil.cpu_freq()
        temperatures = []
        try:
            for sensor_name, readings in psutil.sensors_temperatures().items():
                for reading in readings:
                    if reading.current is not None:
                        temperatures.append(
                            {
                                "sensor": sensor_name,
                                "label": reading.label or "",
                                "current_c": float(reading.current),
                                "high_c": float(reading.high) if reading.high is not None else None,
                                "critical_c": float(reading.critical) if reading.critical is not None else None,
                            }
                        )
        except (AttributeError, OSError):
            temperatures = []

        return {
            "monotonic": now,
            "cpu_pct": float(psutil.cpu_percent(interval=None)),
            "cpu_freq_mhz": float(cpu_freq.current) if cpu_freq and cpu_freq.current is not None else None,
            "cpu_freq_max_mhz": float(cpu_freq.max) if cpu_freq and cpu_freq.max else None,
            "max_temperature_c": max((t["current_c"] for t in temperatures), default=None),
            "temperatures": temperatures,
            "ram_used_bytes": int(vm.used),
            "ram_pct": float(vm.percent),
            "net_rx_mbps": rx,
            "net_tx_mbps": tx,
            "disk_read_mbps": read,
            "disk_write_mbps": write,
            "gpu": gpu,
        }


def _metric_summary(samples: list[dict[str, Any]], key: str) -> dict[str, float | None]:
    values = [float(s[key]) for s in samples if s.get(key) is not None]
    return {
        "mean": sum(values) / len(values) if values else None,
        "p95": percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def resource_summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate raw resource samples into benchmark evidence summaries.

    Args:
        samples: Resource samples collected during a workload.

    Returns:
        CPU, RAM, network, disk, GPU and thermal summary values.
    """
    freq_ratios = [
        float(sample["cpu_freq_mhz"]) / float(sample["cpu_freq_max_mhz"])
        for sample in samples
        if sample.get("cpu_freq_mhz") is not None
        and sample.get("cpu_freq_max_mhz") not in (None, 0)
    ]
    thermal_limit_exceeded = any(
        (
            reading.get("critical_c") is not None
            and float(reading["current_c"]) >= float(reading["critical_c"])
        )
        or (
            reading.get("high_c") is not None
            and float(reading["current_c"]) >= float(reading["high_c"])
        )
        for sample in samples
        for reading in sample.get("temperatures", [])
        if reading.get("current_c") is not None
    )

    gpu_indexes = sorted(
        {
            int(g["index"])
            for sample in samples
            for g in sample.get("gpu", [])
            if "index" in g
        }
    )
    gpu = []
    for index in gpu_indexes:
        util = [
            float(g["utilization_pct"])
            for sample in samples
            for g in sample.get("gpu", [])
            if int(g.get("index", -1)) == index and g.get("utilization_pct") is not None
        ]
        mem = [
            float(g["memory_used_mib"])
            for sample in samples
            for g in sample.get("gpu", [])
            if int(g.get("index", -1)) == index and g.get("memory_used_mib") is not None
        ]
        temp = [
            float(g["temperature_c"])
            for sample in samples
            for g in sample.get("gpu", [])
            if int(g.get("index", -1)) == index and g.get("temperature_c") is not None
        ]
        gpu.append(
            {
                "index": index,
                "utilization_pct": {
                    "mean": sum(util) / len(util) if util else None,
                    "p95": percentile(util, 0.95),
                    "max": max(util) if util else None,
                },
                "memory_used_mib": {
                    "mean": sum(mem) / len(mem) if mem else None,
                    "p95": percentile(mem, 0.95),
                    "max": max(mem) if mem else None,
                },
                "temperature_c": {
                    "mean": sum(temp) / len(temp) if temp else None,
                    "p95": percentile(temp, 0.95),
                    "max": max(temp) if temp else None,
                },
            }
        )

    return {
        "samples": len(samples),
        "cpu_pct": _metric_summary(samples, "cpu_pct"),
        "cpu_freq_mhz": _metric_summary(samples, "cpu_freq_mhz"),
        "cpu_freq_max_mhz": _metric_summary(samples, "cpu_freq_max_mhz"),
        "cpu_freq_ratio_min": min(freq_ratios) if freq_ratios else None,
        "max_temperature_c": _metric_summary(samples, "max_temperature_c"),
        "thermal_measured": any(sample.get("max_temperature_c") is not None for sample in samples),
        "thermal_limit_exceeded": thermal_limit_exceeded,
        "ram_used_bytes": _metric_summary(samples, "ram_used_bytes"),
        "ram_pct": _metric_summary(samples, "ram_pct"),
        "net_rx_mbps": _metric_summary(samples, "net_rx_mbps"),
        "net_tx_mbps": _metric_summary(samples, "net_tx_mbps"),
        "disk_read_mbps": _metric_summary(samples, "disk_read_mbps"),
        "disk_write_mbps": _metric_summary(samples, "disk_write_mbps"),
        "gpu": gpu,
        "gpu_measured": bool(gpu),
    }


def build_result(
    *,
    workload_type: str,
    workload_config: dict[str, Any],
    started_at: str,
    duration_seconds: float,
    warmup_seconds: float,
    operations_ok: int,
    operations_failed: int,
    latencies_seconds: list[float],
    samples: list[dict[str, Any]],
    storage_path: str = ".",
    extra_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one Phase-8 benchmark result with environment/resource evidence.

    Args:
        workload_type: Stable workload type identifier.
        workload_config: Reproducible workload configuration.
        started_at: UTC benchmark start timestamp.
        duration_seconds: Measured workload duration.
        warmup_seconds: Warmup duration excluded from measured workload.
        operations_ok: Successful operation count.
        operations_failed: Failed operation count.
        latencies_seconds: Measured operation latencies.
        samples: Collected host resource samples.
        storage_path: Storage path used for environment metadata.
        extra_metrics: Optional workload-specific result metrics.

    Returns:
        Phase-8 benchmark result dictionary matching the repository schema.
    """
    total = operations_ok + operations_failed
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark_id": str(uuid.uuid4()),
        "environment": environment_metadata(storage_path),
        "workload": {
            "type": workload_type,
            "config": workload_config,
            "started_at": started_at,
            "warmup_seconds": warmup_seconds,
            "duration_seconds": duration_seconds,
        },
        "result": {
            "operations_ok": operations_ok,
            "operations_failed": operations_failed,
            "failure_rate": (operations_failed / total) if total else None,
            "throughput_ops_s": (operations_ok / duration_seconds) if duration_seconds > 0 else None,
            "latency": latency_summary(latencies_seconds),
            **(extra_metrics or {}),
        },
        "resources": resource_summary(samples),
    }


def validate_result(result: dict[str, Any]) -> None:
    """Validate required schema, container types, and evidence fields.

    environment, hardware, workload, result, resources, and result.latency must
    be objects before any attribute access. hardware null is invalid and must
    not collapse to an empty machine identity. Nesting deeper than
    MAX_STRUCTURE_DEPTH is invalid.

    Args:
        result: Benchmark result dictionary.

    Returns:
        None when required schema and evidence fields are valid.

    Raises:
        ValueError: If schema version, container types, nesting, or required
            evidence fields are invalid.
    """
    if not isinstance(result, dict):
        raise ValueError("benchmark result is not an object")
    if _structure_deeper_than(result, MAX_STRUCTURE_DEPTH):
        raise ValueError(f"benchmark record nesting exceeds {MAX_STRUCTURE_DEPTH}")
    required_top = {"schema_version", "benchmark_id", "environment", "workload", "result", "resources"}
    missing = required_top - set(result)
    if missing:
        raise ValueError(f"benchmark result missing keys: {sorted(missing)}")
    if result["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported benchmark result schema")
    environment = result["environment"]
    if not isinstance(environment, dict):
        raise ValueError("environment is not an object")
    if not environment.get("commit_sha"):
        raise ValueError("commit_sha is required")
    if not isinstance(environment.get("hardware"), dict):
        raise ValueError("hardware is not an object")
    workload = result["workload"]
    if not isinstance(workload, dict):
        raise ValueError("workload is not an object")
    if not isinstance(workload.get("type"), str) or not workload.get("type"):
        raise ValueError("workload.type is required")
    metrics = result["result"]
    if not isinstance(metrics, dict):
        raise ValueError("result is not an object")
    latency = metrics.get("latency", {})
    if not isinstance(latency, dict):
        raise ValueError("result.latency is not an object")
    for key in ("p50_ms", "p95_ms", "p99_ms"):
        if key not in latency:
            raise ValueError(f"latency.{key} is required")
    if not isinstance(result["resources"], dict):
        raise ValueError("resources is not an object")


def write_result(
    result: dict[str, Any],
    *,
    json_path: str,
    csv_path: str | None = None,
) -> None:
    """Validate and persist benchmark evidence as JSON and optional CSV summary.

    Args:
        result: Completed benchmark result dictionary.
        json_path: Destination JSON evidence path.
        csv_path: Optional append-only CSV summary path.

    Returns:
        None after evidence is written.

    Raises:
        ValueError: If the result fails schema validation.
        OSError: If output files cannot be written.
    """
    validate_result(result)
    target = Path(json_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")

    if csv_path:
        row = {
            "benchmark_id": result["benchmark_id"],
            "commit_sha": result["environment"]["commit_sha"],
            "workload_type": result["workload"]["type"],
            "duration_seconds": result["workload"]["duration_seconds"],
            "operations_ok": result["result"]["operations_ok"],
            "operations_failed": result["result"]["operations_failed"],
            "throughput_ops_s": result["result"]["throughput_ops_s"],
            "p50_ms": result["result"]["latency"]["p50_ms"],
            "p95_ms": result["result"]["latency"]["p95_ms"],
            "p99_ms": result["result"]["latency"]["p99_ms"],
            "cpu_p95_pct": result["resources"]["cpu_pct"]["p95"],
            "ram_p95_bytes": result["resources"]["ram_used_bytes"]["p95"],
            "net_rx_p95_mbps": result["resources"]["net_rx_mbps"]["p95"],
            "net_tx_p95_mbps": result["resources"]["net_tx_mbps"]["p95"],
            "disk_read_p95_mbps": result["resources"]["disk_read_mbps"]["p95"],
            "disk_write_p95_mbps": result["resources"]["disk_write_mbps"]["p95"],
            "gpu_measured": result["resources"]["gpu_measured"],
        }
        csv_target = Path(csv_path)
        csv_target.parent.mkdir(parents=True, exist_ok=True)
        exists = csv_target.exists()
        with csv_target.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row))
            if not exists:
                writer.writeheader()
            writer.writerow(row)
