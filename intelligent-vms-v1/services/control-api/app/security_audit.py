from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

log = logging.getLogger("vms.security.audit")
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_MAX_AUDIT_FIELD_LENGTH = 128


def _safe_request_id(value: str | None) -> str:
    if value and _REQUEST_ID.fullmatch(value):
        return value
    return uuid.uuid4().hex


def _route_label(request: Request) -> str:
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    return route_path if isinstance(route_path, str) and route_path else "__unmatched__"


def _subject_hash(subject: str | None) -> str:
    if not subject:
        return "anonymous"
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()[:16]


def _bounded_audit_text(value: object | None) -> str:
    """Return a bounded string for JSON audit fields without trusting token claims.

    Args:
        value: Arbitrary value originating from request or principal state.

    Returns:
        A non-empty string capped to the audit field length limit. JSON encoding
        later escapes control characters so one request cannot forge log lines.
    """
    if value is None:
        return "-"
    text = str(value)[:_MAX_AUDIT_FIELD_LENGTH]
    return text or "-"


class SecurityAuditMiddleware(BaseHTTPMiddleware):
    """Emit bounded security audit records for mutations and authorization failures."""

    async def dispatch(self, request: Request, call_next: Callable):
        """Audit one request without logging bodies, tokens, query strings or raw paths.

        Args:
            request: Incoming Starlette request.
            call_next: Downstream ASGI request handler.

        Returns:
            Downstream response with a bounded X-Request-ID header.

        Raises:
            Exception: Downstream failures propagate after the audit record is emitted.
        """
        request_id = _safe_request_id(request.headers.get("x-request-id"))
        request.state.request_id = request_id
        status_code = 500
        response = None
        try:
            response = await call_next(request)
            status_code = int(response.status_code)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            if request.method in _MUTATING_METHODS or status_code in {401, 403}:
                principal = getattr(request.state, "principal", None)
                event = {
                    "request_id": request_id,
                    "method": request.method,
                    "route": _route_label(request),
                    "status": status_code,
                    "subject_hash": _subject_hash(getattr(principal, "subject", None)),
                    "tenant_id": _bounded_audit_text(getattr(principal, "tenant_id", None)),
                    "roles": sorted(getattr(principal, "roles", ())),
                    "node_id": _bounded_audit_text(getattr(principal, "node_id", None)),
                }
                log.info(
                    "security_audit %s",
                    json.dumps(event, separators=(",", ":"), ensure_ascii=True),
                )
