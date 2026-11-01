"""Offline licensing.

Constraints taken directly from the product requirements:
  * Licensing must NEVER prevent an authorized offline installation from
    operating. There is no activation server, no phone-home, no periodic
    online check. Validation is pure local cryptography.
  * A licence is a JSON document signed with Ed25519. The vendor holds the
    private key; the product ships only the public key.
  * An absent or expired licence degrades to a limited but WORKING system.
    Stopping a customer's surveillance recording over licensing would be
    unsafe, so it never happens.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from vms_contracts.enums import Edition, FeatureFlag

from app.core.crypto import sign, verify_signature
from app.core.timeutil import ensure_utc, utcnow
from app.logging_setup import get_logger

log = get_logger("licensing")

LICENSE_VERSION = 1


@dataclass(frozen=True)
class License:
    customer: str
    edition: Edition
    cameras: int
    features: tuple[str, ...]
    issued_at: dt.datetime
    expires_at: dt.datetime | None
    license_id: str
    hardware_binding: str | None = None

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and utcnow() > self.expires_at

    def to_payload(self) -> dict:
        return {
            "version": LICENSE_VERSION,
            "license_id": self.license_id,
            "customer": self.customer,
            "edition": self.edition.value,
            "cameras": self.cameras,
            "features": list(self.features),
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "hardware_binding": self.hardware_binding,
        }


@dataclass
class LicenseStatusResult:
    valid: bool
    license: License | None = None
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)


def canonical_payload(payload: dict) -> bytes:
    """Deterministic serialization so signatures verify byte-for-byte."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def issue(private_key: bytes, *, customer: str, edition: Edition, cameras: int,
          features: list[str], valid_days: int | None = 365,
          license_id: str | None = None, hardware_binding: str | None = None) -> str:
    """Vendor-side issuance. Shipped for build tooling and tests."""
    issued = utcnow()
    expires = issued + dt.timedelta(days=valid_days) if valid_days else None
    obj = License(customer=customer, edition=edition, cameras=cameras,
                  features=tuple(features), issued_at=issued, expires_at=expires,
                  license_id=license_id or uuid.uuid4().hex,
                  hardware_binding=hardware_binding)
    payload = obj.to_payload()
    document = {"payload": payload,
                "signature": base64.b64encode(
                    sign(private_key, canonical_payload(payload))).decode("ascii")}
    return json.dumps(document, indent=2)


def parse_and_verify(document_text: str, public_key: bytes) -> LicenseStatusResult:
    try:
        document = json.loads(document_text)
        payload = document["payload"]
        signature = base64.b64decode(document["signature"])
    except (KeyError, ValueError, TypeError) as exc:
        return LicenseStatusResult(False, reason=f"licence file is malformed: {exc}")

    if not verify_signature(public_key, canonical_payload(payload), signature):
        return LicenseStatusResult(False, reason="licence signature is not valid")

    if int(payload.get("version", 0)) != LICENSE_VERSION:
        return LicenseStatusResult(
            False, reason=f"unsupported licence version {payload.get('version')}")

    try:
        obj = License(
            customer=payload["customer"],
            edition=Edition(payload["edition"]),
            cameras=int(payload["cameras"]),
            features=tuple(payload.get("features", [])),
            issued_at=ensure_utc(dt.datetime.fromisoformat(payload["issued_at"])),
            expires_at=(ensure_utc(dt.datetime.fromisoformat(payload["expires_at"]))
                        if payload.get("expires_at") else None),
            license_id=payload["license_id"],
            hardware_binding=payload.get("hardware_binding"))
    except (KeyError, ValueError) as exc:
        return LicenseStatusResult(False, reason=f"licence fields are invalid: {exc}")

    if obj.expired:
        return LicenseStatusResult(False, license=obj, reason="licence has expired")

    warnings: list[str] = []
    if obj.expires_at and (obj.expires_at - utcnow()).days <= 30:
        warnings.append(f"licence expires on {obj.expires_at.date().isoformat()}")
    return LicenseStatusResult(True, license=obj, warnings=warnings)


def load(license_path: Path | None, public_key_path: Path | None) -> LicenseStatusResult:
    if license_path is None or not Path(license_path).is_file():
        return LicenseStatusResult(False, reason="no licence file installed")
    if public_key_path is None or not Path(public_key_path).is_file():
        return LicenseStatusResult(False, reason="licence public key is not installed")
    try:
        public_key = base64.b64decode(Path(public_key_path).read_text().strip())
        return parse_and_verify(Path(license_path).read_text(encoding="utf-8"), public_key)
    except OSError as exc:
        return LicenseStatusResult(False, reason=f"could not read licence: {exc}")


def default_features_for(edition: Edition) -> list[str]:
    if edition is Edition.LITE:
        return [FeatureFlag.API_ACCESS.value]
    if edition is Edition.PRO:
        return [FeatureFlag.API_ACCESS.value, FeatureFlag.MULTI_SITE.value,
                FeatureFlag.SERVER_ANALYTICS.value, FeatureFlag.OIDC.value]
    return [flag.value for flag in FeatureFlag]
