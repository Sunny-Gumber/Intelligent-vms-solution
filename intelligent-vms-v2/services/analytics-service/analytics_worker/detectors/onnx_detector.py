"""Generic ONNX object detector (YOLO-family output layout).

STATUS: implemented, but NOT exercised in CI because NO MODEL IS SHIPPED. The
post-processing (confidence filtering, NMS, coordinate normalisation, class
mapping) is unit tested with synthetic tensors; end-to-end accuracy depends
entirely on the model the operator supplies and is their responsibility to
validate.

Expected model contract:
  input   : (1, 3, H, W) float32, RGB, 0..1, letterboxed
  output  : (1, N, 5 + num_classes) or (1, 5 + num_classes, N)
            [cx, cy, w, h, objectness, class scores...]
Set VMS_ANALYTICS_MODEL_LABELS to a text file with one class name per line to
map model class indices onto Intelligent VMS analytics classes.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from vms_contracts.enums import AnalyticsClass

from app.logging_setup import get_logger
from analytics_worker.detectors.base import Detection, Detector

log = get_logger("analytics.onnx")

# COCO-style names mapped onto the product's analytics classes. Anything not
# listed becomes UNKNOWN rather than being guessed into a wrong class.
LABEL_MAP: dict[str, AnalyticsClass] = {
    "person": AnalyticsClass.PERSON,
    "bicycle": AnalyticsClass.VEHICLE,
    "car": AnalyticsClass.VEHICLE,
    "motorcycle": AnalyticsClass.VEHICLE,
    "bus": AnalyticsClass.VEHICLE,
    "truck": AnalyticsClass.VEHICLE,
    "train": AnalyticsClass.VEHICLE,
    "cat": AnalyticsClass.ANIMAL,
    "dog": AnalyticsClass.ANIMAL,
    "backpack": AnalyticsClass.BAG,
    "handbag": AnalyticsClass.BAG,
    "suitcase": AnalyticsClass.BAG,
}


def load_labels(path: Path | None) -> list[str]:
    if path is None or not Path(path).is_file():
        return []
    return [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


def letterbox(frame: np.ndarray, size: int) -> tuple[np.ndarray, float, float, float]:
    """Resize preserving aspect ratio and pad. Returns (image, scale, dx, dy)."""
    height, width = frame.shape[:2]
    scale = min(size / width, size / height)
    new_w, new_h = int(round(width * scale)), int(round(height * scale))

    # Nearest-neighbour resize without OpenCV so the worker has no heavy
    # image-processing dependency; the model's own preprocessing dominates.
    y_index = (np.arange(new_h) / scale).astype(np.int32).clip(0, height - 1)
    x_index = (np.arange(new_w) / scale).astype(np.int32).clip(0, width - 1)
    resized = frame[y_index][:, x_index]

    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    dx, dy = (size - new_w) // 2, (size - new_h) // 2
    canvas[dy:dy + new_h, dx:dx + new_w] = resized
    return canvas, scale, float(dx), float(dy)


def non_max_suppression(boxes: np.ndarray, scores: np.ndarray,
                        iou_threshold: float = 0.45) -> list[int]:
    """Standard greedy NMS on [x1, y1, x2, y2] boxes."""
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1).clip(0) * (y2 - y1).clip(0)
    order = scores.argsort()[::-1]

    keep: list[int] = []
    while order.size > 0:
        index = int(order[0])
        keep.append(index)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[index], x1[rest])
        yy1 = np.maximum(y1[index], y1[rest])
        xx2 = np.minimum(x2[index], x2[rest])
        yy2 = np.minimum(y2[index], y2[rest])
        inter = (xx2 - xx1).clip(0) * (yy2 - yy1).clip(0)
        union = areas[index] + areas[rest] - inter
        iou = np.where(union > 0, inter / union, 0.0)
        order = rest[iou <= iou_threshold]
    return keep


def decode_output(raw: np.ndarray, labels: list[str], *, input_size: int,
                  frame_width: int, frame_height: int, scale: float,
                  dx: float, dy: float, min_confidence: float,
                  iou_threshold: float = 0.45) -> list[Detection]:
    """Turn a raw model tensor into normalized detections."""
    output = np.squeeze(raw)
    if output.ndim != 2:
        return []
    # Accept both (N, 5+C) and (5+C, N) layouts.
    if output.shape[0] < output.shape[1] and output.shape[0] < 512:
        output = output.transpose()

    if output.shape[1] < 6:
        return []

    boxes_cxcywh = output[:, :4]
    objectness = output[:, 4]
    class_scores = output[:, 5:]

    class_ids = class_scores.argmax(axis=1)
    confidences = objectness * class_scores[np.arange(len(class_ids)), class_ids]

    mask = confidences >= min_confidence
    if not mask.any():
        return []

    boxes_cxcywh = boxes_cxcywh[mask]
    confidences = confidences[mask]
    class_ids = class_ids[mask]

    # cx,cy,w,h -> x1,y1,x2,y2 in letterboxed pixel space
    half_w = boxes_cxcywh[:, 2] / 2
    half_h = boxes_cxcywh[:, 3] / 2
    xyxy = np.stack([
        boxes_cxcywh[:, 0] - half_w, boxes_cxcywh[:, 1] - half_h,
        boxes_cxcywh[:, 0] + half_w, boxes_cxcywh[:, 1] + half_h], axis=1)

    keep = non_max_suppression(xyxy, confidences, iou_threshold)

    detections: list[Detection] = []
    for index in keep:
        x1, y1, x2, y2 = xyxy[index]
        # undo letterbox padding and scaling, then normalise
        x1 = (x1 - dx) / scale / frame_width
        x2 = (x2 - dx) / scale / frame_width
        y1 = (y1 - dy) / scale / frame_height
        y2 = (y2 - dy) / scale / frame_height

        x1, y1 = max(0.0, float(x1)), max(0.0, float(y1))
        x2, y2 = min(1.0, float(x2)), min(1.0, float(y2))
        if x2 <= x1 or y2 <= y1:
            continue

        label = (labels[class_ids[index]] if class_ids[index] < len(labels) else "")
        detections.append(Detection(
            object_class=LABEL_MAP.get(label, AnalyticsClass.UNKNOWN),
            confidence=float(confidences[index]),
            bbox=(x1, y1, x2 - x1, y2 - y1),
            attributes={"model_label": label} if label else {}))
    return detections


class OnnxDetector(Detector):
    name = "onnx"

    def __init__(self, status, labels_path: Path | None = None,  # noqa: ANN001
                 input_size: int = 640) -> None:
        self.status = status
        self.labels = load_labels(labels_path)
        self.input_size = input_size
        self.model_version = status.model_path.stem if status.model_path else "0"
        self._session = None

    def available(self) -> bool:
        return bool(self.status.available)

    def warmup(self) -> None:
        if not self.available():
            return
        from analytics_worker.runtime import create_session

        self._session = create_session(self.status)
        inputs = self._session.get_inputs()[0]
        shape = inputs.shape
        if isinstance(shape[-1], int):
            self.input_size = int(shape[-1])
        log.info("analytics.detector_ready", model=str(self.status.model_path),
                 input_size=self.input_size, provider=self.status.provider)

    def detect(self, frame: np.ndarray, *, min_confidence: float = 0.5) -> list[Detection]:
        if not self.available():
            raise RuntimeError(self.status.reason or "inference runtime unavailable")
        if self._session is None:
            self.warmup()

        height, width = frame.shape[:2]
        canvas, scale, dx, dy = letterbox(frame, self.input_size)
        rgb = canvas[:, :, ::-1]                       # BGR -> RGB
        tensor = (rgb.astype(np.float32) / 255.0).transpose(2, 0, 1)[None, ...]

        outputs = self._session.run(None, {self._session.get_inputs()[0].name: tensor})
        return decode_output(outputs[0], self.labels, input_size=self.input_size,
                             frame_width=width, frame_height=height, scale=scale,
                             dx=dx, dy=dy, min_confidence=min_confidence)

    def close(self) -> None:
        self._session = None
