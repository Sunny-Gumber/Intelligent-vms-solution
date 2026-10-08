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


def _timestamp_key(key: str) -> bool:
    folded = canonical_descriptor(key)
    return folded.endswith("_at") or folded in {"timestamp", "time"}


def _canonical_config_value(value: Any) -> Any:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return _canonical_number(value)
    if isinstance(value, str):
        return canonical_descriptor(value)
    if isinstance(value, dict):
        canonical: dict[str, Any] = {}
        for key, item in value.items():
            if item is None or _timestamp_key(str(key)):
                continue
            canonical[canonical_descriptor(str(key))] = _canonical_config_value(item)
        return canonical
    if isinstance(value, list):
        return [_canonical_config_value(item) for item in value]
    raise ValueError(_NON_FINITE_MEASUREMENT)


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
    config = workload.get("config")
    if isinstance(config, dict) and config:
        canonical_config = _canonical_config_value(config)
        if canonical_config:
            workload_identity["config"] = canonical_config
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


def content_fingerprint(result: dict[str, Any]) -> str:
    """Hash allowlisted measurements and workload-config identity.

    The fingerprint covers durations, throughput and capacity figures, latency
    figures, failure_rate and error counts, resource-usage summaries, and the
    workload type plus non-clock config. Descriptive identity text such as OS
    name and version, CPU model, and NIC or GPU inventory names is not included.
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
    """Validate required schema and evidence fields in a benchmark result.

    Args:
        result: Benchmark result dictionary.

    Returns:
        None when required schema and evidence fields are valid.

    Raises:
        ValueError: If schema version or required evidence fields are invalid.
    """
    required_top = {"schema_version", "benchmark_id", "environment", "workload", "result", "resources"}
    missing = required_top - set(result)
    if missing:
        raise ValueError(f"benchmark result missing keys: {sorted(missing)}")
    if result["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported benchmark result schema")
    if not result["environment"].get("commit_sha"):
        raise ValueError("commit_sha is required")
    latency = result["result"].get("latency", {})
    for key in ("p50_ms", "p95_ms", "p99_ms"):
        if key not in latency:
            raise ValueError(f"latency.{key} is required")


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
