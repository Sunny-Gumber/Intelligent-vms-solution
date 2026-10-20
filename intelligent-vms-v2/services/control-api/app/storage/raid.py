"""RAID awareness (software RAID only — explicitly scoped).

SUPPORTED:     Linux md/mdadm via /proc/mdstat, Windows Storage Spaces via
               Get-VirtualDisk health status.
NOT SUPPORTED: hardware RAID controllers (LSI/MegaRAID, PERC, Adaptec, HP
               SmartArray). Those require vendor CLIs that cannot be
               redistributed with this product. The UI reports "unknown" for
               such volumes rather than implying health it cannot verify.
"""

from __future__ import annotations

import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.logging_setup import get_logger

log = get_logger("storage.raid")

MDSTAT = Path("/proc/mdstat")
_ARRAY_RE = re.compile(r"^(md\d+)\s*:\s*(\w+)\s+(\S+)\s+(.*)$")
_STATUS_RE = re.compile(r"\[(\d+)/(\d+)\]\s*\[([U_]+)\]")


@dataclass(frozen=True)
class RaidArray:
    device: str
    level: str
    state: str          # clean | degraded | rebuilding | unknown
    active_disks: int | None = None
    total_disks: int | None = None
    detail: str | None = None

    @property
    def healthy(self) -> bool:
        return self.state == "clean"


def parse_mdstat(text: str) -> list[RaidArray]:
    arrays: list[RaidArray] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _ARRAY_RE.match(line.strip())
        if not match:
            continue
        device, _, level, _ = match.groups()
        state, active, total, detail = "unknown", None, None, None
        for follow in lines[index + 1:index + 4]:
            status = _STATUS_RE.search(follow)
            if status:
                total = int(status.group(1))
                active = int(status.group(2))
                flags = status.group(3)
                state = "clean" if "_" not in flags else "degraded"
                detail = flags
            if "recovery" in follow or "resync" in follow:
                state = "rebuilding"
                detail = follow.strip()[:120]
        arrays.append(RaidArray(device=device, level=level, state=state,
                                active_disks=active, total_disks=total, detail=detail))
    return arrays


def linux_arrays() -> list[RaidArray]:
    if not MDSTAT.is_file():
        return []
    try:
        return parse_mdstat(MDSTAT.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        log.warning("raid.mdstat_unreadable", error=str(exc))
        return []


def windows_arrays(timeout_s: float = 10.0) -> list[RaidArray]:
    if platform.system() != "Windows":
        return []
    command = ["powershell", "-NoProfile", "-Command",
               "Get-VirtualDisk | Select-Object FriendlyName,ResiliencySettingName,"
               "HealthStatus,OperationalStatus | ConvertTo-Csv -NoTypeInformation"]
    try:
        output = subprocess.run(command,  # noqa: S603 - fixed PowerShell command, no user input capture_output=True, text=True,
                                timeout=timeout_s, check=False).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("raid.windows_query_failed", error=str(exc))
        return []
    arrays: list[RaidArray] = []
    for row in output.splitlines()[1:]:
        parts = [p.strip('"') for p in row.split(",")]
        if len(parts) < 4:
            continue
        name, resiliency, health, operational = parts[:4]
        state = {"Healthy": "clean", "Warning": "degraded",
                 "Unhealthy": "degraded"}.get(health, "unknown")
        arrays.append(RaidArray(device=name, level=resiliency, state=state,
                                detail=operational))
    return arrays


def all_arrays() -> list[RaidArray]:
    system = platform.system()
    if system == "Windows":
        return windows_arrays()
    if system == "Linux":
        return linux_arrays()
    return []


def status_for_device(device: str | None) -> str | None:
    if not device:
        return None
    for array in all_arrays():
        if array.device == device or device.endswith(array.device):
            return array.state
    return "unknown"
