import pytest
from fastapi.middleware.cors import CORSMiddleware

from app.main import _cors_allowlist, app


def _cors_middleware_kwargs() -> dict:
    for middleware in app.user_middleware:
        if middleware.cls is CORSMiddleware:
            return middleware.kwargs
    raise AssertionError("CORS middleware is not configured")


def test_cors_allowlist_parses_explicit_values():
    """Parse comma-separated CORS values while trimming empty entries."""
    assert _cors_allowlist(
        "https://vms.example, https://ops.example,,",
        "CORS_ALLOWED_ORIGINS",
    ) == ["https://vms.example", "https://ops.example"]


def test_cors_allowlist_allows_empty_origin_list():
    """Allow an empty origin list so production browser CORS can default off."""
    assert _cors_allowlist("", "CORS_ALLOWED_ORIGINS") == []


def test_cors_allowlist_rejects_wildcard():
    """Reject wildcard values instead of silently restoring broad CORS access."""
    with pytest.raises(ValueError, match="wildcard is not allowed"):
        _cors_allowlist("https://vms.example,*", "CORS_ALLOWED_ORIGINS")


def test_control_api_cors_uses_explicit_least_privilege_defaults():
    """Verify the configured API middleware never uses wildcard CORS defaults."""
    kwargs = _cors_middleware_kwargs()

    assert "*" not in kwargs["allow_origins"]
    assert kwargs["allow_credentials"] is False
    assert kwargs["allow_methods"] == ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    assert kwargs["allow_headers"] == ["Authorization", "Content-Type", "Range"]
