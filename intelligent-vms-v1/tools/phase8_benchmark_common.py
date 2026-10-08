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


def content_fingerprint(result: dict[str, Any]) -> str:
    """Hash measured benchmark content, ignoring run labels and clock stamps.

    build_result writes three fields that label a run without measuring it:
    benchmark_id, environment.captured_at, and workload.started_at. Those are
    omitted, as are underscore-prefixed internal fields. Every measured value
    stays in the hash, including durations, result metrics, resource summaries,
    and hardware descriptors. commit_sha stays because it is the measured
    source revision, not a per-run label.

    Two results whose remaining canonical JSON is byte-identical share this
    fingerprint. Three genuine runs that aggregate to the same metrics,
    resources, durations, and hardware descriptors are therefore not
    independent repeats. That fail-closed outcome is accepted: the gate cannot
    tell those runs from copies.

    Args:
        result: Benchmark result dictionary.

    Returns:
        Hexadecimal SHA-256 of the measured content.
    """
    payload = {
        key: value
        for key, value in result.items()
        if not str(key).startswith("_") and key != "benchmark_id"
    }
    environment = payload.get("environment")
    if isinstance(environment, dict):
        payload["environment"] = {
            key: value
            for key, value in environment.items()
            if key != "captured_at"
        }
    workload = payload.get("workload")
    if isinstance(workload, dict):
        payload["workload"] = {
            key: value
            for key, value in workload.items()
            if key != "started_at"
        }
    material = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
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
