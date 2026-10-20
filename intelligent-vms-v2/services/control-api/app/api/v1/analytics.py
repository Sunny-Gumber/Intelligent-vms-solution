"""Analytics ingestion, search, zones and privacy-gated capabilities."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from vms_contracts.dto import AnalyticsEventOut
from vms_contracts.enums import AnalyticsClass, AnalyticsSource, Permission

from app.analytics import anpr, counting, ingest, linking, privacy
from app.analytics.zones import validate_polygon
from app.core import audit
from app.core.deps import Ctr, CurrentPrincipal, DbSession, client_ip, load_camera
from app.core.errors import NotFoundError, ValidationError
from app.core.rbac import accessible_camera_ids
from app.db.models import AnalyticsEvent, AnalyticsZone

router = APIRouter(prefix="/analytics", tags=["analytics"])


def dto(record: AnalyticsEvent) -> AnalyticsEventOut:
    return AnalyticsEventOut(
        id=record.id, camera_id=record.camera_id,
        source=AnalyticsSource(record.source),
        object_class=AnalyticsClass(record.object_class),
        confidence=record.confidence, occurred_at=record.occurred_at,
        bbox=record.bbox, track_id=record.track_id, zone_id=record.zone_id,
        rule=record.rule, attributes=record.attributes, plate_text=record.plate_text,
        has_recording=record.segment_id is not None)


class DetectionIn(BaseModel):
    camera_id: str
    object_class: AnalyticsClass = AnalyticsClass.UNKNOWN
    confidence: float | None = Field(default=None, ge=0, le=1)
    occurred_at: dt.datetime | None = None
    bbox: list[float] | None = None
    track_id: str | None = None
    zone_id: str | None = None
    rule: str | None = None
    attributes: dict = Field(default_factory=dict)
    plate_text: str | None = None
    model_name: str | None = None
    model_version: str | None = None


class ZoneIn(BaseModel):
    name: str
    kind: str = "region"
    polygon: list[list[float]]
    direction: str | None = None
    object_classes: list[str] = []
    dwell_seconds: float = 0.0
    min_confidence: float = 0.5
    enabled: bool = True


@router.get("/capabilities",
            summary="Which analytics capabilities are actually available")
def capabilities(container: Ctr, principal: CurrentPrincipal) -> dict:
    principal.require(Permission.LIVE_VIEW)
    settings = container.settings
    return {
        "camera_edge_ingestion": True,
        "onvif_analytics_ingestion": True,
        "external_ingestion": True,
        "server_side_inference": {
            "worker_available": True,
            "models_bundled": False,
            "note": ("The analytics worker and detector interface are implemented. "
                     "No detection model ships with this release; supply an ONNX "
                     "model and set VMS_ANALYTICS_MODEL_DIR."),
        },
        "zone_rules": ["intrusion", "tripwire", "loitering", "region_exit"],
        "counting": True,
        "face_recognition": {"enabled": settings.face_recognition_enabled,
                             "matching_engine_implemented": False},
        "anpr": {"enabled": settings.anpr_enabled,
                 "server_ocr_implemented": False, "camera_read_ingestion": True},
    }


@router.post("/detections", status_code=201,
             summary="Ingest a detection from an external engine")
async def ingest_detection(payload: DetectionIn, session: DbSession, container: Ctr,
                           principal: CurrentPrincipal) -> dict:
    principal.require(Permission.ANALYTICS_MANAGE)
    camera = load_camera(session, principal, payload.camera_id)
    record = ingest.store(session, ingest.AnalyticsInput(
        camera_id=payload.camera_id, source=AnalyticsSource.EXTERNAL,
        object_class=payload.object_class, confidence=payload.confidence,
        occurred_at=payload.occurred_at, bbox=payload.bbox, track_id=payload.track_id,
        zone_id=payload.zone_id, rule=payload.rule, attributes=payload.attributes,
        plate_text=payload.plate_text, model_name=payload.model_name,
        model_version=payload.model_version))
    linking.link_segment(session, record)
    result = {"id": record.id, "linked_segment": record.segment_id,
              "source": record.source}
    session.commit()
    await container.events.submit(ingest.to_event(record, camera))
    return result


@router.get("/search", summary="Search analytics metadata (smart search)")
def search(session: DbSession, principal: CurrentPrincipal,
           camera_id: str | None = None,
           object_class: list[str] | None = Query(None),
           source: list[str] | None = Query(None),
           since: dt.datetime | None = None, until: dt.datetime | None = None,
           zone_id: str | None = None,
           min_confidence: float | None = Query(None, ge=0, le=1),
           limit: int = Query(200, ge=1, le=1000),
           offset: int = Query(0, ge=0)) -> dict:
    principal.require(Permission.PLAYBACK_VIEW)
    allowed = accessible_camera_ids(session, principal)
    if camera_id:
        load_camera(session, principal, camera_id, Permission.PLAYBACK_VIEW)
        camera_ids = [camera_id]
    else:
        camera_ids = allowed

    rows, total = ingest.search(session, camera_ids=camera_ids,
                                object_classes=object_class, sources=source,
                                since=since, until=until, zone_id=zone_id,
                                min_confidence=min_confidence, limit=limit,
                                offset=offset)
    return {"total": total, "limit": limit, "offset": offset,
            "items": [dto(r) for r in rows],
            "note": ("The 'source' field states whether a camera, this server, or "
                     "an external engine produced each detection.")}


@router.get("/counts", summary="Counting and occupancy aggregation")
def counts(session: DbSession, principal: CurrentPrincipal, start: dt.datetime,
           end: dt.datetime, camera_id: str | None = None,
           interval_minutes: int = Query(60, ge=1, le=1440),
           object_class: list[str] | None = Query(None)) -> dict:
    principal.require(Permission.PLAYBACK_VIEW)
    allowed = accessible_camera_ids(session, principal)
    if camera_id:
        load_camera(session, principal, camera_id, Permission.PLAYBACK_VIEW)
        camera_ids = [camera_id]
    else:
        camera_ids = allowed if allowed is not None else [
            c for (c,) in session.execute(select(AnalyticsEvent.camera_id).distinct())]

    buckets = counting.count_by_interval(session, camera_ids=camera_ids, start=start,
                                         end=end, interval_minutes=interval_minutes,
                                         object_classes=object_class)
    peak = counting.peak_occupancy(buckets)
    return {
        "buckets": [{"start": b.start, "end": b.end, "counts": b.counts,
                     "unique_tracks": b.unique_tracks} for b in buckets],
        "totals": counting.class_totals(session, camera_ids=camera_ids, start=start,
                                        end=end),
        "peak": None if peak is None else {"start": peak.start,
                                           "unique_tracks": peak.unique_tracks},
        "accuracy_note": ("Counts reflect detections supplied by cameras or external "
                          "engines. This server does not claim detection accuracy."),
    }


@router.get("/plates/search", summary="ANPR plate search (privacy controlled)")
def plate_search(request: Request, plate: str, session: DbSession, container: Ctr,
                 principal: CurrentPrincipal,
                 justification: str = Query(..., min_length=5),
                 since: dt.datetime | None = None, until: dt.datetime | None = None,
                 limit: int = Query(100, ge=1, le=500)) -> dict:
    privacy.authorize(principal, privacy.CAPABILITY_ANPR, justification,
                      container.settings, container.entitlements)
    allowed = accessible_camera_ids(session, principal)
    rows = anpr.search_plate(session, plate, camera_ids=allowed, since=since,
                             until=until, limit=limit)
    privacy.record_access(
        session,
        privacy.PrivacyContext(actor_id=principal.user_id,
                               capability=privacy.CAPABILITY_ANPR,
                               justification=justification),
        action="plate_search", subject_reference=anpr.normalize_plate(plate),
        result_count=len(rows))
    session.commit()
    return {"query": anpr.normalize_plate(plate), "count": len(rows),
            "items": [dto(r) for r in rows],
            "privacy_note": "This search was recorded in the privacy access log."}


@router.get("/zones/{camera_id}", summary="Zones configured on a camera")
def list_zones(camera_id: str, session: DbSession,
               principal: CurrentPrincipal) -> dict:
    load_camera(session, principal, camera_id, Permission.LIVE_VIEW)
    rows = session.execute(
        select(AnalyticsZone)
        .where(AnalyticsZone.camera_id == camera_id)).scalars().all()
    return {"items": [{"id": z.id, "name": z.name, "kind": z.kind,
                       "polygon": z.polygon, "direction": z.direction,
                       "dwell_seconds": z.dwell_seconds,
                       "object_classes": z.object_classes, "enabled": z.enabled}
                      for z in rows]}


@router.post("/zones/{camera_id}", status_code=201,
             summary="Create a detection zone")
def create_zone(request: Request, camera_id: str, payload: ZoneIn, session: DbSession,
                principal: CurrentPrincipal) -> dict:
    load_camera(session, principal, camera_id, Permission.ANALYTICS_MANAGE)
    try:
        polygon = validate_polygon(payload.polygon, payload.kind)
    except ValueError as exc:
        raise ValidationError(str(exc), code="invalid_zone") from exc

    zone = AnalyticsZone(camera_id=camera_id, name=payload.name, kind=payload.kind,
                         polygon=[list(p) for p in polygon],
                         direction=payload.direction,
                         object_classes=payload.object_classes,
                         dwell_seconds=payload.dwell_seconds,
                         min_confidence=payload.min_confidence,
                         enabled=payload.enabled)
    session.add(zone)
    session.flush()
    audit.record(session, action="analytics.zone.create", actor_id=principal.user_id,
                 actor_name=principal.username, object_type="camera",
                 object_id=camera_id, client_ip=client_ip(request),
                 detail={"zone": payload.name})
    session.commit()
    return {"id": zone.id, "name": zone.name, "kind": zone.kind}


@router.get("/{analytics_id}/playback", summary="Jump to the footage for a detection")
def playback_jump(analytics_id: str, session: DbSession,
                  principal: CurrentPrincipal) -> dict:
    principal.require(Permission.PLAYBACK_VIEW)
    result = linking.playback_jump(session, analytics_id)
    if result is None:
        raise NotFoundError("Detection not found", code="analytics_not_found")
    load_camera(session, principal, result["camera_id"], Permission.PLAYBACK_VIEW)
    session.commit()
    return result
