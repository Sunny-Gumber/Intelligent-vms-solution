"""Stream selection and bandwidth estimation.

Choosing the main stream for a 160x90 thumbnail is the most common cause of
"the VMS brought down the network". Deterministic, testable policy:

  * Tile smaller than the sub stream's resolution -> sub (or third)
  * Tile close to main resolution                 -> main
  * Aggregate bandwidth budget exceeded           -> step down
  * Operator override always wins and is recorded
"""

from __future__ import annotations

from dataclasses import dataclass

from vms_contracts.enums import StreamRole

# Conservative fallbacks used only until a stream has been probed.
DEFAULT_BITRATE_KBPS: dict[StreamRole, int] = {
    StreamRole.MAIN: 4096, StreamRole.SUB: 1024, StreamRole.THIRD: 512,
}
ORDER = (StreamRole.MAIN, StreamRole.SUB, StreamRole.THIRD)


@dataclass(frozen=True)
class StreamOption:
    role: StreamRole
    width: int | None = None
    height: int | None = None
    bitrate_kbps: int | None = None
    enabled: bool = True

    @property
    def pixels(self) -> int:
        return (self.width or 0) * (self.height or 0)

    @property
    def effective_bitrate(self) -> int:
        return self.bitrate_kbps or DEFAULT_BITRATE_KBPS.get(self.role, 2048)


@dataclass(frozen=True)
class SelectionResult:
    role: StreamRole
    reason: str
    estimated_kbps: int


def select_stream(options: list[StreamOption], *, tile_width: int | None = None,
                  tile_height: int | None = None, override: StreamRole | None = None,
                  remaining_budget_kbps: int | None = None) -> SelectionResult:
    available = [o for o in options if o.enabled]
    if not available:
        raise ValueError("camera has no enabled streams")

    by_role = {o.role: o for o in available}

    if override is not None:
        option = by_role.get(override)
        if option is None:
            raise ValueError(f"stream {override.value} is not available on this camera")
        return SelectionResult(option.role, "operator override", option.effective_bitrate)

    ordered = [by_role[r] for r in ORDER if r in by_role]
    chosen = ordered[0]
    reason = "only stream available" if len(ordered) == 1 else "largest available stream"

    if tile_width and tile_height and len(ordered) > 1:
        target = tile_width * tile_height
        candidates = [o for o in reversed(ordered) if o.pixels >= target]
        if candidates:
            chosen = candidates[0]
            reason = f"smallest stream covering {tile_width}x{tile_height}"
        else:
            chosen = ordered[0]
            reason = "tile larger than every available stream"

    if remaining_budget_kbps is not None and chosen.effective_bitrate > remaining_budget_kbps:
        cheaper = [o for o in ordered if o.effective_bitrate <= remaining_budget_kbps]
        if cheaper:
            chosen = max(cheaper, key=lambda o: o.effective_bitrate)
            reason = "reduced to fit remaining bandwidth budget"
        else:
            chosen = ordered[-1]
            reason = "bandwidth budget exceeded; using lowest available stream"

    return SelectionResult(chosen.role, reason, chosen.effective_bitrate)


def estimate_storage_bytes(bitrate_kbps: int, seconds: float,
                           container_overhead: float = 1.02) -> int:
    return int((bitrate_kbps * 1000 / 8) * seconds * container_overhead)


def aggregate_bandwidth_kbps(options: list[StreamOption]) -> int:
    return sum(o.effective_bitrate for o in options if o.enabled)
