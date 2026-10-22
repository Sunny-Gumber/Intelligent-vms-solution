"""Inference runtime selection.

HONEST SCOPE STATEMENT (Module 9)
---------------------------------
This module selects and reports an execution provider for ONNX Runtime. It
does NOT ship a model. Without an operator-supplied model directory the worker
starts, reports `available=False` with a precise reason, and processes nothing.

That is deliberate: a surveillance product must never imply it is performing
detection when it is not.

GPU acceleration is OPTIONAL. If a CUDA/TensorRT provider is requested but the
runtime does not expose it, the worker falls back to CPU and says so, rather
than failing silently or claiming GPU performance it does not have.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.logging_setup import get_logger

log = get_logger("analytics.runtime")

PREFERRED_ORDER = ("TensorrtExecutionProvider", "CUDAExecutionProvider",
                   "DmlExecutionProvider", "CPUExecutionProvider")


@dataclass
class RuntimeStatus:
    available: bool
    provider: str | None = None
    requested_providers: tuple[str, ...] = ()
    available_providers: tuple[str, ...] = ()
    model_path: Path | None = None
    reason: str | None = None
    fell_back_to_cpu: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "provider": self.provider,
            "requested_providers": list(self.requested_providers),
            "available_providers": list(self.available_providers),
            "model": str(self.model_path) if self.model_path else None,
            "reason": self.reason,
            "fell_back_to_cpu": self.fell_back_to_cpu,
            "note": ("No detection model ships with Intelligent VMS. Supply an ONNX "
                     "model and set VMS_ANALYTICS_MODEL_DIR."),
        }


def import_onnxruntime():
    try:
        import onnxruntime

        return onnxruntime
    except ImportError:
        return None


def select_providers(requested: list[str]) -> tuple[list[str], list[str], bool]:
    """Return (chosen, available, fell_back_to_cpu)."""
    onnxruntime = import_onnxruntime()
    if onnxruntime is None:
        return [], [], False

    available = list(onnxruntime.get_available_providers())
    chosen = [p for p in requested if p in available]
    fell_back = False

    if not chosen:
        for provider in PREFERRED_ORDER:
            if provider in available:
                chosen = [provider]
                fell_back = provider == "CPUExecutionProvider" and requested != [provider]
                break
    return chosen, available, fell_back


def resolve(model_dir: Path | None, requested_providers: list[str],
            model_filename: str | None = None) -> RuntimeStatus:
    """Decide whether server-side inference can actually run."""
    onnxruntime = import_onnxruntime()
    if onnxruntime is None:
        return RuntimeStatus(
            available=False,
            requested_providers=tuple(requested_providers),
            reason="onnxruntime is not installed; install the 'onnx' extra to enable "
                   "server-side inference")

    chosen, available, fell_back = select_providers(requested_providers)

    if model_dir is None:
        return RuntimeStatus(
            available=False, requested_providers=tuple(requested_providers),
            available_providers=tuple(available),
            reason="VMS_ANALYTICS_MODEL_DIR is not configured and no model ships with "
                   "this product")

    model_dir = Path(model_dir)
    if not model_dir.is_dir():
        return RuntimeStatus(
            available=False, requested_providers=tuple(requested_providers),
            available_providers=tuple(available),
            reason=f"analytics model directory {model_dir} does not exist")

    candidates = ([model_dir / model_filename] if model_filename
                  else sorted(model_dir.glob("*.onnx")))
    models = [path for path in candidates if path.is_file()]
    if not models:
        return RuntimeStatus(
            available=False, requested_providers=tuple(requested_providers),
            available_providers=tuple(available),
            reason=f"no .onnx model found in {model_dir}")

    status = RuntimeStatus(
        available=True, provider=chosen[0] if chosen else None,
        requested_providers=tuple(requested_providers),
        available_providers=tuple(available), model_path=models[0],
        fell_back_to_cpu=fell_back)

    if fell_back:
        log.warning("analytics.gpu_unavailable", requested=requested_providers,
                    using=status.provider)
    log.info("analytics.runtime_ready", provider=status.provider, model=str(models[0]))
    return status


def create_session(status: RuntimeStatus):
    """Build an ONNX Runtime session. Raises if the runtime is unavailable."""
    if not status.available or status.model_path is None:
        raise RuntimeError(status.reason or "inference runtime is not available")
    onnxruntime = import_onnxruntime()
    providers = [status.provider] if status.provider else ["CPUExecutionProvider"]
    options = onnxruntime.SessionOptions()
    options.graph_optimization_level = (
        onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL)
    return onnxruntime.InferenceSession(str(status.model_path), options,
                                        providers=providers)
