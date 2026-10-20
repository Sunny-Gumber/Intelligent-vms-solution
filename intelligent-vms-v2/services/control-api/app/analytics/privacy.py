"""Privacy controls for facial recognition and ANPR.

These capabilities are regulated in most jurisdictions (GDPR Art. 9 in the EU,
the DPDP Act in India, BIPA in Illinois). The product therefore enforces, in
code:

  1. FEATURE GATE — disabled by default; must be enabled in configuration AND
     licensed.
  2. PERMISSION   — a dedicated permission, separate from ordinary playback.
  3. PURPOSE      — every query records a justification string.
  4. AUDIT        — every access is written to privacy_access_log, which is
                    separate from the general audit log and is never purged by
                    the normal retention job.
  5. RETENTION    — biometric and plate data has its own, shorter retention,
                    applied independently of video retention.

IMPLEMENTATION STATUS: the controls above are implemented. The RECOGNITION
ENGINES are not. This release stores and searches face/plate metadata supplied
by cameras or external engines. No face-matching or OCR model is bundled.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from vms_contracts.enums import FeatureFlag, Permission

from app.config import Settings, get_settings
from app.core.errors import PermissionDeniedError, ValidationError
from app.core.timeutil import utcnow
from app.db.models import AnalyticsEvent, PrivacyAccessLog
from app.logging_setup import get_logger

log = get_logger("analytics.privacy")

CAPABILITY_FACE = "face_recognition"
CAPABILITY_ANPR = "anpr"


@dataclass(frozen=True)
class PrivacyContext:
    actor_id: str
    capability: str
    justification: str | None
    camera_id: str | None = None


def assert_enabled(capability: str, settings: Settings | None = None,
                   entitlements=None) -> None:  # noqa: ANN001
    settings = settings or get_settings()
    if capability == CAPABILITY_FACE and not settings.face_recognition_enabled:
        raise PermissionDeniedError(
            "Facial recognition is disabled on this server. An administrator must enable "
            "it and confirm the legal basis for processing.",
            code="face_recognition_disabled")
    if capability == CAPABILITY_ANPR and not settings.anpr_enabled:
        raise PermissionDeniedError("ANPR is disabled on this server.", code="anpr_disabled")
    if entitlements is not None:
        flag = (FeatureFlag.FACE_RECOGNITION if capability == CAPABILITY_FACE
                else FeatureFlag.ANPR)
        if not entitlements.has(flag):
            raise PermissionDeniedError(
                f"The installed licence does not include {flag.value}.",
                code="feature_not_licensed")


def required_permission(capability: str) -> Permission:
    return (Permission.FACE_RECOGNITION_USE if capability == CAPABILITY_FACE
            else Permission.ANPR_USE)


def authorize(principal, capability: str, justification: str | None,  # noqa: ANN001
              settings: Settings | None = None, entitlements=None) -> None:  # noqa: ANN001
    assert_enabled(capability, settings, entitlements)
    principal.require(required_permission(capability))
    if not justification or len(justification.strip()) < 5:
        raise ValidationError(
            "A purpose must be recorded for this search (minimum 5 characters). This is "
            "stored in the privacy access log.", code="justification_required")


def record_access(session: Session, context: PrivacyContext, *, action: str,
                  subject_reference: str | None = None,
                  result_count: int | None = None) -> PrivacyAccessLog:
    entry = PrivacyAccessLog(actor_id=context.actor_id, capability=context.capability,
                             action=action, subject_reference=subject_reference,
                             camera_id=context.camera_id,
                             justification=(context.justification or "")[:512] or None,
                             result_count=result_count)
    session.add(entry)
    log.info("privacy.access", actor=context.actor_id, capability=context.capability,
             action=action, results=result_count)
    return entry


def apply_privacy_retention(session: Session, settings: Settings | None = None) -> int:
    """Strip biometric/plate attributes older than the privacy retention."""
    settings = settings or get_settings()
    cutoff = utcnow() - dt.timedelta(days=settings.privacy_retention_days)
    rows = session.execute(
        select(AnalyticsEvent).where(
            AnalyticsEvent.occurred_at < cutoff,
            (AnalyticsEvent.plate_text.is_not(None))
            | (AnalyticsEvent.face_subject_id.is_not(None)))).scalars().all()
    for row in rows:
        row.plate_text = None
        row.face_subject_id = None
        attributes = dict(row.attributes or {})
        for key in list(attributes):
            if key.lower() in {"face_embedding", "plate_image", "face_image", "embedding"}:
                attributes.pop(key)
        row.attributes = attributes
    if rows:
        log.info("privacy.retention_applied", rows=len(rows),
                 retention_days=settings.privacy_retention_days)
    return len(rows)


def purge_privacy_log(session: Session, older_than_days: int = 730) -> int:
    """Privacy logs are kept far longer than video, purged only on request."""
    cutoff = utcnow() - dt.timedelta(days=older_than_days)
    return int(session.execute(
        delete(PrivacyAccessLog).where(PrivacyAccessLog.at < cutoff)).rowcount or 0)
