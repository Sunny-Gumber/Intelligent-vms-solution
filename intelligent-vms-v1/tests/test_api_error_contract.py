import asyncio
import json

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import (
    build_error_payload,
    http_exception_handler,
    request_validation_exception_handler,
    unhandled_exception_handler,
)
from app.main import app


def _request(path: str = "/api/v1/test") -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 10000),
        "server": ("testserver", 80),
        "root_path": "",
    }
    return Request(scope)


def _json_body(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def test_string_detail_is_preserved_with_stable_error_object():
    """Keep legacy string detail while adding a stable machine-readable error."""
    payload = build_error_payload(404, "Resource not found")

    assert payload["detail"] == "Resource not found"
    assert payload["error"] == {
        "schema_version": "v1",
        "code": "HTTP_404",
        "message": "Resource not found",
    }


def test_structured_detail_code_and_message_are_reused_without_mutation():
    """Preserve an existing structured detail and promote its stable fields."""
    detail = {"code": "MODE_NOT_IMPLEMENTED", "message": "Not implemented"}

    payload = build_error_payload(422, detail)

    assert payload["detail"] is detail
    assert payload["error"]["code"] == "MODE_NOT_IMPLEMENTED"
    assert payload["error"]["message"] == "Not implemented"


def test_http_exception_handler_preserves_authentication_headers():
    """Keep compatibility-critical HTTP headers while applying the error envelope."""
    exc = StarletteHTTPException(
        status_code=401,
        detail="Bearer token required",
        headers={"WWW-Authenticate": "Bearer"},
    )

    response = asyncio.run(http_exception_handler(_request(), exc))
    payload = _json_body(response)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert payload["detail"] == "Bearer token required"
    assert payload["error"]["code"] == "HTTP_401"


def test_validation_handler_preserves_fastapi_detail_list():
    """Keep validation details while adding the stable validation error code."""
    exc = RequestValidationError(
        [
            {
                "type": "missing",
                "loc": ("query", "camera_id"),
                "msg": "Field required",
                "input": None,
            }
        ]
    )

    response = asyncio.run(request_validation_exception_handler(_request(), exc))
    payload = _json_body(response)

    assert response.status_code == 422
    assert isinstance(payload["detail"], list)
    assert payload["error"] == {
        "schema_version": "v1",
        "code": "REQUEST_VALIDATION_ERROR",
        "message": "Request validation failed",
    }


def test_unhandled_exception_handler_does_not_expose_exception_or_path(caplog):
    """Return a generic 500 without logging exception text or raw path values."""
    exc = RuntimeError("database password must never reach client")
    sensitive_path_value = "customer-personal-id"
    request = _request(f"/api/v1/cameras/{sensitive_path_value}")

    response = asyncio.run(unhandled_exception_handler(request, exc))
    payload = _json_body(response)

    assert response.status_code == 500
    assert payload["detail"] == "Internal server error"
    assert payload["error"]["code"] == "INTERNAL_SERVER_ERROR"
    assert "database password" not in response.body.decode("utf-8")
    assert "unhandled_api_exception" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "database password" not in caplog.text
    assert sensitive_path_value not in caplog.text


def test_application_registers_v1_error_handlers():
    """Verify the running API uses the compatibility-safe v1 error handlers."""
    assert app.exception_handlers[StarletteHTTPException] is http_exception_handler
    assert app.exception_handlers[RequestValidationError] is request_validation_exception_handler
    assert app.exception_handlers[Exception] is unhandled_exception_handler
