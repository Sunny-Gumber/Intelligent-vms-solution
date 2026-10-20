"""Runtime feature entitlement.

Enforcement points are deliberately few and explicit:
  * camera onboarding (count limit)
  * federation registration
  * server-side analytics worker start
  * facial recognition / ANPR query paths
  * OIDC login

Everything else works regardless of licence, including recording, playback and
export of EXISTING cameras. A licence problem must never destroy evidence or
stop an operating site.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from vms_contracts.dto import LicenseStatus
from vms_contracts.enums import Edition, FeatureFlag

from app.config import Settings, get_settings
from app.core.errors import LicenseError
from app.db.models import Camera
from app.licensing.editions import spec_for
from app.licensing.license_file import LicenseStatusResult, load
from app.logging_setup import get_logger

log = get_logger("licensing.entitlements")


@dataclass
class Entitlements:
    edition: Edition
    max_cameras: int
    features: frozenset[FeatureFlag]
    valid: bool
    customer: str | None = None
    reason: str | None = None
    expires_at: dt.datetime | None = field(default=None)

    def has(self, flag: FeatureFlag) -> bool:
        return flag in self.features

    def require(self, flag: FeatureFlag) -> None:
        if not self.has(flag):
            raise LicenseError(
                f"The feature '{flag.value}' is not included in the installed "
                f"licence ({self.edition.value}).",
                code="feature_not_licensed", extra={"feature": flag.value})

    def check_camera_quota(self, session: Session, adding: int = 1) -> None:
        current = int(session.execute(
            select(func.count()).select_from(Camera)).scalar_one())
        if current + adding > self.max_cameras:
            raise LicenseError(
                f"This licence permits {self.max_cameras} cameras and {current} are "
                "already configured. Existing cameras continue to record.",
                code="camera_limit_reached",
                extra={"limit": self.max_cameras, "current": current})

    def to_dto(self, cameras_in_use: int) -> LicenseStatus:
        return LicenseStatus(
            valid=self.valid, edition=self.edition, licensed_cameras=self.max_cameras,
            cameras_in_use=cameras_in_use,
            features=sorted(f.value for f in self.features),
            expires_at=self.expires_at, customer=self.customer, reason=self.reason,
            offline=True)


def unlicensed(settings: Settings) -> Entitlements:
    """What an installation without a licence can do: a working small system."""
    return Entitlements(
        edition=Edition.LITE, max_cameras=settings.unlicensed_camera_limit,
        features=frozenset({FeatureFlag.API_ACCESS}), valid=False,
        reason="no valid licence installed; running in limited mode")


def resolve(settings: Settings | None = None,
            result: LicenseStatusResult | None = None) -> Entitlements:
    settings = settings or get_settings()
    result = result or load(settings.license_file, settings.license_public_key_file)

    if not result.valid or result.license is None:
        log.warning("licensing.unlicensed", reason=result.reason)
        return unlicensed(settings)

    licence = result.license
    spec = spec_for(licence.edition)
    known = {flag.value for flag in FeatureFlag}
    features = {FeatureFlag(f) for f in licence.features if f in known} | spec.features

    log.info("licensing.loaded", edition=licence.edition.value, cameras=licence.cameras,
             customer=licence.customer)
    return Entitlements(
        edition=licence.edition, max_cameras=min(licence.cameras, spec.max_cameras),
        features=frozenset(features), valid=True, customer=licence.customer,
        expires_at=licence.expires_at)
