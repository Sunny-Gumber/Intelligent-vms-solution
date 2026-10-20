"""Analytics ingestion.

THE CRITICAL HONESTY RULE OF THIS MODULE (Module 9): every analytics record
carries `source`:

    camera_edge       the CAMERA detected this. Intelligent VMS only stored it.
    server_inference  THIS SOFTWARE ran a model on decoded frames.
    external          a third-party engine pushed it over the API.

The API, the UI and every report display this field. Receiving ONVIF analytics
events from a camera is NEVER presented as server-side AI.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from vms_contracts.enums import AnalyticsClass, AnalyticsSource, EventSeverity, EventType

from app.core.errors import ValidationError
from app.core.timeutil import ensure_utc, utcnow
from app.db.models import AnalyticsEvent, Camera
from app.logging_setup import get_logger

log = get_logger("analytics.ingest")

ONVIF_TOPIC_MAP: dict[str, AnalyticsClass] = {
    "ruleengine/objectdetector/person": AnalyticsClass.PERSON,
    "ruleengine/objectdetector/vehicle": AnalyticsClass.VEHICLE,
    "ruleengine/fielddetector/objectsinside": AnalyticsClass.UNKNOWN,
    "ruleengine/linedetector/crossed": AnalyticsClass.UNKNOWN,
    "ruleengine/cellmotiondetector/motion": AnalyticsClass.UNKNOWN,
    "ruleengine/tamperdetector/tamper": AnalyticsClass.UNKNOWN,
    "videosource/motionalarm": AnalyticsClass.UNKNOWN,
    "analytics/facedetection": AnalyticsClass.FACE,
    "analytics/licenseplaterecognition": AnalyticsClass.LICENSE_PLATE,
}


@dataclass
class AnalyticsInput:
    camera_id: str
    source: AnalyticsSource
    object_class: AnalyticsClass = AnalyticsClass.UNKNOWN
    confidence: float | None = None
    occurred_at: dt.datetime | None = None
    bbox: list[float] | None = None
    track_id: str | None = None
    zone_id: str | None = None
    rule: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    plate_text: str | None = None
    face_subject_id: str | None = None
    model_name: str | None = None
    model_version: str | None = None


def validate_bbox(bbox: list[float] | None) -> list[float] | None:
    if bbox is None:
        return None
    if len(bbox) != 4:
        raise ValidationError("bbox must be [x, y, w, h] normalized 0..1", code="invalid_bbox")
    if any((not isinstance(v, (int, float))) or v < -0.01 or v > 1.01 for v in bbox):
        raise ValidationError("bbox values must be normalized between 0 and 1",
                              code="invalid_bbox")
    return [float(v) for v in bbox]


def store(session: Session, data: AnalyticsInput) -> AnalyticsEvent:
    if session.get(Camera, data.camera_id) is None:
        raise ValidationError("Unknown camera", code="camera_not_found")

    record = AnalyticsEvent(
        camera_id=data.camera_id, source=data.source.value,
        object_class=data.object_class.value, confidence=data.confidence,
        occurred_at=ensure_utc(data.occurred_at or utcnow()),
        bbox=validate_bbox(data.bbox), track_id=data.track_id, zone_id=data.zone_id,
        rule=data.rule, attributes=dict(data.attributes or {}),
        plate_text=(data.plate_text or "").upper().replace(" ", "") or None,
        face_subject_id=data.face_subject_id, model_name=data.model_name,
        model_version=data.model_version)
    session.add(record)
    session.flush()
    return record


def to_event(record: AnalyticsEvent, camera: Camera | None = None) -> dict[str, Any]:
    """Convert an analytics record into a pipeline event for rule evaluation."""
    return {
        "type": EventType.ANALYTICS_EVENT,
        "severity": EventSeverity.MEDIUM if record.rule else EventSeverity.LOW,
        "camera_id": record.camera_id,
        "site_id": camera.site_id if camera else None,
        "source": record.source,
        "occurred_at": record.occurred_at,
        "discriminator": f"{record.object_class}:{record.zone_id or '-'}:{record.rule or '-'}",
        "payload": {"analytics_id": record.id, "object_class": record.object_class,
                    "confidence": record.confidence, "analytics_source": record.source,
                    "zone_id": record.zone_id, "rule": record.rule,
                    "track_id": record.track_id, "plate_text": record.plate_text,
                    "attributes": record.attributes},
    }


def from_onvif_notification(camera_id: str, message: dict[str, Any]) -> AnalyticsInput | None:
    """Map an ONVIF NotificationMessage to an analytics input (camera-side)."""
    topic = (message.get("topic") or "").lower()
    normalized = topic.split("tns1:")[-1].strip("/")
    object_class = AnalyticsClass.UNKNOWN
    for known, mapped in ONVIF_TOPIC_MAP.items():
        if known in normalized:
            object_class = mapped
            break
    else:
        if "analytics" not in normalized and "ruleengine" not in normalized:
            return None

    data = message.get("data") or {}
    occurred = message.get("utc_time")
    try:
        occurred_at = (dt.datetime.fromisoformat(occurred.replace("Z", "+00:00"))
                       if occurred else None)
    except (AttributeError, ValueError):
        occurred_at = None

    return AnalyticsInput(
        camera_id=camera_id, source=AnalyticsSource.CAMERA_EDGE,
        object_class=object_class, occurred_at=occurred_at,
        rule=normalized.split("/")[-1] or None,
        attributes={k: v for k, v in data.items() if k.lower() != "state"},
        plate_text=data.get("PlateNumber") or data.get("LicensePlateNumber"))


def search(session: Session, *, camera_ids: list[str] | None = None,
           object_classes: list[str] | None = None, sources: list[str] | None = None,
           since: dt.datetime | None = None, until: dt.datetime | None = None,
           plate_text: str | None = None, zone_id: str | None = None,
           min_confidence: float | None = None, limit: int = 200,
           offset: int = 0) -> tuple[list[AnalyticsEvent], int]:
    def apply(statement):  # noqa: ANN001
        if camera_ids is not None:
            statement = statement.where(AnalyticsEvent.camera_id.in_(camera_ids))
        if object_classes:
            statement = statement.where(AnalyticsEvent.object_class.in_(object_classes))
        if sources:
            statement = statement.where(AnalyticsEvent.source.in_(sources))
        if since:
            statement = statement.where(AnalyticsEvent.occurred_at >= ensure_utc(since))
        if until:
            statement = statement.where(AnalyticsEvent.occurred_at <= ensure_utc(until))
        if plate_text:
            statement = statement.where(
                AnalyticsEvent.plate_text.like(f"%{plate_text.upper().replace(' ', '')}%"))
        if zone_id:
            statement = statement.where(AnalyticsEvent.zone_id == zone_id)
        if min_confidence is not None:
            statement = statement.where(AnalyticsEvent.confidence >= min_confidence)
        return statement

    total = int(session.execute(
        apply(select(func.count()).select_from(AnalyticsEvent))).scalar_one())
    rows = session.execute(
        apply(select(AnalyticsEvent)).order_by(AnalyticsEvent.occurred_at.desc())
        .limit(limit).offset(offset)).scalars().all()
    return list(rows), total
