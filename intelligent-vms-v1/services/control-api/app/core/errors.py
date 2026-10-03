import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


ERROR_SCHEMA_VERSION = "v1"
log = logging.getLogger(__name__)


def build_error_payload(
    status_code: int,
    detail: Any,
    *,
    code: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """Build the compatibility-safe v1 API error envelope.

    Args:
        status_code: HTTP status code returned to the caller.
        detail: Legacy FastAPI detail value preserved without type changes.
        code: Optional stable machine-readable error code.
        message: Optional stable human-readable error message.

    Returns:
        A response payload containing the legacy detail and the v1 error object.

    Raises:
        No domain-specific exception is expected for ordinary status/detail values;
        unknown HTTP status values fall back to a generic message.
    """
    detail_code = detail.get("code") if isinstance(detail, dict) else None
    detail_message = detail.get("message") if isinstance(detail, dict) else None

    final_code = code or (str(detail_code) if detail_code else f"HTTP_{status_code}")
    if message:
        final_message = message
    elif isinstance(detail_message, str) and detail_message:
        final_message = detail_message
    elif isinstance(detail, str) and detail:
        final_message = detail
    else:
        try:
            final_message = HTTPStatus(status_code).phrase
        except ValueError:
            final_message = "Request failed"

    return {
        "detail": detail,
        "error": {
            "schema_version": ERROR_SCHEMA_VERSION,
            "code": final_code,
            "message": final_message,
        },
    }


async def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    """Render HTTP exceptions with the stable v1 error object.

    Args:
        request: Incoming request associated with the exception.
        exc: HTTP exception raised by routing, authorization, or application code.

    Returns:
        JSON response preserving the legacy detail and exception headers.

    Raises:
        Exception: Serialization failures propagate rather than hiding a server bug.
    """
    del request
    payload = build_error_payload(exc.status_code, exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=jsonable_encoder(payload),
        headers=exc.headers,
    )


async def request_validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Render request-validation errors with the stable v1 error object.

    Args:
        request: Incoming request that failed validation.
        exc: FastAPI request-validation exception.

    Returns:
        HTTP 422 response preserving FastAPI validation details.

    Raises:
        Exception: Serialization failures propagate rather than hiding a server bug.
    """
    del request
    detail = exc.errors()
    payload = build_error_payload(
        422,
        detail,
        code="REQUEST_VALIDATION_ERROR",
        message="Request validation failed",
    )
    return JSONResponse(status_code=422, content=jsonable_encoder(payload))


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return a safe v1 error envelope for an unexpected server exception.

    Args:
        request: Incoming request that triggered the failure.
        exc: Unexpected exception raised while processing the request.

    Returns:
        Generic HTTP 500 response without internal exception details.

    Raises:
        Exception: The handler itself does not intentionally raise.
    """
    log.error(
        "unhandled_api_exception method=%s error_class=%s",
        request.method,
        exc.__class__.__name__,
    )
    payload = build_error_payload(
        500,
        "Internal server error",
        code="INTERNAL_SERVER_ERROR",
        message="Internal server error",
    )
    return JSONResponse(status_code=500, content=payload)


def install_error_handlers(app: FastAPI) -> None:
    """Install the v1 error contract on a FastAPI application.

    Args:
        app: FastAPI application to configure.

    Returns:
        None after handlers are registered.

    Raises:
        Exception: FastAPI registration errors propagate to application startup.
    """
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
