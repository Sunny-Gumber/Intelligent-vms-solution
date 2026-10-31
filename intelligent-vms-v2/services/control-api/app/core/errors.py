"""Uniform error taxonomy.

Every failure surfaces as a Problem document with a STABLE `code`. Clients
branch on the code; text is for humans. HTTP 5xx is reserved for genuine
server faults - a bad camera URL is 400, a denied permission 403, a stale
alarm version 409, an unimplemented feature 501.
"""

from __future__ import annotations

from typing import Any


class VmsError(Exception):
    status_code = 500
    code = "internal_error"
    title = "Internal error"

    def __init__(self, detail: str | None = None, *, code: str | None = None,
                 status_code: int | None = None, extra: dict[str, Any] | None = None) -> None:
        super().__init__(detail or self.title)
        self.detail = detail
        self.extra = extra or {}
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code

    def to_problem(self, correlation_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": f"https://docs.intelligent-vms.local/errors/{self.code}",
            "title": self.title,
            "status": self.status_code,
            "code": self.code,
            "detail": self.detail,
            "correlation_id": correlation_id,
        }
        body.update(self.extra)
        return body


class ValidationError(VmsError):
    status_code = 400
    code = "validation_error"
    title = "Request validation failed"


class AuthenticationError(VmsError):
    status_code = 401
    code = "authentication_failed"
    title = "Authentication failed"


class MfaRequiredError(VmsError):
    status_code = 401
    code = "mfa_required"
    title = "Multi-factor authentication required"


class AccountLockedError(VmsError):
    status_code = 423
    code = "account_locked"
    title = "Account temporarily locked"


class PermissionDeniedError(VmsError):
    status_code = 403
    code = "permission_denied"
    title = "Permission denied"


class NotFoundError(VmsError):
    status_code = 404
    code = "not_found"
    title = "Resource not found"


class ConflictError(VmsError):
    status_code = 409
    code = "conflict"
    title = "Conflicting state"


class StaleVersionError(ConflictError):
    code = "stale_version"
    title = "The resource was modified by someone else"


class CapacityError(VmsError):
    status_code = 429
    code = "capacity_exceeded"
    title = "Capacity limit reached"


class RateLimitedError(VmsError):
    status_code = 429
    code = "rate_limited"
    title = "Too many requests"


class LicenseError(VmsError):
    status_code = 402
    code = "license_limit"
    title = "License limit reached"


class DependencyUnavailableError(VmsError):
    status_code = 503
    code = "dependency_unavailable"
    title = "A required component is unavailable"


class NotImplementedFeatureError(VmsError):
    """Explicitly tells the caller a feature is not implemented in this build.

    Required by GOLDEN RULE 1.3: never pretend an unimplemented feature works.
    """

    status_code = 501
    code = "not_implemented"
    title = "Feature not implemented in this release"


class MediaUnavailableError(VmsError):
    status_code = 404
    code = "media_unavailable"
    title = "Requested media is not available"


class DeviceError(VmsError):
    status_code = 502
    code = "device_error"
    title = "Camera returned an error"


class DeviceUnauthorizedError(DeviceError):
    code = "device_unauthorized"
    title = "Camera rejected the stored credentials"


class StorageError(VmsError):
    status_code = 507
    code = "storage_error"
    title = "Storage problem"
