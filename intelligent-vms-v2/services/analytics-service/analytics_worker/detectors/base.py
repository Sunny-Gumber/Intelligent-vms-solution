"""Detector interface.

Any object detector - bundled or supplied by the customer - implements this
interface. The worker depends only on this abstraction, so swapping a model
never touches the pipeline, the database or the API.

NOTE: no concrete detector with weights is shipped. `NullDetector` is the
default and is explicit about doing nothing.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from vms_contracts.enums import AnalyticsClass


@dataclass(frozen=True)
class Detection:
    """One detected object in normalized image coordinates (0..1)."""

    object_class: AnalyticsClass
    confidence: float
    bbox: tuple[float, float, float, float]   # x, y, w, h
    track_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def as_list(self) -> list[float]:
        return [round(v, 5) for v in self.bbox]


class Detector(abc.ABC):
    """Contract every detector must satisfy."""

    name: str = "abstract"
    model_version: str = "0"

    @abc.abstractmethod
    def available(self) -> bool:
        """True only when this detector can genuinely run inference."""

    @abc.abstractmethod
    def detect(self, frame, *, min_confidence: float = 0.5) -> list[Detection]:
        """Run inference on one decoded frame (HWC, BGR, uint8)."""

    def warmup(self) -> None:
        return None

    def close(self) -> None:
        return None


class NullDetector(Detector):
    """The default. Performs no inference and says so.

    Used when no model is configured. It exists so the worker, the health
    report and the API can all report "server-side inference is unavailable"
    truthfully instead of returning an empty result set that looks like
    "nothing was detected".
    """

    name = "null"

    def __init__(self, reason: str = "no detection model is configured") -> None:
        self.reason = reason

    def available(self) -> bool:
        return False

    def detect(self, frame, *, min_confidence: float = 0.5) -> list[Detection]:
        raise RuntimeError(
            f"server-side inference is not available: {self.reason}")
